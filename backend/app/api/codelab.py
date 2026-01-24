"""
代码实验室 API - Jupyter-style Notebook 后端
支持代码单元执行、Notebook 管理、输出处理

核心改进：
1. 实现持久化的执行内核，让 cell 之间共享执行上下文
2. 数据库持久化存储，重启后数据不丢失
3. 内存缓存加速访问
"""
import asyncio
import subprocess
import tempfile
import os
import json
import base64
import uuid
import io
import sys
import threading
import queue
import traceback
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from loguru import logger
from contextlib import redirect_stdout, redirect_stderr

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.notebook_service import NotebookService
from app.config import settings

router = APIRouter()

# ========== Pydantic Models ==========

class CellOutput(BaseModel):
    """单元格输出"""
    output_type: str  # 'stream', 'execute_result', 'display_data', 'error'
    content: Any
    mime_type: Optional[str] = None

class Cell(BaseModel):
    """Notebook 单元格"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    cell_type: str = "code"  # 'code' or 'markdown'
    source: str = ""
    outputs: List[CellOutput] = []
    execution_count: Optional[int] = None
    metadata: Dict[str, Any] = {}

class NotebookCreate(BaseModel):
    """创建 Notebook"""
    title: str = "Untitled Notebook"
    description: Optional[str] = None

class NotebookUpdate(BaseModel):
    """更新 Notebook"""
    title: Optional[str] = None
    description: Optional[str] = None
    cells: Optional[List[Cell]] = None

class NotebookResponse(BaseModel):
    """Notebook 响应"""
    id: str
    user_id: int
    title: str
    description: Optional[str]
    cells: List[Cell]
    created_at: datetime
    updated_at: datetime
    execution_count: int

class ExecuteRequest(BaseModel):
    """代码执行请求"""
    code: str
    cell_id: Optional[str] = None
    timeout: int = None  # 执行超时（秒），默认使用配置值
    
    def get_timeout(self) -> int:
        return self.timeout if self.timeout is not None else settings.code_execution_timeout

class ExecuteResponse(BaseModel):
    """代码执行响应"""
    success: bool
    outputs: List[CellOutput]
    execution_count: int
    execution_time_ms: int


# ========== 持久化执行内核 ==========

class PythonKernel:
    """
    Python 执行内核 - 为每个 Notebook 维护一个持久化的执行上下文
    所有 cell 共享同一个命名空间，变量在 cell 之间保持
    """
    
    def __init__(self, notebook_id: str):
        self.notebook_id = notebook_id
        self.execution_count = 0
        self.created_at = datetime.utcnow()
        self.last_used_at = datetime.utcnow()
        
        # 共享的命名空间 - 所有 cell 在这里执行
        self.namespace: Dict[str, Any] = {}
        
        # 初始化命名空间，预导入常用库
        self._initialize_namespace()
        
        logger.info(f"创建执行内核: notebook_id={notebook_id}")
    
    def _initialize_namespace(self):
        """初始化命名空间，预导入常用库"""
        # 基础模块
        init_code = """
import sys
import os
import io
import json
import math
import random
import time
import datetime
import re
import collections
import itertools
import functools
from typing import *

# 数据科学常用库
try:
    import numpy as np
except ImportError:
    pass

try:
    import pandas as pd
except ImportError:
    pass

try:
    import matplotlib
    matplotlib.use('Agg')  # 非交互式后端
    import matplotlib.pyplot as plt
except ImportError:
    pass

# 机器学习库
try:
    import sklearn
except ImportError:
    pass

try:
    import torch
except ImportError:
    pass

# 图表相关
_plot_outputs = []

def _capture_plot():
    '''捕获 matplotlib 图表'''
    try:
        import matplotlib.pyplot as plt
        if plt.get_fignums():
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0f172a')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            _plot_outputs.append(img_base64)
            plt.close('all')
            buf.close()
    except:
        pass

def show():
    '''替代 plt.show()，用于在 notebook 中显示图表'''
    _capture_plot()

# 替换 plt.show
try:
    import matplotlib.pyplot as plt
    plt.show = show
except:
    pass
