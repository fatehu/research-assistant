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
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from loguru import logger
from contextlib import redirect_stdout, redirect_stderr

from app.core.database import get_db, async_session_factory
from app.core.security import get_current_user
from app.models.user import User
from app.services.notebook_service import NotebookService
from app.services.notebook_agent_history_service import (
    append_history_message,
    clear_history as clear_history_in_db,
    load_history,
)
from app.services.agent_runtime_service import get_agent_runtime_service
from app.services.codelab_executor import CodeLabExecutor, RunnerUnavailableError
from app.services.notebook_workspace_service import (
    build_notebook_workspace_context,
    delete_notebook_workspace,
    delete_notebook_workspace_file,
    ensure_notebook_workspace,
    list_notebook_workspace_files,
    save_notebook_workspace_upload,
)
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
    terminated_reason: str = "none"  # timeout | policy_violation | resource_limit | none
    policy_violation_code: Optional[str] = None


class NotebookWorkspaceFileResponse(BaseModel):
    name: str
    relative_path: str
    runtime_path: str
    size_bytes: int
    content_type: Optional[str] = None
    updated_at: str
    extension: str


class NotebookWorkspaceResponse(BaseModel):
    notebook_id: str
    workspace_dir: str
    display_path: str
    file_count: int
    files: List[NotebookWorkspaceFileResponse]


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
        self._sandbox_enabled = bool(settings.codelab_sandbox_enabled)
        self._sandbox_executor: Optional[CodeLabExecutor] = None
        self._variable_previews: Dict[str, str] = {}
        
        # 共享的命名空间 - 所有 cell 在这里执行
        self.namespace: Dict[str, Any] = {}

        if self._sandbox_enabled:
            self._sandbox_executor = CodeLabExecutor(
                notebook_id=notebook_id,
                hard_timeout_seconds=settings.codelab_exec_timeout_hard_seconds,
            )
        else:
            # 初始化命名空间，预导入常用库
            self._initialize_namespace()
        
        logger.info(f"创建执行内核: notebook_id={notebook_id}, sandbox={self._sandbox_enabled}")
    
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

    def _apply_workspace_context(self, workspace_context: Optional[Dict[str, Any]] = None) -> None:
        workspace = workspace_context if isinstance(workspace_context, dict) else {}
        workspace_dir = str(workspace.get("directory") or "").strip()
        file_names = [str(item) for item in list(workspace.get("file_names") or []) if str(item or "").strip()]
        file_paths = {
            str(key): str(value)
            for key, value in dict(workspace.get("file_paths") or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }

        def _resolve_uploaded_file(name: str) -> str:
            raw_name = str(name or "").strip()
            if not raw_name:
                raise FileNotFoundError("文件名不能为空")
            explicit_path = file_paths.get(raw_name)
            if explicit_path and os.path.isfile(explicit_path):
                return explicit_path
            if workspace_dir:
                normalized = os.path.abspath(os.path.join(workspace_dir, raw_name))
                workspace_root = os.path.abspath(workspace_dir)
                if normalized.startswith(workspace_root) and os.path.isfile(normalized):
                    return normalized
            raise FileNotFoundError(f"找不到上传文件: {raw_name}")

        def list_uploaded_files():
            return list(file_names)

        def uploaded_file_path(name: str) -> str:
            return _resolve_uploaded_file(name)

        def read_uploaded_text(name: str, encoding: str = "utf-8") -> str:
            file_path = _resolve_uploaded_file(name)
            with open(file_path, "r", encoding=encoding) as handle:
                return handle.read()

        self.namespace["NOTEBOOK_FILES_DIR"] = workspace_dir
        self.namespace["NOTEBOOK_FILES"] = list(file_names)
        self.namespace["NOTEBOOK_FILE_PATHS"] = dict(file_paths)
        self.namespace["list_uploaded_files"] = list_uploaded_files
        self.namespace["uploaded_file_path"] = uploaded_file_path
        self.namespace["read_uploaded_text"] = read_uploaded_text
        
    def execute(
        self,
        code: str,
        timeout: int = 30,
        workspace_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        在持久化的命名空间中执行代码
        返回执行结果，包括输出、图表、错误等
        """
        if self._sandbox_executor is not None:
            self.last_used_at = datetime.utcnow()
            hard_timeout = max(1, int(settings.codelab_exec_timeout_hard_seconds))
            safe_timeout = max(1, min(int(timeout or 1), hard_timeout))
            result = self._sandbox_executor.execute(
                code=code,
                timeout_seconds=safe_timeout,
                workspace_context=workspace_context,
            )
            self.execution_count = int(result.get('execution_count', self.execution_count) or 0)
            self.namespace = {}
            self._variable_previews = dict(result.get("variable_previews", {}) or {})
            return result

        self._apply_workspace_context(workspace_context)
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
    
    def reset(self, workspace_context: Optional[Dict[str, Any]] = None):
        """重置内核状态"""
        if self._sandbox_executor is not None:
            self._sandbox_executor.reset(workspace_context=workspace_context)
            self.execution_count = 0
            self.namespace.clear()
            self._variable_previews = {}
            logger.info(f"沙箱内核已重置: notebook_id={self.notebook_id}")
            return

        self.namespace.clear()
        self.execution_count = 0
        self._initialize_namespace()
        self._apply_workspace_context(workspace_context)
        logger.info(f"内核已重置: notebook_id={self.notebook_id}")
    
    def get_variables(self) -> Dict[str, str]:
        """获取当前命名空间中的变量列表（用于调试/显示）"""
        if self._sandbox_executor is not None:
            variables = self._sandbox_executor.get_variables()
            return variables

        variables = {}
        for name, value in self.namespace.items():
            if not name.startswith('_') and not callable(value) and not isinstance(value, type):
                try:
                    variables[name] = type(value).__name__
                except:
                    pass
        return variables

    def get_variable_preview(self, name: str) -> Optional[str]:
        if not name:
            return None
        if self._sandbox_executor is not None:
            return self._sandbox_executor.get_variable_preview(name)
        value = self.namespace.get(name)
        if value is None:
            return None
        try:
            if hasattr(value, "shape"):
                return f"shape={getattr(value, 'shape', None)}"
            if hasattr(value, "__len__") and not isinstance(value, str):
                return f"len={len(value)}"
            text = repr(value)
            return text[:160] + ("..." if len(text) > 160 else "")
        except Exception:
            return None

    def has_variable(self, name: str) -> bool:
        if not name:
            return False
        if self._sandbox_executor is not None:
            return self._sandbox_executor.has_variable(name)
        return name in self.namespace

    def close(self) -> None:
        if self._sandbox_executor is not None:
            self._sandbox_executor.close()


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
    
    def reset_kernel(self, notebook_id: str, workspace_context: Optional[Dict[str, Any]] = None) -> PythonKernel:
        """重置 Notebook 的执行内核"""
        with self._lock:
            if notebook_id in self._kernels:
                self._kernels[notebook_id].reset(workspace_context=workspace_context)
            else:
                self._kernels[notebook_id] = PythonKernel(notebook_id)
                self._kernels[notebook_id].reset(workspace_context=workspace_context)
            return self._kernels[notebook_id]
    
    def destroy_kernel(self, notebook_id: str):
        """销毁 Notebook 的执行内核"""
        with self._lock:
            if notebook_id in self._kernels:
                try:
                    self._kernels[notebook_id].close()
                except Exception:
                    logger.warning(f"关闭内核失败: notebook_id={notebook_id}")
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
                try:
                    self._kernels[notebook_id].close()
                except Exception:
                    logger.warning(f"关闭不活跃内核失败: notebook_id={notebook_id}")
                del self._kernels[notebook_id]
                logger.info(f"清理不活跃内核: notebook_id={notebook_id}")


# 全局内核管理器实例
kernel_manager = KernelManager()


# ========== 内存缓存 + 数据库持久化 ==========

# 内存缓存：用于快速访问和 Agent 工具的实时交互
_notebooks_cache: Dict[str, Dict] = {}

# 标记已从数据库加载的用户
_loaded_users: set = set()
_loaded_users_at: Dict[int, float] = {}
_cache_lock = asyncio.Lock()
_CACHE_TTL_SECONDS = max(60, int(getattr(settings, "codelab_cache_ttl_seconds", 300)))

# 用户维度执行并发计数（避免单用户占满服务）
_user_execution_counter: Dict[int, int] = {}
_user_execution_lock = asyncio.Lock()


class _ResourceLimitError(Exception):
    pass


class _UserExecutionSlot:
    def __init__(self, user_id: int):
        self.user_id = int(user_id)

    async def __aenter__(self):
        limit = max(1, int(settings.codelab_max_concurrency_per_user))
        async with _user_execution_lock:
            current = _user_execution_counter.get(self.user_id, 0)
            if current >= limit:
                raise _ResourceLimitError("CodeLab 并发执行已达上限，请稍后重试")
            _user_execution_counter[self.user_id] = current + 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with _user_execution_lock:
            current = max(0, _user_execution_counter.get(self.user_id, 1) - 1)
            if current == 0:
                _user_execution_counter.pop(self.user_id, None)
            else:
                _user_execution_counter[self.user_id] = current
        return False


async def _sync_to_cache(notebook: Dict):
    """同步 Notebook 到缓存"""
    notebook_id = str(notebook.get("id"))
    if not notebook_id:
        return
    async with _cache_lock:
        _notebooks_cache[notebook_id] = notebook
        user_id = notebook.get("user_id")
        if user_id is not None:
            _loaded_users.add(int(user_id))
            _loaded_users_at[int(user_id)] = time.time()


async def _remove_from_cache(notebook_id: str):
    if not notebook_id:
        return
    async with _cache_lock:
        _notebooks_cache.pop(str(notebook_id), None)


async def _list_user_notebooks_from_cache(user_id: int) -> List[Dict]:
    async with _cache_lock:
        return [nb for nb in _notebooks_cache.values() if nb.get("user_id") == user_id]


async def _get_notebook_from_cache(notebook_id: str, user_id: int) -> Optional[Dict]:
    async with _cache_lock:
        nb = _notebooks_cache.get(str(notebook_id))
        if nb and nb.get("user_id") == user_id:
            return nb
    return None


async def _load_user_notebooks_to_cache(db: AsyncSession, user_id: int):
    """从数据库加载用户的 Notebooks 到缓存"""
    now = time.time()
    loaded_at = _loaded_users_at.get(int(user_id))
    if user_id in _loaded_users and loaded_at is not None and (now - loaded_at) < _CACHE_TTL_SECONDS:
        return

    service = NotebookService(db)
    notebooks = await service.get_user_notebooks(user_id)
    async with _cache_lock:
        for nb in notebooks:
            nb_id = str(nb.get("id"))
            if not nb_id:
                continue
            _notebooks_cache[nb_id] = nb
        _loaded_users.add(int(user_id))
        _loaded_users_at[int(user_id)] = now
    logger.info(f"已加载用户 {user_id} 的 {len(notebooks)} 个 Notebook 到缓存")


async def get_user_notebooks_cached(db: AsyncSession, user_id: int) -> List[Dict]:
    """获取用户的所有 Notebook（带缓存）"""
    await _load_user_notebooks_to_cache(db, user_id)
    return await _list_user_notebooks_from_cache(user_id)


async def get_notebook_cached(db: AsyncSession, notebook_id: str, user_id: int) -> Optional[Dict]:
    """获取单个 Notebook（带缓存）"""
    # 先查缓存
    nb = await _get_notebook_from_cache(notebook_id, user_id)
    if nb:
        return nb

    # 缓存未命中，从数据库加载
    service = NotebookService(db)
    nb = await service.get_notebook(notebook_id, user_id)
    if nb:
        await _sync_to_cache(nb)
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


def _workspace_context_for_notebook(notebook: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    notebook_id = str(notebook.get("id") or "").strip()
    user_id = notebook.get("user_id")
    if not notebook_id or user_id is None:
        return None
    return build_notebook_workspace_context(notebook_id, int(user_id))


# ========== API 端点 ==========

@router.get("/notebooks", response_model=List[NotebookResponse])
async def list_notebooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户的所有 Notebook"""
    # 总是从数据库获取最新数据，确保列表页数据一致性
    service = NotebookService(db)
    notebooks = await service.get_user_notebooks(current_user.id)
    
    # 同步更新缓存
    for nb in notebooks:
        await _sync_to_cache(nb)
    
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
    await _sync_to_cache(notebook)

    ensure_notebook_workspace(notebook["id"], current_user.id)
    
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
    # 总是从数据库获取最新数据，确保详情页数据一致性
    service = NotebookService(db)
    notebook = await service.get_notebook(notebook_id, current_user.id)
    
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 更新缓存
    await _sync_to_cache(notebook)
    
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
    
    # 先获取当前 notebook
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 更新基本信息
    if data.title is not None or data.description is not None:
        await service.update_notebook(notebook_id, current_user.id, data.title, data.description)
    
    # 如果更新 cells，使用 sync_cells 完整同步
    if data.cells is not None:
        # 将 Cell 对象转换为字典
        cells_data = []
        for cell in data.cells:
            if hasattr(cell, 'dict'):
                cells_data.append(cell.dict())
            elif hasattr(cell, 'model_dump'):
                cells_data.append(cell.model_dump())
            elif isinstance(cell, dict):
                cells_data.append(cell)
            else:
                cells_data.append({
                    'id': getattr(cell, 'id', None),
                    'cell_type': getattr(cell, 'cell_type', 'code'),
                    'source': getattr(cell, 'source', ''),
                    'outputs': getattr(cell, 'outputs', []),
                    'execution_count': getattr(cell, 'execution_count', None),
                    'metadata': getattr(cell, 'metadata', {}),
                })
        
        notebook = await service.sync_cells(notebook_id, current_user.id, cells_data)
    else:
        # 只更新基本信息，重新获取
        notebook = await service.get_notebook(notebook_id, current_user.id)
    
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 同步到缓存
    await _sync_to_cache(notebook)
    
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
    await _remove_from_cache(notebook_id)
    
    # 销毁对应的内核
    kernel_manager.destroy_kernel(notebook_id)
    delete_notebook_workspace(notebook_id, current_user.id)
    
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
    workspace_context = _workspace_context_for_notebook(notebook)

    # 在内核中执行代码（用户维度并发限制）
    try:
        async with _UserExecutionSlot(current_user.id):
            result = await asyncio.to_thread(
                kernel.execute,
                request.code,
                request.get_timeout(),
                workspace_context,
            )
    except _ResourceLimitError as exc:
        return ExecuteResponse(
            success=False,
            outputs=[
                CellOutput(
                    output_type="error",
                    content={
                        "ename": "ResourceLimitError",
                        "evalue": str(exc),
                        "traceback": [],
                    },
                )
            ],
            execution_count=notebook.get("execution_count", 0),
            execution_time_ms=0,
            terminated_reason="resource_limit",
            policy_violation_code=None,
        )
    except RunnerUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "sandbox_runner_unavailable",
                "message": str(exc),
            },
        )
    
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
    await _sync_to_cache(notebook)
    logger.info(
        f"[CodeLabExecute] user_id={current_user.id} notebook_id={notebook_id} "
        f"success={result.get('success')} terminated_reason={result.get('terminated_reason', 'none')} "
        f"execution_time_ms={result.get('execution_time_ms', 0)}"
    )
    
    return ExecuteResponse(
        success=result['success'],
        outputs=result['outputs'],
        execution_count=result['execution_count'],
        execution_time_ms=result['execution_time_ms'],
        terminated_reason=str(result.get("terminated_reason", "none")),
        policy_violation_code=result.get("policy_violation_code"),
    )


