"""
Notebook 专用工具集 - 让 Agent 能够直接操作 Notebook

工具列表:
1. NotebookExecuteTool - 在 Notebook 内核中执行代码
2. NotebookVariablesTool - 获取当前变量状态
3. NotebookCellTool - 操作单元格 (添加/删除/更新)
4. PipInstallTool - 安装 Python 包
5. WebScrapeTool - 爬取网页内容
6. CodeAnalysisTool - 代码分析和优化建议
"""
import json
import re
import sys
import asyncio
import subprocess
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime
from loguru import logger
import httpx
from urllib.parse import urlparse

# 尝试导入 bs4，如果失败则在使用时报错
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

from app.services.agent_tools import Tool, ToolResult


# ========== 安全配置 ==========

# pip install 白名单 - 只允许安装这些包
ALLOWED_PACKAGES = {
    # 数据科学基础
    'numpy', 'pandas', 'scipy', 'statsmodels',
    # 可视化
    'matplotlib', 'seaborn', 'plotly', 'bokeh', 'altair', 'pygal',
    # 机器学习
    'scikit-learn', 'sklearn', 'xgboost', 'lightgbm', 'catboost',
    # 深度学习
    'torch', 'torchvision', 'torchaudio', 'tensorflow', 'keras',
    'transformers', 'datasets', 'accelerate',
    # NLP
    'nltk', 'spacy', 'gensim', 'jieba', 'snownlp',
    # 图像处理
    'pillow', 'opencv-python', 'opencv-python-headless', 'imageio',
    # 网络请求
    'requests', 'httpx', 'aiohttp', 'urllib3',
    # 数据解析
    'beautifulsoup4', 'bs4', 'lxml', 'html5lib', 'cssselect',
    'pyquery', 'parsel',
    # 数据格式
    'openpyxl', 'xlrd', 'xlwt', 'python-docx', 'PyPDF2', 'pdfplumber',
    'python-pptx', 'csvkit',
    # 数据库
    'sqlalchemy', 'pymysql', 'psycopg2-binary', 'redis', 'pymongo',
    # 工具库
    'tqdm', 'loguru', 'rich', 'typer', 'click',
    'pydantic', 'python-dotenv', 'python-dateutil', 'pytz',
    # 科学计算
    'sympy', 'networkx', 'igraph',
    # 其他常用
    'faker', 'arrow', 'pendulum', 'humanize',
    'tabulate', 'prettytable', 'colorama',
}

# 网页爬取黑名单域名
BLOCKED_DOMAINS = {
    'localhost', '127.0.0.1', '0.0.0.0',
    'internal', 'intranet', 'corp', 'private',
}


# ========== 工具实现 ==========

class NotebookExecuteTool(Tool):
    """
    在 Notebook 内核中执行 Python 代码
    
    这是最核心的工具，让 Agent 能够直接操控 Notebook 环境
    执行后会自动在 Notebook 中创建新的 Cell 并保存结果
    """
    name = "notebook_execute"
    description = """在 Notebook 的 Python 内核中执行代码。
代码会在持久化的命名空间中执行，变量在多次调用之间保持。
执行后会自动在 Notebook 中创建新的代码单元格并显示结果。
适用于：运行数据分析代码、创建图表、测试代码片段等。
注意：此操作需要用户授权。"""
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码"
            },
            "description": {
                "type": "string",
                "description": "代码功能描述（可选，用于日志）"
            }
        },
        "required": ["code"]
    }
    
    def __init__(self, kernel_manager, notebook_id: str, notebooks_store: dict = None, user_authorized: bool = False):
        self.kernel_manager = kernel_manager
        self.notebook_id = notebook_id
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized
    
    async def execute(self, code: str, description: str = None, **kwargs) -> ToolResult:
        """执行代码并创建 Cell"""
        import uuid
        from datetime import datetime
        
        logger.info(f"[NotebookExecute] notebook_id={self.notebook_id}, authorized={self.user_authorized}")
        logger.debug(f"[NotebookExecute] 代码: {code[:200]}...")
        
        # 检查授权
        if not self.user_authorized:
            return ToolResult(
                success=False,
                output="执行代码需要用户授权。请先启用「允许 AI 操作 Notebook」选项。",
                error="authorization_required",
                data={"requires_authorization": True, "action": "execute_code"}
            )
        
        try:
            # 获取内核
            kernel = self.kernel_manager.get_or_create_kernel(self.notebook_id)
            
            # 执行代码
            result = kernel.execute(code, timeout=60)
            
            # 将 outputs 转换为可序列化的格式
            serialized_outputs = []
            for output in result.get('outputs', []):
                if hasattr(output, 'model_dump'):
                    serialized_outputs.append(output.model_dump())
                elif hasattr(output, 'dict'):
                    serialized_outputs.append(output.dict())
                else:
                    serialized_outputs.append(output)
            
            # 创建新的 Cell 并添加到 Notebook
            new_cell_id = None
            new_cell = None
            if self.notebooks_store is not None and self.notebook_id in self.notebooks_store:
                notebook = self.notebooks_store[self.notebook_id]
                new_cell_id = str(uuid.uuid4())
                
                new_cell = {
                    'id': new_cell_id,
                    'cell_type': 'code',
                    'source': code,
                    'outputs': serialized_outputs,
                    'execution_count': result.get('execution_count'),
                    'metadata': {
                        'created_by': 'ai_agent',
                        'description': description,
                        'created_at': datetime.utcnow().isoformat()
                    }
                }
                
                # 添加到 Notebook
                if 'cells' not in notebook:
                    notebook['cells'] = []
                notebook['cells'].append(new_cell)
                notebook['updated_at'] = datetime.utcnow()
                notebook['execution_count'] = result.get('execution_count', notebook.get('execution_count', 0))
                
                logger.info(f"[NotebookExecute] 创建新 Cell: {new_cell_id}")
            
            # 格式化输出
            output_parts = []
            
            if description:
                output_parts.append(f"📝 {description}\n")
            
            for output in serialized_outputs:
                output_type = output.get('output_type', '')
                content = output.get('content', '')
                mime_type = output.get('mime_type', '')
                
                if output_type == 'stream':
                    output_parts.append(f"📤 输出:\n{content}")
                elif output_type == 'execute_result':
                    output_parts.append(f"✅ 结果:\n{content}")
                elif output_type == 'error':
                    output_parts.append(f"❌ 错误:\n{content}")
                elif output_type == 'display_data':
                    if mime_type and 'image' in mime_type:
                        output_parts.append(f"📊 [图表已生成并显示在 Notebook 中]")
                    else:
                        output_parts.append(f"📋 显示数据:\n{str(content)[:500]}")
            
            if not output_parts:
                output_parts.append("✅ 代码执行成功（无输出）")
            
            output_text = "\n".join(output_parts)
            
            return ToolResult(
                success=result.get('success', True),
                output=output_text,
                data={
                    "cell_id": new_cell_id,
                    "execution_count": result.get('execution_count'),
                    "execution_time_ms": result.get('execution_time_ms'),
                    "outputs": serialized_outputs,
                    "notebook_updated": new_cell_id is not None,
                    "new_cell": new_cell if new_cell_id else None
                }
            )
            
        except Exception as e:
            logger.error(f"[NotebookExecute] 执行失败: {e}")
            return ToolResult(
                success=False,
                output=f"代码执行失败: {str(e)}",
                error=str(e)
            )