"""
        try:
            # 添加一些必要的模块到命名空间
            self.namespace['__builtins__'] = __builtins__
            self.namespace['io'] = io
            self.namespace['base64'] = base64
            
            exec(init_code, self.namespace)
            logger.debug(f"内核初始化完成: notebook_id={self.notebook_id}")
        except Exception as e:
            logger.warning(f"内核初始化部分失败: {e}")
    
    def execute(self, code: str, timeout: int = 30) -> Dict[str, Any]:
        """
        在持久化的命名空间中执行代码
        返回执行结果，包括输出、图表、错误等
        """
        self.execution_count += 1
        self.last_used_at = datetime.utcnow()
        
        start_time = time.time()
        outputs: List[CellOutput] = []
        success = True
        
        # 重置图表输出列表
        self.namespace['_plot_outputs'] = []
        
        # 捕获标准输出和标准错误
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        
        try:
            # 使用 compile 来检测是否是表达式
            # 如果最后一行是表达式，我们需要特殊处理以显示其值
            lines = code.strip().split('\n')
            last_line = lines[-1].strip() if lines else ''
            
            # 尝试将最后一行作为表达式编译
            last_expr_value = None
            main_code = code
            
            # 检查最后一行是否是表达式（不是赋值、import等语句）
            try:
                if last_line and not any(last_line.startswith(kw) for kw in 
                    ['import ', 'from ', 'def ', 'class ', 'if ', 'for ', 'while ', 
                     'try:', 'with ', 'return ', 'raise ', 'pass', 'break', 'continue',
                     '#', '@']):
                    # 检查是否是赋值语句
                    if '=' in last_line and not any(op in last_line for op in ['==', '!=', '<=', '>=', '+=', '-=', '*=', '/=']):
                        # 这是赋值语句，不需要特殊处理
                        pass
                    else:
                        # 尝试作为表达式编译
                        compile(last_line, '<string>', 'eval')
                        # 成功，说明最后一行是表达式
                        main_code = '\n'.join(lines[:-1]) if len(lines) > 1 else ''
            except SyntaxError:
                pass
            
            # 执行主代码
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                if main_code.strip():
                    exec(main_code, self.namespace)
                
                # 如果最后一行是表达式，评估它
                if main_code != code and last_line:
                    try:
                        last_expr_value = eval(last_line, self.namespace)
                    except:
                        # 如果 eval 失败，尝试 exec
                        exec(last_line, self.namespace)
                elif not main_code.strip() and last_line:
                    # 整个代码就是一个表达式
                    try:
                        last_expr_value = eval(code, self.namespace)
                    except:
                        exec(code, self.namespace)
            
            # 捕获任何未关闭的图表
            try:
                if '_capture_plot' in self.namespace:
                    self.namespace['_capture_plot']()
            except:
                pass
            
            # 处理标准输出
            stdout_text = stdout_capture.getvalue()
            if stdout_text:
                outputs.append(CellOutput(
                    output_type='stream',
                    content=stdout_text.rstrip('\n'),
                    mime_type='text/plain'
                ))
            
            # 处理图表输出
            plot_outputs = self.namespace.get('_plot_outputs', [])
            for plot_base64 in plot_outputs:
                outputs.append(CellOutput(
                    output_type='display_data',
                    content=f'data:image/png;base64,{plot_base64}',
                    mime_type='image/png'
                ))
            
            # 处理最后一个表达式的值
            if last_expr_value is not None:
                # 特殊处理 DataFrame 和 Series
                display_value = self._format_value(last_expr_value)
                outputs.append(CellOutput(
                    output_type='execute_result',
                    content=display_value,
                    mime_type='text/plain'
                ))
            
            # 处理标准错误（警告等）
            stderr_text = stderr_capture.getvalue()
            if stderr_text:
                # 过滤掉一些无关紧要的警告
                filtered_lines = [l for l in stderr_text.split('\n') 
                                  if l and not l.startswith('WARNING')]
                if filtered_lines:
                    outputs.append(CellOutput(
                        output_type='stream',
                        content='\n'.join(filtered_lines),
                        mime_type='text/stderr'
                    ))
        
        except Exception as e:
            success = False
            # 获取详细的错误信息
            tb_lines = traceback.format_exception(type(e), e, e.__traceback__)
            
            # 过滤掉内部调用栈，只保留用户代码相关的部分
            filtered_tb = []
            for line in tb_lines:
                if '<string>' in line or 'exec(' not in line:
                    filtered_tb.append(line)
            
            outputs.append(CellOutput(
                output_type='error',
                content={
                    'ename': type(e).__name__,
                    'evalue': str(e),
                    'traceback': filtered_tb if filtered_tb else tb_lines
                }
            ))
        
        finally:
            stdout_capture.close()
            stderr_capture.close()
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        return {
            'success': success,
            'outputs': outputs,
            'execution_count': self.execution_count,
            'execution_time_ms': execution_time_ms
        }
    
    def _format_value(self, value: Any) -> str:
        """格式化输出值，特殊处理某些类型"""
        try:
            # 检查是否是 pandas DataFrame
            if hasattr(value, 'to_string') and hasattr(value, 'shape'):
                # 限制显示的行数和列数
                if hasattr(value, 'head'):
                    return value.head(50).to_string()
                return value.to_string()
            
            # 检查是否是 numpy array
            if hasattr(value, 'shape') and hasattr(value, '__array__'):
                return repr(value)
            
            # 默认使用 repr
            return repr(value)
        except:
            return str(value)
    
    def reset(self):
        """重置内核状态"""
        self.namespace.clear()
        self.execution_count = 0
        self._initialize_namespace()
        logger.info(f"内核已重置: notebook_id={self.notebook_id}")
    
    def get_variables(self) -> Dict[str, str]:
        """获取当前命名空间中的变量列表（用于调试/显示）"""
        variables = {}
        for name, value in self.namespace.items():
            if not name.startswith('_') and not callable(value) and not isinstance(value, type):
                try:
                    variables[name] = type(value).__name__
                except:
                    pass
        return variables


# ========== 内核管理器 ==========

class KernelManager:
    """
    管理所有 Notebook 的执行内核
    负责创建、获取、销毁内核
    """
    
    def __init__(self):
        self._kernels: Dict[str, PythonKernel] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = 3600  # 1小时清理一次不活跃的内核
        self._kernel_timeout = settings.kernel_idle_timeout  # 使用配置的超时值
        
        # 启动后台清理任务
        self._start_cleanup_task()
    
    def get_or_create_kernel(self, notebook_id: str) -> PythonKernel:
        """获取或创建 Notebook 的执行内核"""
        with self._lock:
            if notebook_id not in self._kernels:
                self._kernels[notebook_id] = PythonKernel(notebook_id)
            return self._kernels[notebook_id]
    
    def get_kernel(self, notebook_id: str) -> Optional[PythonKernel]:
        """获取 Notebook 的执行内核（如果存在）"""
        return self._kernels.get(notebook_id)
    
    def reset_kernel(self, notebook_id: str) -> PythonKernel:
        """重置 Notebook 的执行内核"""
        with self._lock:
            if notebook_id in self._kernels:
                self._kernels[notebook_id].reset()
            else:
                self._kernels[notebook_id] = PythonKernel(notebook_id)
            return self._kernels[notebook_id]
    
    def destroy_kernel(self, notebook_id: str):
        """销毁 Notebook 的执行内核"""
        with self._lock:
            if notebook_id in self._kernels:
                del self._kernels[notebook_id]
                logger.info(f"内核已销毁: notebook_id={notebook_id}")
    
    def _start_cleanup_task(self):
        """启动后台清理任务"""
        def cleanup():
            while True:
                time.sleep(self._cleanup_interval)
                self._cleanup_inactive_kernels()
        
        thread = threading.Thread(target=cleanup, daemon=True)
        thread.start()
    
    def _cleanup_inactive_kernels(self):
        """清理不活跃的内核"""
        now = datetime.utcnow()
        to_remove = []
        
        with self._lock:
            for notebook_id, kernel in self._kernels.items():
                inactive_seconds = (now - kernel.last_used_at).total_seconds()
                if inactive_seconds > self._kernel_timeout:
                    to_remove.append(notebook_id)
            
            for notebook_id in to_remove:
                del self._kernels[notebook_id]
                logger.info(f"清理不活跃内核: notebook_id={notebook_id}")


# 全局内核管理器实例
kernel_manager = KernelManager()


# ========== 内存缓存 + 数据库持久化 ==========

# 内存缓存：用于快速访问和 Agent 工具的实时交互
_notebooks_cache: Dict[str, Dict] = {}

# 标记已从数据库加载的用户
_loaded_users: set = set()


async def _sync_to_cache(notebook: Dict):
    """同步 Notebook 到缓存"""
    _notebooks_cache[notebook['id']] = notebook


async def _load_user_notebooks_to_cache(db: AsyncSession, user_id: int):
    """从数据库加载用户的 Notebooks 到缓存"""
    if user_id in _loaded_users:
        return
    
    service = NotebookService(db)
    notebooks = await service.get_user_notebooks(user_id)
    for nb in notebooks:
        _notebooks_cache[nb['id']] = nb
    _loaded_users.add(user_id)
    logger.info(f"已加载用户 {user_id} 的 {len(notebooks)} 个 Notebook 到缓存")


async def get_user_notebooks_cached(db: AsyncSession, user_id: int) -> List[Dict]:
    """获取用户的所有 Notebook（带缓存）"""
    await _load_user_notebooks_to_cache(db, user_id)
    return [nb for nb in _notebooks_cache.values() if nb.get('user_id') == user_id]


async def get_notebook_cached(db: AsyncSession, notebook_id: str, user_id: int) -> Optional[Dict]:
    """获取单个 Notebook（带缓存）"""
    # 先查缓存
    if notebook_id in _notebooks_cache:
        nb = _notebooks_cache[notebook_id]
        if nb.get('user_id') == user_id:
            return nb
    
    # 缓存未命中，从数据库加载
    service = NotebookService(db)
    nb = await service.get_notebook(notebook_id, user_id)
    if nb:
        _notebooks_cache[notebook_id] = nb
    return nb


# 兼容旧代码的全局访问（用于 Agent 工具）
_notebooks = _notebooks_cache


def get_user_notebooks(user_id: int) -> List[Dict]:
    """获取用户的所有 Notebook（同步版本，仅从缓存）"""
    return [nb for nb in _notebooks_cache.values() if nb.get('user_id') == user_id]


def get_notebook(notebook_id: str, user_id: int) -> Optional[Dict]:
    """获取单个 Notebook（同步版本，仅从缓存）"""
    nb = _notebooks_cache.get(notebook_id)
    if nb and nb.get('user_id') == user_id:
        return nb
    return None


# ========== API 端点 ==========

@router.get("/notebooks", response_model=List[NotebookResponse])
async def list_notebooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户的所有 Notebook"""
    notebooks = await get_user_notebooks_cached(db, current_user.id)
    
    # 定义排序键函数，处理 datetime 对象和 ISO 字符串的混合情况
    def sort_key(x):
        val = x.get('updated_at')
        if val is None:
            return datetime.min
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                return datetime.min
        return datetime.min
    
    return sorted(notebooks, key=sort_key, reverse=True)