@router.post("/execute", response_model=ExecuteResponse)
async def execute_code_directly(
    request: ExecuteRequest,
    current_user: User = Depends(get_current_user)
):
    """直接执行代码（使用临时内核，不保存状态）"""
    if not settings.codelab_direct_execute_enabled:
        raise HTTPException(
            status_code=403,
            detail="直接执行端点已关闭（CODELAB_DIRECT_EXECUTE_ENABLED=false）",
        )
    if current_user.role != "admin" and not settings.debug:
        raise HTTPException(
            status_code=403,
            detail="仅管理员或 DEBUG 模式允许直接执行",
        )

    # 创建一个临时内核
    temp_kernel = PythonKernel(f"temp_{uuid.uuid4()}")
    try:
        try:
            async with _UserExecutionSlot(current_user.id):
                result = await asyncio.to_thread(temp_kernel.execute, request.code, request.get_timeout())
        except _ResourceLimitError as exc:
            return ExecuteResponse(
                success=False,
                outputs=[
                    CellOutput(
                        output_type="error",
                        content={
                            "ename": "ResourceLimitError",
                            "evalue": str(exc),
                            "traceback": [],
                        },
                    )
                ],
                execution_count=0,
                execution_time_ms=0,
                terminated_reason="resource_limit",
                policy_violation_code=None,
            )
        except RunnerUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "sandbox_runner_unavailable",
                    "message": str(exc),
                },
            )
        logger.info(
            f"[CodeLabExecuteDirect] user_id={current_user.id} success={result.get('success')} "
            f"terminated_reason={result.get('terminated_reason', 'none')} "
            f"execution_time_ms={result.get('execution_time_ms', 0)}"
        )

        return ExecuteResponse(
            success=result['success'],
            outputs=result['outputs'],
            execution_count=0,
            execution_time_ms=result['execution_time_ms'],
            terminated_reason=str(result.get("terminated_reason", "none")),
            policy_violation_code=result.get("policy_violation_code"),
        )
    finally:
        temp_kernel.close()


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
    await _sync_to_cache(notebook)
    
    # 返回新创建的单元格
    cells = notebook['cells']
    if index is not None and 0 <= index < len(cells):
        return cells[index]
    return cells[-1]