class NotebookVariablesTool(Tool):
    """
    获取 Notebook 当前的变量状态
    
    帮助 Agent 了解当前环境中有哪些数据可用
    """
    name = "notebook_variables"
    description = """获取 Notebook 内核中当前定义的变量列表。
返回变量名、类型、简要描述等信息。
适用于：了解可用数据、检查数据状态、调试等。"""
    parameters = {
        "type": "object",
        "properties": {
            "filter_type": {
                "type": "string",
                "description": "按类型过滤，如 'DataFrame', 'ndarray', 'list' 等（可选）"
            },
            "include_values": {
                "type": "boolean",
                "description": "是否包含变量值预览，默认 True"
            }
        },
        "required": []
    }
    
    def __init__(self, kernel_manager, notebook_id: str):
        self.kernel_manager = kernel_manager
        self.notebook_id = notebook_id
    
    async def execute(self, filter_type: str = None, include_values: bool = True, **kwargs) -> ToolResult:
        """获取变量列表"""
        logger.info(f"[NotebookVariables] notebook_id={self.notebook_id}, filter={filter_type}")
        
        try:
            kernel = self.kernel_manager.get_kernel(self.notebook_id)
            
            if not kernel:
                return ToolResult(
                    success=True,
                    output="内核尚未启动，没有可用变量。",
                    data={"variables": {}}
                )
            
            # get_variables() 返回 Dict[str, str]，即 {变量名: 类型名}
            variables = kernel.get_variables()
            
            # 应用过滤
            if filter_type:
                variables = {
                    k: v for k, v in variables.items()
                    if filter_type.lower() in v.lower()
                }
            
            if not variables:
                return ToolResult(
                    success=True,
                    output=f"没有找到{'类型为 ' + filter_type + ' 的' if filter_type else ''}变量。",
                    data={"variables": {}}
                )
            
            # 格式化输出
            output_parts = ["📊 当前变量状态:\n"]
            
            for name, var_type in variables.items():
                # 类型图标
                icon = "📦"
                if 'DataFrame' in var_type:
                    icon = "📊"
                elif 'array' in var_type.lower() or 'ndarray' in var_type:
                    icon = "🔢"
                elif 'list' in var_type or 'dict' in var_type:
                    icon = "📋"
                elif 'str' in var_type:
                    icon = "📝"
                elif 'int' in var_type or 'float' in var_type:
                    icon = "🔢"
                
                line = f"{icon} {name}: {var_type}"
                
                # 如果需要值预览，获取变量值的简要描述
                if include_values:
                    try:
                        value = kernel.namespace.get(name)
                        if value is not None:
                            # 获取简要描述
                            if hasattr(value, 'shape'):
                                line += f" (shape: {value.shape})"
                            elif hasattr(value, '__len__') and not isinstance(value, str):
                                line += f" (length: {len(value)})"
                            
                            # 值预览
                            repr_str = repr(value)
                            if len(repr_str) > 100:
                                repr_str = repr_str[:100] + "..."
                            line += f"\n   预览: {repr_str}"
                    except Exception as e:
                        logger.debug(f"获取变量 {name} 预览失败: {e}")
                
                output_parts.append(line)
            
            return ToolResult(
                success=True,
                output="\n".join(output_parts),
                data={"variables": variables}
            )
            
        except Exception as e:
            logger.error(f"[NotebookVariables] 获取变量失败: {e}")
            return ToolResult(
                success=False,
                output=f"获取变量失败: {str(e)}",
                error=str(e)
            )