@router.post("/notebooks", response_model=NotebookResponse)
async def create_notebook(
    data: NotebookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建新的 Notebook"""
    service = NotebookService(db)
    notebook = await service.create_notebook(
        user_id=current_user.id,
        title=data.title,
        description=data.description
    )
    
    # 同步到缓存
    _notebooks_cache[notebook['id']] = notebook
    
    # 预创建内核
    kernel_manager.get_or_create_kernel(notebook['id'])
    
    return notebook


@router.get("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def get_notebook_detail(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取 Notebook 详情"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    return notebook


@router.patch("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(
    notebook_id: str,
    data: NotebookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新 Notebook"""
    service = NotebookService(db)
    
    # 如果更新 cells，需要特殊处理
    if data.cells is not None:
        # 先获取当前 notebook
        notebook = await get_notebook_cached(db, notebook_id, current_user.id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook 不存在")
        
        # 更新基本信息
        if data.title is not None or data.description is not None:
            await service.update_notebook(notebook_id, current_user.id, data.title, data.description)
        
        # 更新 cells - 逐个更新
        for cell_data in data.cells:
            cell_dict = cell_data.dict() if hasattr(cell_data, 'dict') else cell_data
            await service.update_cell(
                notebook_id, current_user.id, cell_dict['id'],
                source=cell_dict.get('source'),
                cell_type=cell_dict.get('cell_type'),
                outputs=cell_dict.get('outputs'),
                execution_count=cell_dict.get('execution_count')
            )
        
        # 重新获取更新后的 notebook
        notebook = await service.get_notebook(notebook_id, current_user.id)
    else:
        # 只更新基本信息
        notebook = await service.update_notebook(notebook_id, current_user.id, data.title, data.description)
    
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 同步到缓存
    _notebooks_cache[notebook_id] = notebook
    
    return notebook


@router.delete("/notebooks/{notebook_id}")
async def delete_notebook(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除 Notebook"""
    service = NotebookService(db)
    deleted = await service.delete_notebook(notebook_id, current_user.id)
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 从缓存中移除
    if notebook_id in _notebooks_cache:
        del _notebooks_cache[notebook_id]
    
    # 销毁对应的内核
    kernel_manager.destroy_kernel(notebook_id)
    
    return {"message": "Notebook 已删除"}


@router.post("/notebooks/{notebook_id}/execute", response_model=ExecuteResponse)
async def execute_cell(
    notebook_id: str,
    request: ExecuteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    执行代码单元格
    使用持久化的执行内核，cell 之间共享变量
    """
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取或创建执行内核
    kernel = kernel_manager.get_or_create_kernel(notebook_id)
    
    # 在内核中执行代码
    result = kernel.execute(request.code, request.get_timeout())
    
    # 序列化输出
    serialized_outputs = []
    for o in result['outputs']:
        if hasattr(o, 'model_dump'):
            serialized_outputs.append(o.model_dump())
        elif hasattr(o, 'dict'):
            serialized_outputs.append(o.dict())
        elif isinstance(o, dict):
            serialized_outputs.append(o)
        else:
            serialized_outputs.append({'output_type': 'unknown', 'content': str(o)})
    
    # 更新数据库中的单元格输出
    if request.cell_id:
        service = NotebookService(db)
        await service.save_cell_execution(
            notebook_id, current_user.id, request.cell_id,
            serialized_outputs, result['execution_count']
        )
        
        # 更新缓存
        for cell in notebook['cells']:
            if cell['id'] == request.cell_id:
                cell['outputs'] = serialized_outputs
                cell['execution_count'] = result['execution_count']
                break
    
    notebook['updated_at'] = datetime.utcnow()
    notebook['execution_count'] = result['execution_count']
    _notebooks_cache[notebook_id] = notebook
    
    return ExecuteResponse(
        success=result['success'],
        outputs=result['outputs'],
        execution_count=result['execution_count'],
        execution_time_ms=result['execution_time_ms']
    )


@router.post("/execute", response_model=ExecuteResponse)
async def execute_code_directly(
    request: ExecuteRequest,
    current_user: User = Depends(get_current_user)
):
    """直接执行代码（使用临时内核，不保存状态）"""
    # 创建一个临时内核
    temp_kernel = PythonKernel(f"temp_{uuid.uuid4()}")
    result = temp_kernel.execute(request.code, request.get_timeout())
    
    return ExecuteResponse(
        success=result['success'],
        outputs=result['outputs'],
        execution_count=0,
        execution_time_ms=result['execution_time_ms']
    )


@router.post("/notebooks/{notebook_id}/cells")
async def add_cell(
    notebook_id: str,
    cell_type: str = "code",
    index: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """添加新单元格"""
    service = NotebookService(db)
    notebook = await service.add_cell(notebook_id, current_user.id, cell_type, '', index)
    
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 同步到缓存
    _notebooks_cache[notebook_id] = notebook
    
    # 返回新创建的单元格
    cells = notebook['cells']
    if index is not None and 0 <= index < len(cells):
        return cells[index]
    return cells[-1]


@router.delete("/notebooks/{notebook_id}/cells/{cell_id}")
async def delete_cell(
    notebook_id: str,
    cell_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除单元格"""
    service = NotebookService(db)
    notebook = await service.delete_cell(notebook_id, current_user.id, cell_id)
    
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 同步到缓存
    _notebooks_cache[notebook_id] = notebook
    
    return {"message": "单元格已删除"}


@router.post("/notebooks/{notebook_id}/run-all")
async def run_all_cells(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """运行所有代码单元格"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取执行内核
    kernel = kernel_manager.get_or_create_kernel(notebook_id)
    service = NotebookService(db)
    
    results = []
    for cell in notebook['cells']:
        if cell['cell_type'] == 'code' and cell['source'].strip():
            # 执行代码
            result = kernel.execute(cell['source'], timeout=settings.code_execution_timeout)
            
            # 序列化输出
            serialized_outputs = []
            for o in result['outputs']:
                if hasattr(o, 'model_dump'):
                    serialized_outputs.append(o.model_dump())
                elif hasattr(o, 'dict'):
                    serialized_outputs.append(o.dict())
                elif isinstance(o, dict):
                    serialized_outputs.append(o)
                else:
                    serialized_outputs.append({'output_type': 'unknown', 'content': str(o)})
            
            cell['outputs'] = serialized_outputs
            cell['execution_count'] = result['execution_count']
            
            # 保存到数据库
            await service.save_cell_execution(
                notebook_id, current_user.id, cell['id'],
                serialized_outputs, result['execution_count']
            )
            
            results.append({
                'cell_id': cell['id'],
                'success': result['success'],
                'execution_count': result['execution_count']
            })
    
    notebook['updated_at'] = datetime.utcnow()
    notebook['execution_count'] = kernel.execution_count
    _notebooks_cache[notebook_id] = notebook
    
    # 更新 notebook 执行计数
    await service.update_execution_count(notebook_id, current_user.id, kernel.execution_count)
    
    return {
        'message': f'已执行 {len(results)} 个单元格',
        'results': results
    }


@router.post("/notebooks/{notebook_id}/restart-kernel")
async def restart_kernel(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """重启内核（清除所有变量状态）"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 重置内核
    kernel_manager.reset_kernel(notebook_id)
    
    # 清除所有 cell 的输出和执行计数
    service = NotebookService(db)
    for cell in notebook['cells']:
        cell['outputs'] = []
        cell['execution_count'] = None
        await service.update_cell(notebook_id, current_user.id, cell['id'], outputs=[], execution_count=None)
    
    notebook['execution_count'] = 0
    notebook['updated_at'] = datetime.utcnow()
    _notebooks_cache[notebook_id] = notebook
    
    await service.update_execution_count(notebook_id, current_user.id, 0)
    
    return {"message": "内核已重启，所有变量已清除"}


@router.get("/notebooks/{notebook_id}/kernel-status")
async def get_kernel_status(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取内核状态"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    kernel = kernel_manager.get_kernel(notebook_id)
    
    if kernel:
        return {
            'status': 'running',
            'execution_count': kernel.execution_count,
            'created_at': kernel.created_at.isoformat(),
            'last_used_at': kernel.last_used_at.isoformat(),
            'variables': kernel.get_variables()
        }
    else:
        return {
            'status': 'stopped',
            'execution_count': 0,
            'variables': {}
        }


@router.post("/notebooks/{notebook_id}/interrupt")
async def interrupt_kernel(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """中断内核执行（当前实现中，由于使用同步执行，此功能有限）"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 注意：当前实现使用同步执行，无法真正中断
    # 未来可以考虑使用多进程来实现真正的中断功能
    return {"message": "中断请求已发送"}


# ========== Notebook Agent API ==========

# Agent 对话历史存储 (内存中)
_agent_histories: Dict[str, Dict[str, Any]] = {}


class AgentChatRequest(BaseModel):
    """Agent 对话请求"""
    message: str
    include_context: bool = True
    include_variables: bool = False
    user_authorized: bool = False  # 用户是否授权 Agent 操作 Notebook
    stream: bool = True


class AgentCodeBlock(BaseModel):
    """代码块"""
    id: str
    language: str
    code: str


class AgentMessage(BaseModel):
    """Agent 消息"""
    id: str
    role: str  # 'user', 'assistant', 'system'
    content: str
    code_blocks: List[AgentCodeBlock] = []
    timestamp: str
    metadata: Dict[str, Any] = {}


def get_agent_history(notebook_id: str, user_id: int) -> Dict[str, Any]:
    """获取 Agent 对话历史"""
    key = f"{user_id}:{notebook_id}"
    if key not in _agent_histories:
        _agent_histories[key] = {
            "notebook_id": notebook_id,
            "messages": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
    return _agent_histories[key]


def save_agent_message(notebook_id: str, user_id: int, message: AgentMessage):
    """保存 Agent 消息"""
    history = get_agent_history(notebook_id, user_id)
    history["messages"].append(message.model_dump())
    history["updated_at"] = datetime.now().isoformat()


@router.get("/notebooks/{notebook_id}/agent/context")
async def get_agent_context(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取 Notebook 上下文供 Agent 使用"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    kernel = kernel_manager.get_kernel(notebook_id)
    variables = kernel.get_variables() if kernel else {}
    
    # 获取最近的输出（使用配置的 Cell 数量）
    recent_outputs = []
    for cell in notebook.get("cells", [])[-settings.notebook_context_output_cells:]:
        if cell.get("outputs"):
            recent_outputs.append({
                "cell_id": cell["id"],
                "execution_count": cell.get("execution_count"),
                "outputs": cell["outputs"][:2]  # 每个 cell 最多 2 个输出
            })
    
    # 生成代码摘要
    code_cells = [c for c in notebook.get("cells", []) if c.get("cell_type") == "code"]
    code_summary = f"共 {len(code_cells)} 个代码单元格"
    if code_cells:
        # 统计导入的库
        imports = set()
        for cell in code_cells:
            source = cell.get("source", "")
            for line in source.split("\n"):
                line = line.strip()
                if line.startswith("import ") or line.startswith("from "):
                    imports.add(line.split()[1].split(".")[0])
        if imports:
            code_summary += f"，使用了 {', '.join(sorted(imports)[:5])} 等库"
    
    return {
        "notebook_id": notebook_id,
        "notebook_title": notebook.get("title", "未命名"),
        "cell_count": len(notebook.get("cells", [])),
        "execution_count": notebook.get("execution_count", 0),
        "variables": variables,
        "recent_outputs": recent_outputs,
        "code_summary": code_summary
    }


@router.get("/notebooks/{notebook_id}/agent/history")
async def get_agent_history_endpoint(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取 Agent 对话历史"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    return get_agent_history(notebook_id, current_user.id)


@router.delete("/notebooks/{notebook_id}/agent/history")
async def clear_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """清空 Agent 对话历史"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    key = f"{current_user.id}:{notebook_id}"
    if key in _agent_histories:
        del _agent_histories[key]
    
    return {"message": "对话历史已清空"}


@router.post("/notebooks/{notebook_id}/agent/chat")
async def notebook_agent_chat(
    notebook_id: str,
    request: AgentChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Notebook AI Agent 对话端点
    
    支持流式响应，Agent 可以：
    - 执行代码 (需要用户授权)
    - 查看变量
    - 操作单元格 (需要用户授权)
    - 安装包 (需要用户授权)
    - 爬取网页
    - 搜索文献
    - 分析代码
    """
    from fastapi.responses import StreamingResponse
    
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 保存用户消息
    user_message = AgentMessage(
        id=str(uuid.uuid4()),
        role="user",
        content=request.message,
        code_blocks=[],
        timestamp=datetime.now().isoformat(),
        metadata={}
    )
    save_agent_message(notebook_id, current_user.id, user_message)
    
    async def generate_response():
        """生成流式响应"""
        try:
            from app.services.agent_tools import ToolRegistry
            from app.services.react_agent import ReActAgent
            from app.services.llm_service import get_llm_service
            
            # 创建带 Notebook 上下文的工具注册表
            tool_registry = ToolRegistry(
                db=None,
                user_id=current_user.id,
                notebook_id=notebook_id,
                kernel_manager=kernel_manager,
                notebooks_store=_notebooks,
                user_authorized=request.user_authorized
            )
            
            # 获取 LLM 服务 (异步)
            llm_service = await get_llm_service()
            
            # 创建 Agent
            agent = ReActAgent(
                llm_service=llm_service,
                tool_registry=tool_registry,
                max_iterations=settings.react_max_iterations
            )
            
            # 构建 Notebook 单元格概要（使用配置的上下文参数）
            cells = notebook.get('cells', [])
            code_cells = [c for c in cells if c.get('cell_type') == 'code']
            cell_summary_parts = []
            max_cell_length = settings.notebook_context_cell_max_length
            for i, cell in enumerate(code_cells[-settings.notebook_context_cells:]):
                source = cell.get('source', '')[:max_cell_length]
                has_output = bool(cell.get('outputs'))
                exec_count = cell.get('execution_count')
                cell_summary_parts.append(
                    f"[Cell {exec_count or '?'}] {source}{'...' if len(cell.get('source', '')) > max_cell_length else ''}"
                    f"{' (有输出)' if has_output else ''}"
                )
            cells_summary = "\n".join(cell_summary_parts) if cell_summary_parts else "（无代码单元格）"
            
            # 获取当前变量状态（使用配置的变量数量限制）
            kernel = kernel_manager.get_kernel(notebook_id)
            variables_info = ""
            if kernel:
                variables = kernel.get_variables()
                if variables:
                    var_items = list(variables.items())[:settings.notebook_context_variables]
                    variables_info = "\n当前变量：\n" + "\n".join([f"- {k}: {v}" for k, v in var_items])
            
            # 构建系统消息，包含完整 Notebook 上下文
            system_context = f"""你是一个专业的数据科学助手，正在帮助用户使用代码实验室 (CodeLab)。

## 当前 Notebook 信息
- ID: {notebook_id}
- 标题: {notebook.get('title', '未命名')}
- 单元格数量: {len(cells)} (代码: {len(code_cells)})
- 执行次数: {notebook.get('execution_count', 0)}

## 最近的代码单元格
{cells_summary}
{variables_info}

## 用户授权状态: {'✅ 已授权' if request.user_authorized else '❌ 未授权'}
{'- 你可以直接执行代码、安装包、操作单元格' if request.user_authorized else '- 你只能提供代码建议，不能直接执行。如需执行，请提示用户开启「允许 AI 操作」'}

## 可用工具
- notebook_execute: 在 Notebook 内核中执行 Python 代码 {'(可用)' if request.user_authorized else '(需授权)'}
- notebook_variables: 查看当前变量状态 (可用)
- notebook_cell: 操作单元格 {'(可用)' if request.user_authorized else '(需授权)'}
- pip_install: 安装 Python 包 {'(可用)' if request.user_authorized else '(需授权)'}
- web_scrape: 爬取网页内容 (可用)
- code_analysis: 分析代码质量和性能 (可用)
- literature_search: 搜索学术论文 (可用)
- web_search: 网络搜索 (可用)
- calculator: 数学计算 (可用)

请根据用户需求和 Notebook 上下文选择合适的工具完成任务。"""
            
            # 发送开始事件
            yield f"data: {json.dumps({'type': 'start', 'provider': llm_service.provider, 'model': llm_service.config['model']})}\n\n"
            
            # 收集完整响应
            full_content = ""
            code_blocks = []
            
            # 调用 Agent - 注意: messages 构建已包含 system context
            messages = [
                {"role": "system", "content": system_context}
            ]
            
            # 获取对话历史
            history = get_agent_history(notebook_id, current_user.id)
            # 添加最近的对话历史 (最多 10 条)
            for msg in history.get("messages", [])[-10:-1]:  # 不包括刚添加的用户消息
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
            messages.append({"role": "user", "content": request.message})
            
            # 使用 agent.run() 方法，它是一个 async generator
            async for event in agent.run(messages, stream=True):
                event_type = event.get("type", "")
                event_data = event.get("data", "")
                
                if event_type == "thought":
                    # data 是思考内容字符串
                    yield f"data: {json.dumps({'type': 'thought', 'content': event_data})}\n\n"
                
                elif event_type == "thinking":
                    # 流式思考内容
                    yield f"data: {json.dumps({'type': 'content', 'content': event_data})}\n\n"
                
                elif event_type == "action":
                    # data 是字典 {"tool": "...", "input": {...}}
                    tool_name = event_data.get("tool", "") if isinstance(event_data, dict) else ""
                    tool_input = event_data.get("input", {}) if isinstance(event_data, dict) else {}
                    yield f"data: {json.dumps({'type': 'action', 'tool': tool_name, 'input': tool_input})}\n\n"
                
                elif event_type == "observation":
                    # data 是字典 {"tool": "...", "success": ..., "output": ..., "data": ...}
                    success = event_data.get("success", False) if isinstance(event_data, dict) else False
                    max_output_len = settings.react_output_max_length
                    output = event_data.get("output", "")[:max_output_len] if isinstance(event_data, dict) else str(event_data)[:max_output_len]
                    tool_data = event_data.get("data", {}) if isinstance(event_data, dict) else {}
                    
                    # 检查是否有 notebook 更新
                    notebook_updated = tool_data.get("notebook_updated", False) if isinstance(tool_data, dict) else False
                    cell_id = tool_data.get("cell_id") if isinstance(tool_data, dict) else None
                    
                    # 获取 cell 数据（新增或更新）
                    new_cell = tool_data.get("new_cell") if isinstance(tool_data, dict) else None
                    updated_cell = tool_data.get("updated_cell") if isinstance(tool_data, dict) else None
                    
                    # 如果没有直接返回 new_cell，从缓存中查找
                    if notebook_updated and cell_id and not new_cell and notebook_id in _notebooks_cache:
                        nb = _notebooks_cache[notebook_id]
                        for cell in nb.get('cells', []):
                            if cell.get('id') == cell_id:
                                new_cell = cell
                                break
                    
                    # 同步缓存到数据库（直接 await）
                    if notebook_updated and notebook_id in _notebooks_cache:
                        try:
                            from app.core.database import AsyncSessionLocal
                            async with AsyncSessionLocal() as db_session:
                                service = NotebookService(db_session)
                                nb = _notebooks_cache[notebook_id]
                                user_id = nb.get('user_id')
                                
                                if new_cell:
                                    # 检查 cell 是否已存在
                                    existing = await service.get_notebook(notebook_id, user_id)
                                    if existing:
                                        cell_exists = any(c.get('id') == new_cell.get('id') for c in existing.get('cells', []))
                                        if not cell_exists:
                                            # 新增 cell - 使用特定的 cell_id
                                            from app.models.notebook import NotebookCell
                                            notebook_model = await service.get_notebook_model(notebook_id, user_id)
                                            if notebook_model:
                                                new_db_cell = NotebookCell(
                                                    id=new_cell.get('id'),
                                                    notebook_id=notebook_id,
                                                    cell_type=new_cell.get('cell_type', 'code'),
                                                    source=new_cell.get('source', ''),
                                                    outputs=new_cell.get('outputs', []),
                                                    execution_count=new_cell.get('execution_count'),
                                                    cell_metadata=new_cell.get('metadata', {}),
                                                    position=len(notebook_model.cells),
                                                )
                                                notebook_model.cells.append(new_db_cell)
                                                await db_session.commit()
                                                logger.info(f"[Agent] 新 Cell 已同步到数据库: {new_cell.get('id')}")
                                
                                elif updated_cell:
                                    # 更新 cell
                                    await service.update_cell(
                                        notebook_id, user_id,
                                        updated_cell.get('id'),
                                        source=updated_cell.get('source'),
                                        cell_type=updated_cell.get('cell_type'),
                                        outputs=updated_cell.get('outputs'),
                                        execution_count=updated_cell.get('execution_count')
                                    )
                                    logger.info(f"[Agent] Cell 更新已同步到数据库: {updated_cell.get('id')}")
                                    
                        except Exception as e:
                            logger.warning(f"同步到数据库失败: {e}")
                    
                    yield f"data: {json.dumps({'type': 'observation', 'success': success, 'output': output, 'notebook_updated': notebook_updated, 'cell_id': cell_id, 'new_cell': new_cell, 'updated_cell': updated_cell})}\n\n"
                
                elif event_type == "authorization_required":
                    yield f"data: {json.dumps({'type': 'authorization_required', 'action': event_data.get('action', '') if isinstance(event_data, dict) else ''})}\n\n"
                
                elif event_type == "answer":
                    # data 是答案内容字符串
                    full_content = event_data if isinstance(event_data, str) else str(event_data)
                    yield f"data: {json.dumps({'type': 'answer', 'content': full_content})}\n\n"
                
                elif event_type == "start":
                    # 开始事件，data 是字典 {"provider": "...", "model": "..."}
                    provider = event_data.get("provider", "") if isinstance(event_data, dict) else ""
                    model = event_data.get("model", "") if isinstance(event_data, dict) else ""
                    yield f"data: {json.dumps({'type': 'start', 'provider': provider, 'model': model})}\n\n"
                
                elif event_type == "done":
                    # 完成事件，data 包含迭代信息
                    if isinstance(event_data, dict) and event_data.get("answer"):
                        full_content = event_data.get("answer", full_content)
                
                elif event_type == "error":
                    error_msg = event_data if isinstance(event_data, str) else str(event_data)
                    yield f"data: {json.dumps({'type': 'error', 'error': error_msg})}\n\n"
            
            # 提取代码块
            import re
            code_pattern = r'```(\w+)?\n(.*?)```'
            matches = re.findall(code_pattern, full_content, re.DOTALL)
            for i, (lang, code) in enumerate(matches):
                code_blocks.append({
                    "id": f"code_{i}",
                    "language": lang or "python",
                    "code": code.strip()
                })
            
            # 保存助手消息
            assistant_message = AgentMessage(
                id=str(uuid.uuid4()),
                role="assistant",
                content=full_content,
                code_blocks=[AgentCodeBlock(**cb) for cb in code_blocks],
                timestamp=datetime.now().isoformat(),
                metadata={}
            )
            save_agent_message(notebook_id, current_user.id, assistant_message)
            
            # 发送完成事件
            yield f"data: {json.dumps({'type': 'done', 'code_blocks': code_blocks})}\n\n"
            
        except Exception as e:
            logger.error(f"Agent 对话错误: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    
    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/notebooks/{notebook_id}/agent/suggest-code")
async def suggest_code(
    notebook_id: str,
    description: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """根据描述生成代码建议"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    try:
        from app.services.llm_service import get_llm_service
        
        llm_service = await get_llm_service()
        
        # 获取变量信息
        kernel = kernel_manager.get_kernel(notebook_id)
        variables_info = ""
        if kernel:
            variables = kernel.get_variables()
            if variables:
                variables_info = "\n当前可用变量:\n" + "\n".join([f"- {k}: {v}" for k, v in list(variables.items())[:10]])
        
        prompt = f"""请根据以下描述生成 Python 代码：

描述: {description}
{variables_info}

要求：
1. 代码应该简洁、可读
2. 添加必要的注释
3. 如果需要导入库，请包含 import 语句
4. 只输出代码，不要其他解释

```python
"""
        
        messages = [{"role": "user", "content": prompt}]
        response = await llm_service.chat(messages)
        
        # 提取代码
        content = response.get("content", "")
        # 尝试提取代码块
        import re
        code_match = re.search(r'```python\n(.*?)```', content, re.DOTALL)
        if code_match:
            code = code_match.group(1).strip()
        else:
            code = content.strip()
        
        return {
            "description": description,
            "code": code,
            "full_response": content
        }
    except Exception as e:
        logger.error(f"生成代码建议失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/notebooks/{notebook_id}/agent/explain-error")
async def explain_error(
    notebook_id: str,
    error_message: str,
    code: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """解释代码错误并提供修复建议"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    try:
        from app.services.llm_service import get_llm_service
        
        llm_service = await get_llm_service()
        
        prompt = f"""请分析以下 Python 错误并提供修复建议：

错误信息:
{error_message}
"""
        if code:
            prompt += f"""
相关代码:
```python
{code}
```
"""
        prompt += """
请提供：
1. 错误原因的简明解释
2. 修复建议
3. 如果可能，提供修复后的代码
"""
        
        messages = [{"role": "user", "content": prompt}]
        response = await llm_service.chat(messages)
        
        content = response.get("content", "")
        
        # 尝试提取修复代码
        import re
        code_match = re.search(r'```python\n(.*?)```', content, re.DOTALL)
        fix_code = code_match.group(1).strip() if code_match else None
        
        return {
            "explanation": content,
            "fix_code": fix_code
        }
    except Exception as e:
        logger.error(f"解释错误失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/notebooks/{notebook_id}/agent/analyze-data")
async def analyze_data(
    notebook_id: str,
    variable_name: str,
    analysis_type: str = "overview",  # 'overview', 'statistics', 'distribution', 'correlation'
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """分析数据变量"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    try:
        from app.services.llm_service import get_llm_service
        
        llm_service = await get_llm_service()
        
        # 获取变量信息
        kernel = kernel_manager.get_kernel(notebook_id)
        if not kernel:
            raise HTTPException(status_code=400, detail="内核未启动")
        
        variables = kernel.get_variables()
        if variable_name not in variables:
            raise HTTPException(status_code=404, detail=f"变量 '{variable_name}' 不存在")
        
        var_info = variables[variable_name]
        
        analysis_prompts = {
            "overview": f"请为变量 {variable_name} ({var_info}) 生成数据概览代码，包括形状、类型、前几行数据等",
            "statistics": f"请为变量 {variable_name} ({var_info}) 生成统计分析代码，包括均值、中位数、标准差等",
            "distribution": f"请为变量 {variable_name} ({var_info}) 生成数据分布可视化代码，使用直方图或密度图",
            "correlation": f"请为变量 {variable_name} ({var_info}) 生成相关性分析代码，包括热力图可视化"
        }
        
        prompt = analysis_prompts.get(analysis_type, analysis_prompts["overview"])
        prompt += "\n\n只输出可直接执行的 Python 代码，使用 matplotlib 或 seaborn 进行可视化。"
        
        messages = [{"role": "user", "content": prompt}]
        response = await llm_service.chat(messages)
        
        content = response.get("content", "")
        
        # 提取代码
        import re
        code_match = re.search(r'```python\n(.*?)```', content, re.DOTALL)
        suggested_code = code_match.group(1).strip() if code_match else content.strip()
        
        return {
            "variable_name": variable_name,
            "analysis_type": analysis_type,
            "suggested_code": suggested_code,
            "description": content
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"数据分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