@router.get("/notebooks/{notebook_id}/files", response_model=NotebookWorkspaceResponse)
async def list_notebook_files(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    workspace = build_notebook_workspace_context(notebook_id, current_user.id)
    return {
        "notebook_id": notebook_id,
        "workspace_dir": workspace["directory"],
        "display_path": workspace["display_path"],
        "file_count": workspace["file_count"],
        "files": list_notebook_workspace_files(notebook_id, current_user.id),
    }


@router.post("/notebooks/{notebook_id}/files/upload", response_model=NotebookWorkspaceFileResponse)
async def upload_notebook_file(
    notebook_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    try:
        return await save_notebook_workspace_upload(notebook_id, current_user.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/notebooks/{notebook_id}/files/{file_name}")
async def download_notebook_file(
    notebook_id: str,
    file_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    files = list_notebook_workspace_files(notebook_id, current_user.id)
    target = next((item for item in files if item.get("name") == file_name), None)
    if not target:
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        path=str(target["runtime_path"]),
        media_type=str(target.get("content_type") or "application/octet-stream"),
        filename=str(target["name"]),
    )


@router.delete("/notebooks/{notebook_id}/files/{file_name}")
async def delete_uploaded_notebook_file(
    notebook_id: str,
    file_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    deleted = delete_notebook_workspace_file(notebook_id, current_user.id, file_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"message": "文件已删除"}


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
    await _sync_to_cache(notebook)
    
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
    workspace_context = _workspace_context_for_notebook(notebook)
    
    results = []
    try:
        async with _UserExecutionSlot(current_user.id):
            for cell in notebook['cells']:
                if cell['cell_type'] == 'code' and cell['source'].strip():
                    # 执行代码
                    result = await asyncio.to_thread(
                        kernel.execute,
                        cell['source'],
                        settings.code_execution_timeout,
                        workspace_context,
                    )
                    
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
                        'execution_count': result['execution_count'],
                        'terminated_reason': result.get('terminated_reason', 'none'),
                        'policy_violation_code': result.get('policy_violation_code'),
                    })
    except _ResourceLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "resource_limit",
                "message": str(exc),
                "terminated_reason": "resource_limit",
            },
        )
    except RunnerUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "sandbox_runner_unavailable",
                "message": str(exc),
            },
        )
    
    notebook['updated_at'] = datetime.utcnow()
    notebook['execution_count'] = kernel.execution_count
    await _sync_to_cache(notebook)
    timeout_count = sum(1 for item in results if item.get("terminated_reason") == "timeout")
    policy_count = sum(1 for item in results if item.get("terminated_reason") == "policy_violation")
    logger.info(
        f"[CodeLabRunAll] user_id={current_user.id} notebook_id={notebook_id} "
        f"executed_cells={len(results)} timeout_cells={timeout_count} policy_cells={policy_count}"
    )
    
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
    workspace_context = _workspace_context_for_notebook(notebook)
    kernel_manager.reset_kernel(notebook_id, workspace_context=workspace_context)
    
    # 清除所有 cell 的输出和执行计数
    service = NotebookService(db)
    for cell in notebook['cells']:
        cell['outputs'] = []
        cell['execution_count'] = None
        await service.update_cell(notebook_id, current_user.id, cell['id'], outputs=[], execution_count=None)
    
    notebook['execution_count'] = 0
    notebook['updated_at'] = datetime.utcnow()
    await _sync_to_cache(notebook)
    
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



# 兼容既有路由前缀：将 Agent 相关路由拆分到独立模块后再挂载。
from app.api import codelab_agent as codelab_agent_routes  # noqa: E402
router.include_router(codelab_agent_routes.router)

# Backward-compatible export for tests and legacy imports after route split.
notebook_agent_chat = codelab_agent_routes.notebook_agent_chat