class NotebookCellTool(Tool):
    """
    操作 Notebook 单元格
    
    支持添加、删除、更新单元格
    """
    name = "notebook_cell"
    description = """操作 Notebook 的单元格。
支持: add (添加), delete (删除), update (更新), get (获取)。
注意：修改操作需要用户授权。"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "delete", "update", "get"],
                "description": "操作类型"
            },
            "cell_id": {
                "type": "string",
                "description": "单元格 ID（delete/update/get 需要）"
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "单元格类型（add 时使用）"
            },
            "content": {
                "type": "string",
                "description": "单元格内容（add/update 时使用）"
            },
            "index": {
                "type": "integer",
                "description": "插入位置（add 时使用，可选）"
            }
        },
        "required": ["action"]
    }
    
    def __init__(self, notebooks_store: dict, notebook_id: str, user_authorized: bool = False):
        self.notebooks_store = notebooks_store
        self.notebook_id = notebook_id
        self.user_authorized = user_authorized
    
    async def execute(
        self,
        action: str,
        cell_id: str = None,
        cell_type: str = "code",
        content: str = "",
        index: int = None,
        **kwargs
    ) -> ToolResult:
        """执行单元格操作"""
        logger.info(f"[NotebookCell] action={action}, notebook_id={self.notebook_id}")
        
        # 获取 notebook
        notebook = self.notebooks_store.get(self.notebook_id)
        if not notebook:
            return ToolResult(
                success=False,
                output="Notebook 不存在",
                error="notebook_not_found"
            )
        
        # 检查授权（get 操作不需要）
        if action in ['add', 'delete', 'update'] and not self.user_authorized:
            return ToolResult(
                success=False,
                output=f"操作 '{action}' 需要用户授权。请先启用「允许 AI 操作 Notebook」选项。",
                error="authorization_required",
                data={"requires_authorization": True, "action": f"cell_{action}"}
            )
        
        try:
            if action == "get":
                return self._get_cells(notebook)
            elif action == "add":
                return self._add_cell(notebook, cell_type, content, index)
            elif action == "delete":
                return self._delete_cell(notebook, cell_id)
            elif action == "update":
                return self._update_cell(notebook, cell_id, content, cell_type)
            else:
                return ToolResult(
                    success=False,
                    output=f"未知操作: {action}",
                    error="invalid_action"
                )
        except Exception as e:
            logger.error(f"[NotebookCell] 操作失败: {e}")
            return ToolResult(
                success=False,
                output=f"操作失败: {str(e)}",
                error=str(e)
            )
    
    def _get_cells(self, notebook: dict) -> ToolResult:
        """获取所有单元格摘要"""
        cells = notebook.get('cells', [])
        
        if not cells:
            return ToolResult(
                success=True,
                output="Notebook 没有单元格",
                data={"cells": []}
            )
        
        output_parts = [f"📓 Notebook 共有 {len(cells)} 个单元格:\n"]
        
        for i, cell in enumerate(cells):
            cell_type = cell.get('cell_type', 'code')
            source = cell.get('source', '')[:100]
            exec_count = cell.get('execution_count')
            has_output = bool(cell.get('outputs'))
            
            icon = "💻" if cell_type == "code" else "📝"
            status = f"[{exec_count}]" if exec_count else "[_]"
            output_indicator = " 📤" if has_output else ""
            
            output_parts.append(
                f"{icon} Cell {i+1} {status}{output_indicator}: {source}..."
            )
        
        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data={
                "cells": [
                    {
                        "id": c.get('id'),
                        "index": i,
                        "type": c.get('cell_type'),
                        "preview": c.get('source', '')[:100],
                        "execution_count": c.get('execution_count'),
                        "has_output": bool(c.get('outputs'))
                    }
                    for i, c in enumerate(cells)
                ]
            }
        )
    
    def _add_cell(self, notebook: dict, cell_type: str, content: str, index: int = None) -> ToolResult:
        """添加新单元格"""
        import uuid
        
        new_cell = {
            'id': str(uuid.uuid4()),
            'cell_type': cell_type,
            'source': content,
            'outputs': [],
            'execution_count': None,
            'metadata': {
                'created_by': 'ai_agent',
                'created_at': datetime.utcnow().isoformat()
            }
        }
        
        cells = notebook.get('cells', [])
        
        if index is not None and 0 <= index <= len(cells):
            cells.insert(index, new_cell)
            pos_msg = f"在位置 {index + 1}"
            actual_index = index
        else:
            cells.append(new_cell)
            pos_msg = "在末尾"
            actual_index = len(cells) - 1
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已{pos_msg}添加{'代码' if cell_type == 'code' else 'Markdown'}单元格",
            data={
                "cell_id": new_cell['id'],
                "index": actual_index,
                "notebook_updated": True,
                "new_cell": new_cell
            }
        )
    
    def _delete_cell(self, notebook: dict, cell_id: str) -> ToolResult:
        """删除单元格"""
        if not cell_id:
            return ToolResult(
                success=False,
                output="需要指定 cell_id",
                error="missing_cell_id"
            )
        
        cells = notebook.get('cells', [])
        original_count = len(cells)
        
        notebook['cells'] = [c for c in cells if c.get('id') != cell_id]
        
        if len(notebook['cells']) == original_count:
            return ToolResult(
                success=False,
                output=f"未找到 ID 为 {cell_id} 的单元格",
                error="cell_not_found"
            )
        
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已删除单元格 {cell_id[:8]}...",
            data={"deleted_id": cell_id}
        )
    
    def _update_cell(self, notebook: dict, cell_id: str, content: str, cell_type: str = None) -> ToolResult:
        """更新单元格"""
        if not cell_id:
            return ToolResult(
                success=False,
                output="需要指定 cell_id",
                error="missing_cell_id"
            )
        
        for cell in notebook.get('cells', []):
            if cell.get('id') == cell_id:
                if content is not None:
                    cell['source'] = content
                if cell_type is not None:
                    cell['cell_type'] = cell_type
                cell['metadata'] = cell.get('metadata', {})
                cell['metadata']['updated_by'] = 'ai_agent'
                cell['metadata']['updated_at'] = datetime.utcnow().isoformat()
                
                notebook['updated_at'] = datetime.utcnow()
                self.notebooks_store[self.notebook_id] = notebook
                
                return ToolResult(
                    success=True,
                    output=f"✅ 已更新单元格 {cell_id[:8]}...",
                    data={
                        "updated_id": cell_id,
                        "notebook_updated": True,
                        "updated_cell": cell
                    }
                )
        
        return ToolResult(
            success=False,
            output=f"未找到 ID 为 {cell_id} 的单元格",
            error="cell_not_found"
        )


class PipInstallTool(Tool):
    """
    安装 Python 包
    
    出于安全考虑，只允许安装白名单中的包
    """
    name = "pip_install"
    description = """使用 pip 安装 Python 包。
