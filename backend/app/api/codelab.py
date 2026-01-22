"""
代码实验室 API - Jupyter-style Notebook 后端
支持代码单元执行、Notebook 管理、输出处理

核心改进：实现持久化的执行内核，让 cell 之间共享执行上下文
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
    timeout: int = 30  # 执行超时（秒）

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
        self._kernel_timeout = 7200  # 2小时不活跃则销毁内核
        
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


# ========== 内存存储（生产环境应使用数据库）==========

# 简单的内存存储，用于演示
_notebooks: Dict[str, Dict] = {}

def get_user_notebooks(user_id: int) -> List[Dict]:
    """获取用户的所有 Notebook"""
    return [nb for nb in _notebooks.values() if nb.get('user_id') == user_id]

def get_notebook(notebook_id: str, user_id: int) -> Optional[Dict]:
    """获取单个 Notebook"""
    nb = _notebooks.get(notebook_id)
    if nb and nb.get('user_id') == user_id:
        return nb
    return None


# ========== API 端点 ==========

@router.get("/notebooks", response_model=List[NotebookResponse])
async def list_notebooks(
    current_user: User = Depends(get_current_user)
):
    """获取用户的所有 Notebook"""
    notebooks = get_user_notebooks(current_user.id)
    return sorted(notebooks, key=lambda x: x['updated_at'], reverse=True)


@router.post("/notebooks", response_model=NotebookResponse)
async def create_notebook(
    data: NotebookCreate,
    current_user: User = Depends(get_current_user)
):
    """创建新的 Notebook"""
    notebook_id = str(uuid.uuid4())
    now = datetime.utcnow()
    
    notebook = {
        'id': notebook_id,
        'user_id': current_user.id,
        'title': data.title,
        'description': data.description,
        'cells': [
            {
                'id': str(uuid.uuid4()),
                'cell_type': 'code',
                'source': '# 欢迎使用代码实验室！\n# 支持 Python、numpy、pandas、matplotlib、torch 等\n# Cell 之间共享变量，就像 Jupyter Notebook 一样\n\nimport numpy as np\nimport pandas as pd\nimport matplotlib.pyplot as plt\n\nprint("Hello, Code Lab!")\nx = 10  # 这个变量可以在后续 cell 中使用',
                'outputs': [],
                'execution_count': None,
                'metadata': {}
            }
        ],
        'created_at': now,
        'updated_at': now,
        'execution_count': 0
    }
    
    _notebooks[notebook_id] = notebook
    
    # 预创建内核
    kernel_manager.get_or_create_kernel(notebook_id)
    
    return notebook


@router.get("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def get_notebook_detail(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """获取 Notebook 详情"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    return notebook


@router.patch("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(
    notebook_id: str,
    data: NotebookUpdate,
    current_user: User = Depends(get_current_user)
):
    """更新 Notebook"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    if data.title is not None:
        notebook['title'] = data.title
    if data.description is not None:
        notebook['description'] = data.description
    if data.cells is not None:
        notebook['cells'] = [cell.dict() for cell in data.cells]
    
    notebook['updated_at'] = datetime.utcnow()
    _notebooks[notebook_id] = notebook
    
    return notebook


@router.delete("/notebooks/{notebook_id}")
async def delete_notebook(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """删除 Notebook"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    del _notebooks[notebook_id]
    
    # 销毁对应的内核
    kernel_manager.destroy_kernel(notebook_id)
    
    return {"message": "Notebook 已删除"}


@router.post("/notebooks/{notebook_id}/execute", response_model=ExecuteResponse)
async def execute_cell(
    notebook_id: str,
    request: ExecuteRequest,
    current_user: User = Depends(get_current_user)
):
    """
    执行代码单元格
    使用持久化的执行内核，cell 之间共享变量
    """
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取或创建执行内核
    kernel = kernel_manager.get_or_create_kernel(notebook_id)
    
    # 在内核中执行代码
    result = kernel.execute(request.code, request.timeout)
    
    # 更新 Notebook 中的单元格输出
    if request.cell_id:
        for cell in notebook['cells']:
            if cell['id'] == request.cell_id:
                cell['outputs'] = [o.dict() if isinstance(o, CellOutput) else o for o in result['outputs']]
                cell['execution_count'] = result['execution_count']
                break
    
    notebook['updated_at'] = datetime.utcnow()
    notebook['execution_count'] = result['execution_count']
    _notebooks[notebook_id] = notebook
    
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
    result = temp_kernel.execute(request.code, request.timeout)
    
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
    current_user: User = Depends(get_current_user)
):
    """添加新单元格"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    new_cell = {
        'id': str(uuid.uuid4()),
        'cell_type': cell_type,
        'source': '',
        'outputs': [],
        'execution_count': None,
        'metadata': {}
    }
    
    if index is not None and 0 <= index <= len(notebook['cells']):
        notebook['cells'].insert(index, new_cell)
    else:
        notebook['cells'].append(new_cell)
    
    notebook['updated_at'] = datetime.utcnow()
    _notebooks[notebook_id] = notebook
    
    return new_cell


@router.delete("/notebooks/{notebook_id}/cells/{cell_id}")
async def delete_cell(
    notebook_id: str,
    cell_id: str,
    current_user: User = Depends(get_current_user)
):
    """删除单元格"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    notebook['cells'] = [c for c in notebook['cells'] if c['id'] != cell_id]
    notebook['updated_at'] = datetime.utcnow()
    _notebooks[notebook_id] = notebook
    
    return {"message": "单元格已删除"}


@router.post("/notebooks/{notebook_id}/run-all")
async def run_all_cells(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """运行所有代码单元格"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 获取执行内核
    kernel = kernel_manager.get_or_create_kernel(notebook_id)
    
    results = []
    for cell in notebook['cells']:
        if cell['cell_type'] == 'code' and cell['source'].strip():
            # 执行代码
            result = kernel.execute(cell['source'], timeout=30)
            cell['outputs'] = [o.dict() if isinstance(o, CellOutput) else o for o in result['outputs']]
            cell['execution_count'] = result['execution_count']
            
            results.append({
                'cell_id': cell['id'],
                'success': result['success'],
                'execution_count': result['execution_count']
            })
    
    notebook['updated_at'] = datetime.utcnow()
    notebook['execution_count'] = kernel.execution_count
    _notebooks[notebook_id] = notebook
    
    return {
        'message': f'已执行 {len(results)} 个单元格',
        'results': results
    }


@router.post("/notebooks/{notebook_id}/restart-kernel")
async def restart_kernel(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """重启内核（清除所有变量状态）"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 重置内核
    kernel_manager.reset_kernel(notebook_id)
    
    # 清除所有 cell 的输出和执行计数
    for cell in notebook['cells']:
        cell['outputs'] = []
        cell['execution_count'] = None
    
    notebook['execution_count'] = 0
    notebook['updated_at'] = datetime.utcnow()
    _notebooks[notebook_id] = notebook
    
    return {"message": "内核已重启，所有变量已清除"}


@router.get("/notebooks/{notebook_id}/kernel-status")
async def get_kernel_status(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """获取内核状态"""
    notebook = get_notebook(notebook_id, current_user.id)
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
    current_user: User = Depends(get_current_user)
):
    """中断内核执行（当前实现中，由于使用同步执行，此功能有限）"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 注意：当前实现使用同步执行，无法真正中断
    # 未来可以考虑使用多进程来实现真正的中断功能
    return {"message": "中断请求已发送"}