出于安全考虑，只能安装预定义白名单中的包（numpy, pandas, sklearn 等常用库）。
注意：此操作需要用户授权。"""
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要安装的包列表，如 ['pandas', 'scikit-learn==1.0']"
            },
            "upgrade": {
                "type": "boolean",
                "description": "是否升级到最新版本",
                "default": False
            }
        },
        "required": ["packages"]
    }
    
    def __init__(self, user_authorized: bool = False):
        self.user_authorized = user_authorized
    
    async def execute(self, packages: List[str], upgrade: bool = False, **kwargs) -> ToolResult:
        """安装包"""
        logger.info(f"[PipInstall] packages={packages}, upgrade={upgrade}")
        
        # 检查授权
        if not self.user_authorized:
            return ToolResult(
                success=False,
                output="安装包需要用户授权。请先启用「允许 AI 操作 Notebook」选项。",
                error="authorization_required",
                data={"requires_authorization": True, "action": "pip_install"}
            )
        
        # 验证包名
        blocked = []
        allowed = []
        
        for pkg in packages:
            # 提取包名（去掉版本号）
            pkg_name = re.split(r'[<>=!~]', pkg)[0].strip().lower()
            
            if pkg_name in ALLOWED_PACKAGES:
                allowed.append(pkg)
            else:
                blocked.append(pkg)
        
        if blocked:
            return ToolResult(
                success=False,
                output=f"以下包不在允许列表中: {', '.join(blocked)}\n允许的包包括: numpy, pandas, matplotlib, scikit-learn, torch, requests, beautifulsoup4 等常用库。",
                error="packages_not_allowed",
                data={"blocked": blocked, "allowed": allowed}
            )
        
        if not allowed:
            return ToolResult(
                success=False,
                output="没有可安装的包",
                error="no_packages"
            )
        
        try:
            # 构建 pip 命令
            cmd = [sys.executable, "-m", "pip", "install"]
            if upgrade:
                cmd.append("--upgrade")
            cmd.extend(allowed)
            
            logger.info(f"[PipInstall] 执行命令: {' '.join(cmd)}")
            
            # 执行安装（设置超时 5 分钟）
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=300
                )
            except asyncio.TimeoutError:
                process.kill()
                return ToolResult(
                    success=False,
                    output="安装超时（超过 5 分钟）",
                    error="timeout"
                )
            
            stdout_text = stdout.decode('utf-8', errors='ignore')
            stderr_text = stderr.decode('utf-8', errors='ignore')
            
            if process.returncode == 0:
                output_parts = [f"✅ 成功安装: {', '.join(allowed)}"]
                
                # 提取安装信息
                if "Successfully installed" in stdout_text:
                    match = re.search(r'Successfully installed (.+)', stdout_text)
                    if match:
                        output_parts.append(f"\n安装详情: {match.group(1)}")
                
                return ToolResult(
                    success=True,
                    output="\n".join(output_parts),
                    data={"installed": allowed, "stdout": stdout_text}
                )
            else:
                return ToolResult(
                    success=False,
                    output=f"安装失败:\n{stderr_text or stdout_text}",
                    error="pip_error",
                    data={"returncode": process.returncode}
                )
                
        except Exception as e:
            logger.error(f"[PipInstall] 安装失败: {e}")
            return ToolResult(
                success=False,
                output=f"安装失败: {str(e)}",
                error=str(e)
            )


class WebScrapeTool(Tool):
    """
    网页内容爬取工具
    
    支持提取文本、链接、表格等
    """
    name = "web_scrape"
    description = """爬取网页内容。
可以提取: text (纯文本), html (HTML), links (链接), tables (表格), all (全部)。
支持 CSS 选择器精确定位元素。"""
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要爬取的 URL"
            },
            "extract": {
                "type": "string",
                "enum": ["text", "html", "links", "tables", "all"],
                "description": "提取内容类型",
                "default": "text"
            },
            "selector": {
                "type": "string",
                "description": "CSS 选择器（可选），用于定位特定元素"
            },
            "max_length": {
                "type": "integer",
                "description": "最大返回长度",
                "default": 5000
            }
        },
        "required": ["url"]
    }
    
    async def execute(
        self,
        url: str,
        extract: str = "text",
        selector: str = None,
        max_length: int = 5000,
        **kwargs
    ) -> ToolResult:
        """爬取网页"""
        logger.info(f"[WebScrape] url={url}, extract={extract}")
        
        # 检查 bs4 是否可用
        if not BS4_AVAILABLE:
            return ToolResult(
                success=False,
                output="beautifulsoup4 未安装，请先安装: pip install beautifulsoup4 lxml",
                error="bs4_not_installed"
            )
        
        # URL 安全检查
        try:
            parsed = urlparse(url)
            
            # 检查协议
            if parsed.scheme not in ('http', 'https'):
                return ToolResult(
                    success=False,
                    output=f"不支持的 URL 协议: {parsed.scheme}",
                    error="invalid_protocol"
                )
            
            # 检查域名黑名单
            hostname = parsed.hostname or ''
            for blocked in BLOCKED_DOMAINS:
                if hostname.startswith(blocked) or hostname.endswith(blocked) or blocked in hostname:
                    return ToolResult(
                        success=False,
                        output=f"出于安全原因，无法访问此域名: {hostname}",
                        error="blocked_domain"
                    )
            
            # 检查私有 IP 地址
            if hostname.startswith('10.') or hostname.startswith('192.168.') or hostname.startswith('172.'):
                return ToolResult(
                    success=False,
                    output=f"无法访问私有 IP 地址",
                    error="private_ip"
                )
                    
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"无效的 URL: {e}",
                error="invalid_url"
            )
        
        try:
            # 发起请求
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    }
                )
                
                if response.status_code != 200:
                    return ToolResult(
                        success=False,
                        output=f"请求失败: HTTP {response.status_code}",
                        error=f"http_{response.status_code}"
                    )
                
                # 解析 HTML
                try:
                    soup = BeautifulSoup(response.text, 'lxml')
                except:
                    soup = BeautifulSoup(response.text, 'html.parser')
                
                # 移除脚本和样式
                for tag in soup(['script', 'style', 'noscript', 'iframe']):
                    tag.decompose()
                
                # 如果有选择器，定位到特定元素
                if selector:
                    elements = soup.select(selector)
                    if not elements:
                        return ToolResult(
                            success=True,
                            output=f"未找到匹配选择器 '{selector}' 的元素",
                            data={"url": url, "selector": selector}
                        )
                    # 创建一个新的容器来存放选中的元素
                    try:
                        container = BeautifulSoup('<div></div>', 'lxml').div
                    except:
                        container = BeautifulSoup('<div></div>', 'html.parser').div
                    for el in elements:
                        container.append(el.extract())
                    soup = container
                
                result_data = {"url": url, "selector": selector}
                output_parts = [f"🌐 网页内容 ({url[:50]}...):\n"]
                
                # 根据提取类型处理
                if extract == "text" or extract == "all":
                    text = soup.get_text(separator='\n', strip=True)
                    text = re.sub(r'\n{3,}', '\n\n', text)  # 压缩多余空行
                    text = text[:max_length]
                    result_data["text"] = text
                    output_parts.append(f"📄 文本内容:\n{text}")
                
                if extract == "links" or extract == "all":
                    links = []
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        text = a.get_text(strip=True)[:50]
                        if href.startswith(('http://', 'https://')):
                            links.append({"url": href, "text": text})
                    
                    links = links[:50]  # 限制链接数量
                    result_data["links"] = links
                    
                    if links:
                        output_parts.append(f"\n\n🔗 链接 ({len(links)} 个):")
                        for i, link in enumerate(links[:10], 1):
                            output_parts.append(f"\n{i}. [{link['text']}]({link['url']})")
                        if len(links) > 10:
                            output_parts.append(f"\n... 还有 {len(links) - 10} 个链接")
                
                if extract == "tables" or extract == "all":
                    tables = []
                    for table in soup.find_all('table'):
                        rows = []
                        for tr in table.find_all('tr'):
                            cells = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
                            if cells:
                                rows.append(cells)
                        if rows:
                            tables.append(rows)
                    
                    tables = tables[:5]  # 限制表格数量
                    result_data["tables"] = tables
                    
                    if tables:
                        output_parts.append(f"\n\n📊 表格 ({len(tables)} 个):")
                        for i, table in enumerate(tables[:2], 1):
                            output_parts.append(f"\n表格 {i}:")
                            for row in table[:5]:
                                output_parts.append(f"  | {' | '.join(str(c)[:20] for c in row)} |")
                            if len(table) > 5:
                                output_parts.append(f"  ... 还有 {len(table) - 5} 行")
                
                if extract == "html":
                    html = str(soup)[:max_length]
                    result_data["html"] = html
                    output_parts.append(f"📄 HTML 片段:\n{html[:1000]}...")
                
                return ToolResult(
                    success=True,
                    output="\n".join(output_parts)[:max_length],
                    data=result_data
                )
                
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="请求超时（30秒）",
                error="timeout"
            )
        except Exception as e:
            logger.error(f"[WebScrape] 爬取失败: {e}")
            return ToolResult(
                success=False,
                output=f"爬取失败: {str(e)}",
                error=str(e)
            )


class CodeAnalysisTool(Tool):
    """
    代码分析和优化建议工具
    
    分析语法错误、代码风格、性能问题等
    """
    name = "code_analysis"
    description = """分析 Python 代码，检查语法错误、代码风格和性能问题。
可以针对特定错误信息提供修复建议。"""
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要分析的代码"
            },
            "error_message": {
                "type": "string",
                "description": "错误信息（如果有的话）"
            },
            "analysis_type": {
                "type": "string",
                "enum": ["syntax", "style", "performance", "all"],
                "description": "分析类型",
                "default": "all"
            }
        },
        "required": ["code"]
    }
    
    async def execute(
        self,
        code: str,
        error_message: str = None,
        analysis_type: str = "all",
        **kwargs
    ) -> ToolResult:
        """分析代码"""
        logger.info(f"[CodeAnalysis] analysis_type={analysis_type}, has_error={bool(error_message)}")
        
        issues = []
        suggestions = []
        
        # 1. 语法检查
        if analysis_type in ["syntax", "all"]:
            try:
                compile(code, '<string>', 'exec')
            except SyntaxError as e:
                issues.append({
                    "type": "syntax_error",
                    "severity": "error",
                    "line": e.lineno,
                    "message": f"语法错误: {e.msg}",
                    "text": e.text
                })
        
        # 2. 代码风格检查
        if analysis_type in ["style", "all"]:
            lines = code.split('\n')
            
            # 检查行长度
            for i, line in enumerate(lines, 1):
                if len(line) > 120:
                    issues.append({
                        "type": "style",
                        "severity": "warning",
                        "line": i,
                        "message": f"行长度超过 120 字符 ({len(line)} 字符)"
                    })
            
            # 检查未使用的导入
            import_pattern = r'^(?:from\s+(\S+)\s+)?import\s+(.+)$'
            imports = []
            for i, line in enumerate(lines, 1):
                match = re.match(import_pattern, line.strip())
                if match:
                    module = match.group(1) or match.group(2).split(',')[0].split(' as ')[0].strip()
                    imports.append((i, module))
            
            # 检查是否使用了导入的模块
            code_without_imports = '\n'.join(
                l for l in lines if not re.match(r'^\s*(from|import)\s+', l)
            )
            for line_num, module in imports:
                # 简单检查：模块名是否出现在代码中
                module_base = module.split('.')[0]
                if module_base not in code_without_imports:
                    issues.append({
                        "type": "style",
                        "severity": "info",
                        "line": line_num,
                        "message": f"可能未使用的导入: {module}"
                    })
            
            # 检查 TODO/FIXME
            for i, line in enumerate(lines, 1):
                if 'TODO' in line or 'FIXME' in line:
                    issues.append({
                        "type": "style",
                        "severity": "info",
                        "line": i,
                        "message": f"发现 TODO/FIXME 注释"
                    })
        
        # 3. 性能检查
        if analysis_type in ["performance", "all"]:
            # 检查低效模式
            perf_patterns = [
                (r'for\s+\w+\s+in\s+range\s*\(\s*len\s*\(', 
                 "建议使用 enumerate() 代替 range(len())"),
                (r'\+=\s*\[', 
                 "在循环中使用 += [] 效率较低，考虑使用 extend() 或列表推导"),
                (r'\[\w+\]\[\w+\]',
                 "链式索引可能导致性能问题，考虑使用 .loc[] 或 .iloc[]"),
            ]
            
            for pattern, message in perf_patterns:
                if re.search(pattern, code, re.MULTILINE):
                    issues.append({
                        "type": "performance",
                        "severity": "warning",
                        "message": message
                    })
            
            # 检查可能的 N+1 问题
            if re.search(r'for.+:\s*\n\s+.*\.(query|execute|find|get)\(', code):
                issues.append({
                    "type": "performance",
                    "severity": "warning",
                    "message": "可能存在 N+1 查询问题，考虑批量操作"
                })
        
        # 4. 针对错误信息的建议
        if error_message:
            suggestions.extend(self._analyze_error(error_message, code))
        
        # 格式化输出
        output_parts = ["🔍 代码分析结果:\n"]
        
        if not issues and not suggestions:
            output_parts.append("✅ 未发现问题！")
        else:
            # 按严重程度排序
            severity_order = {'error': 0, 'warning': 1, 'info': 2}
            issues.sort(key=lambda x: severity_order.get(x.get('severity', 'info'), 3))
            
            error_count = sum(1 for i in issues if i.get('severity') == 'error')
            warning_count = sum(1 for i in issues if i.get('severity') == 'warning')
            
            if error_count:
                output_parts.append(f"❌ 发现 {error_count} 个错误")
            if warning_count:
                output_parts.append(f"⚠️ 发现 {warning_count} 个警告")
            
            output_parts.append("")
            
            for issue in issues:
                severity = issue.get('severity', 'info')
                icon = '❌' if severity == 'error' else ('⚠️' if severity == 'warning' else 'ℹ️')
                line_info = f"[行 {issue['line']}] " if 'line' in issue else ""
                output_parts.append(f"{icon} {line_info}{issue['message']}")
            
            if suggestions:
                output_parts.append("\n💡 修复建议:")
                for i, suggestion in enumerate(suggestions, 1):
                    output_parts.append(f"{i}. {suggestion}")
        
        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data={
                "issues": issues,
                "suggestions": suggestions,
                "summary": {
                    "errors": sum(1 for i in issues if i.get('severity') == 'error'),
                    "warnings": sum(1 for i in issues if i.get('severity') == 'warning'),
                    "info": sum(1 for i in issues if i.get('severity') == 'info')
                }
            }
        )
    
    def _analyze_error(self, error_message: str, code: str) -> List[str]:
        """根据错误信息提供修复建议"""
        suggestions = []
        error_lower = error_message.lower()
        
        # NameError
        if 'nameerror' in error_lower:
            match = re.search(r"name '(\w+)' is not defined", error_message)
            if match:
                var_name = match.group(1)
                suggestions.append(f"变量 '{var_name}' 未定义，请检查是否拼写错误或需要先导入")
                # 检查是否是常见模块
                if var_name in ['np', 'pd', 'plt', 'sns', 'tf', 'torch']:
                    module_map = {
                        'np': 'numpy', 'pd': 'pandas', 'plt': 'matplotlib.pyplot',
                        'sns': 'seaborn', 'tf': 'tensorflow', 'torch': 'torch'
                    }
                    suggestions.append(f"如果要使用 {var_name}，请添加: import {module_map.get(var_name, var_name)} as {var_name}")
        
        # TypeError
        elif 'typeerror' in error_lower:
            if 'not subscriptable' in error_lower:
                suggestions.append("尝试对不可索引的对象使用下标，检查变量类型是否正确")
            elif 'not iterable' in error_lower:
                suggestions.append("尝试遍历不可迭代对象，确保变量是列表、字典等可迭代类型")
            elif 'takes' in error_lower and 'argument' in error_lower:
                suggestions.append("函数参数数量不匹配，检查函数定义和调用")
        
        # IndexError
        elif 'indexerror' in error_lower:
            suggestions.append("索引超出范围，检查列表/数组长度")
            suggestions.append("可以使用 len() 检查长度，或使用 try-except 捕获异常")
        
        # KeyError
        elif 'keyerror' in error_lower:
            suggestions.append("字典键不存在，使用 .get() 方法可以避免此错误")
            suggestions.append("或者先用 'key in dict' 检查键是否存在")
        
        # ImportError / ModuleNotFoundError
        elif 'importerror' in error_lower or 'modulenotfound' in error_lower:
            match = re.search(r"No module named '(\w+)'", error_message)
            if match:
                module_name = match.group(1)
                suggestions.append(f"模块 '{module_name}' 未安装，可以使用 pip_install 工具安装")
        
        # AttributeError
        elif 'attributeerror' in error_lower:
            suggestions.append("对象没有此属性或方法，检查对象类型和拼写")
            suggestions.append("使用 dir(obj) 或 type(obj) 查看对象信息")
        
        # ValueError
        elif 'valueerror' in error_lower:
            if 'convert' in error_lower or 'literal' in error_lower:
                suggestions.append("类型转换失败，检查数据格式是否正确")
            else:
                suggestions.append("值错误，检查输入数据的范围和格式")
        
        # FileNotFoundError
        elif 'filenotfound' in error_lower:
            suggestions.append("文件不存在，检查文件路径是否正确")
            suggestions.append("可以使用 os.path.exists() 先检查文件是否存在")
        
        return suggestions


# ========== 增强的 LiteratureSearchTool ==========

class EnhancedLiteratureSearchTool(Tool):
    """
    增强版学术文献搜索工具
    
    支持多源搜索、过滤选项、详细元数据
    """
    name = "literature_search"
    description = """搜索学术论文和文献。
支持的来源: semantic_scholar, arxiv, pubmed, openalex, crossref。
可以按年份、领域过滤结果。"""
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词"
            },
            "source": {
                "type": "string",
                "enum": ["semantic_scholar", "arxiv", "pubmed", "openalex", "crossref"],
                "description": "搜索来源",
                "default": "semantic_scholar"
            },
            "max_results": {
                "type": "integer",
                "description": "最大结果数",
                "default": 5
            },
            "year_start": {
                "type": "integer",
                "description": "起始年份"
            },
            "year_end": {
                "type": "integer",
                "description": "结束年份"
            },
            "fields": {
                "type": "string",
                "description": "研究领域过滤（逗号分隔）"
            }
        },
        "required": ["query"]
    }
    
    def __init__(self):
        from app.services.literature_service import get_literature_service
        self.service = get_literature_service()
    
    async def execute(
        self,
        query: str,
        source: str = "semantic_scholar",
        max_results: int = 5,
        year_start: int = None,
        year_end: int = None,
        fields: str = None,
        **kwargs
    ) -> ToolResult:
        """执行学术文献搜索"""
        logger.info(f"[LiteratureSearch] query={query}, source={source}")
        
        try:
            # 构建搜索参数
            search_kwargs = {}
            if year_start and year_end:
                search_kwargs["year_range"] = (year_start, year_end)
            if fields:
                search_kwargs["fields_of_study"] = [f.strip() for f in fields.split(',')]
            
            # 执行搜索
            result = await self.service.search(
                query=query,
                source=source,
                limit=max_results,
                **search_kwargs
            )
            
            if "error" in result:
                return ToolResult(
                    success=False,
                    output=f"搜索失败: {result['error']}",
                    error=result["error"]
                )
            
            papers = result.get("papers", [])
            
            if not papers:
                return ToolResult(
                    success=True,
                    output=f"未找到关于 '{query}' 的学术论文。",
                    data={"papers": [], "query": query, "source": source}
                )
            
            # 格式化输出
            source_names = {
                "semantic_scholar": "Semantic Scholar",
                "arxiv": "arXiv",
                "pubmed": "PubMed",
                "openalex": "OpenAlex",
                "crossref": "Crossref"
            }
            
            output_parts = [f"📚 在 {source_names.get(source, source)} 搜索 '{query}' 的结果:\n"]
            
            for i, paper in enumerate(papers, 1):
                # 作者
                authors = paper.authors[:3] if paper.authors else []
                author_names = [a.get("name", "Unknown") for a in authors]
                author_str = ", ".join(author_names)
                if len(paper.authors) > 3:
                    author_str += " et al."
                
                output_parts.append(f"\n【{i}】{paper.title}")
                if paper.year:
                    output_parts.append(f" ({paper.year})")
                output_parts.append(f"\n👥 {author_str}")
                
                if paper.venue:
                    output_parts.append(f"\n📍 {paper.venue}")
                
                if paper.citation_count > 0:
                    output_parts.append(f"\n📊 引用: {paper.citation_count}")
                
                if paper.abstract:
                    abstract = paper.abstract[:200]
                    if len(paper.abstract) > 200:
                        abstract += "..."
                    output_parts.append(f"\n📝 {abstract}")
                
                if paper.url:
                    output_parts.append(f"\n🔗 {paper.url}")
                
                if paper.pdf_url:
                    output_parts.append(f"\n📄 PDF: {paper.pdf_url}")
            
            return ToolResult(
                success=True,
                output="\n".join(output_parts),
                data={
                    "papers": [self._paper_to_dict(p) for p in papers],
                    "query": query,
                    "source": source,
                    "total": result.get("total", len(papers))
                }
            )
            
        except Exception as e:
            logger.error(f"[LiteratureSearch] 错误: {e}")
            return ToolResult(
                success=False,
                output=f"搜索错误: {str(e)}",
                error=str(e)
            )
    
    def _paper_to_dict(self, paper) -> dict:
        """将论文对象转为字典"""
        return {
            "source": paper.source,
            "external_id": paper.external_id,
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "year": paper.year,
            "venue": paper.venue,
            "citation_count": paper.citation_count,
            "reference_count": paper.reference_count,
            "url": paper.url,
            "pdf_url": paper.pdf_url,
            "arxiv_id": paper.arxiv_id,
            "doi": paper.doi,
            "fields_of_study": paper.fields_of_study
        }


# ========== 工具工厂函数 ==========

def create_notebook_tools(
    kernel_manager,
    notebooks_store: dict,
    notebook_id: str,
    user_authorized: bool = False
) -> List[Tool]:
    """
    创建 Notebook 专用工具集
    
    Args:
        kernel_manager: 内核管理器
        notebooks_store: notebooks 存储字典
        notebook_id: Notebook ID
        user_authorized: 用户是否授权操作
        
    Returns:
        工具列表
    """
    return [
        NotebookExecuteTool(kernel_manager, notebook_id, user_authorized),
        NotebookVariablesTool(kernel_manager, notebook_id),
        NotebookCellTool(notebooks_store, notebook_id, user_authorized),
        PipInstallTool(user_authorized),
        WebScrapeTool(),
        CodeAnalysisTool(),
        EnhancedLiteratureSearchTool(),
    ]
