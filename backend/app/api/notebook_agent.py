"""
Notebook Agent API - 让 AI 助手能够操作 Notebook

提供:
1. Agent 聊天接口（流式）
2. 对话历史管理
3. 授权控制
"""
import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from app.core.security import get_current_user
from app.models.user import User
from app.services.llm_service import get_llm_service
from app.services.react_agent import create_react_agent
from app.services.agent_tools import ToolRegistry, Tool
from app.services.notebook_tools import create_notebook_tools

# 导入 codelab 的内核管理器和 notebooks 存储
from app.api.codelab import kernel_manager, _notebooks, get_notebook

router = APIRouter()


# ========== Pydantic Models ==========

class NotebookAgentChatRequest(BaseModel):
    """Notebook Agent 聊天请求"""
    message: str
    include_context: bool = True
    include_variables: bool = True
    user_authorized: bool = False  # 用户是否授权 Agent 操作 Notebook
    stream: bool = True


class NotebookAgentMessage(BaseModel):
    """Agent 消息"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str  # 'user', 'assistant', 'system'
    content: str
    code_blocks: List[Dict[str, Any]] = []
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = {}


class NotebookAgentHistoryResponse(BaseModel):
    """对话历史响应"""
    notebook_id: str
    messages: List[NotebookAgentMessage]
    context: Optional[Dict[str, Any]] = None


# ========== 对话历史存储（内存） ==========

_agent_histories: Dict[str, List[Dict[str, Any]]] = {}


def get_agent_history(notebook_id: str) -> List[Dict[str, Any]]:
    """获取 Agent 对话历史"""
    return _agent_histories.get(notebook_id, [])


def save_agent_message(notebook_id: str, message: Dict[str, Any]):
    """保存 Agent 消息"""
    if notebook_id not in _agent_histories:
        _agent_histories[notebook_id] = []
    _agent_histories[notebook_id].append(message)
    # 限制历史长度
    if len(_agent_histories[notebook_id]) > 100:
        _agent_histories[notebook_id] = _agent_histories[notebook_id][-50:]


def clear_agent_history(notebook_id: str):
    """清空 Agent 对话历史"""
    _agent_histories[notebook_id] = []


# ========== 扩展的工具注册表 ==========

class NotebookToolRegistry(ToolRegistry):
    """
    扩展的工具注册表，支持 Notebook 上下文
    """
    
    def __init__(
        self,
        db,
        user_id: int,
        notebook_id: str = None,
        kernel_manager=None,
        notebooks_store: dict = None,
        user_authorized: bool = False
    ):
        # 初始化基础工具
        super().__init__(db, user_id)
        
        self.notebook_id = notebook_id
        self.kernel_manager = kernel_manager
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized
        
        # 如果提供了 notebook 上下文，注册 notebook 工具
        if notebook_id and kernel_manager and notebooks_store is not None:
            self._register_notebook_tools()
    
    def _register_notebook_tools(self):
        """注册 Notebook 专用工具"""
        notebook_tools = create_notebook_tools(
            self.kernel_manager,
            self.notebooks_store,
            self.notebook_id,
            self.user_authorized
        )
        
        for tool in notebook_tools:
            # 覆盖同名工具（如 literature_search）
            self.register(tool)
        
        logger.info(f"[NotebookToolRegistry] 已注册 {len(notebook_tools)} 个 Notebook 工具")


# ========== API 端点 ==========

@router.post("/notebooks/{notebook_id}/agent/chat")
async def notebook_agent_chat(
    notebook_id: str,
    request: NotebookAgentChatRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Notebook AI Agent 聊天接口
    
    支持流式响应，Agent 可以操作 Notebook（需要授权）
    """
    # 验证 notebook 存在
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    logger.info(f"[NotebookAgent] Chat request: notebook_id={notebook_id}, authorized={request.user_authorized}")
    
    # 创建工具注册表（带 notebook 上下文）
    tool_registry = NotebookToolRegistry(
        db=None,  # Notebook 工具不需要数据库
        user_id=current_user.id,
        notebook_id=notebook_id,
        kernel_manager=kernel_manager,
        notebooks_store=_notebooks,
        user_authorized=request.user_authorized
    )
    
    # 获取 LLM 服务
    llm_service = get_llm_service()
    
    # 创建 ReAct Agent
    agent = create_react_agent(llm_service, tool_registry)
    
    # 构建上下文信息
    context_parts = []
    
    if request.include_context:
        # 添加 notebook 信息
        cells_info = []
        for i, cell in enumerate(notebook.get('cells', [])):
            cell_type = cell.get('cell_type', 'code')
            source = cell.get('source', '')[:200]
            exec_count = cell.get('execution_count')
            has_output = bool(cell.get('outputs'))
            
            cell_desc = f"Cell {i+1} ({cell_type})"
            if exec_count:
                cell_desc += f" [执行次数: {exec_count}]"
            if has_output:
                cell_desc += " [有输出]"
            cell_desc += f": {source[:100]}..." if len(source) > 100 else f": {source}"
            cells_info.append(cell_desc)
        
        context_parts.append(f"## Notebook 状态\n- 标题: {notebook.get('title')}\n- 单元格数: {len(notebook.get('cells', []))}")
        if cells_info:
            context_parts.append("### 单元格列表:\n" + "\n".join(cells_info[:10]))
    
    if request.include_variables:
        # 获取当前变量
        kernel = kernel_manager.get_kernel(notebook_id)
        if kernel:
            variables = kernel.get_variables()
            if variables:
                vars_info = []
                for name, info in list(variables.items())[:10]:
                    var_type = info.get('type', 'unknown')
                    shape = info.get('shape', info.get('length', ''))
                    vars_info.append(f"- {name}: {var_type}" + (f" ({shape})" if shape else ""))
                context_parts.append("### 当前变量:\n" + "\n".join(vars_info))
    
    # 构建完整的系统上下文
    system_context = ""
    if context_parts:
        system_context = "\n\n".join(context_parts)
    
    # 获取历史消息
    history = get_agent_history(notebook_id)
    
    # 构建消息列表
    messages = []
    
    # 添加历史消息（最近 10 条）
    for msg in history[-10:]:
        messages.append({
            "role": msg.get('role', 'user'),
            "content": msg.get('content', '')
        })
    
    # 添加当前用户消息（包含上下文）
    user_content = request.message
    if system_context:
        user_content = f"{system_context}\n\n---\n\n用户问题: {request.message}"
    
    messages.append({
        "role": "user",
        "content": user_content
    })
    
    # 保存用户消息
    save_agent_message(notebook_id, {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": request.message,
        "timestamp": datetime.utcnow().isoformat(),
        "metadata": {"authorized": request.user_authorized}
    })
    
    # 流式响应
    async def event_generator():
        full_content = ""
        code_blocks = []
        
        try:
            async for event in agent.run(messages, stream=True):
                event_type = event.get("type")
                event_data = event.get("data")
                
                if event_type == "content":
                    # 流式输出内容
                    full_content += event_data
                    yield f"data: {json.dumps({'type': 'content', 'content': event_data})}\n\n"
                
                elif event_type == "thought":
                    # Agent 思考过程
                    yield f"data: {json.dumps({'type': 'thought', 'content': event_data})}\n\n"
                
                elif event_type == "action":
                    # Agent 执行工具
                    tool_name = event_data.get("tool", "")
                    tool_input = event_data.get("input", {})
                    yield f"data: {json.dumps({'type': 'action', 'tool': tool_name, 'input': tool_input})}\n\n"
                
                elif event_type == "observation":
                    # 工具执行结果
                    yield f"data: {json.dumps({'type': 'observation', 'tool': event_data.get('tool'), 'success': event_data.get('success'), 'output': event_data.get('output', '')[:500]})}\n\n"
                    
                    # 检查是否需要授权
                    output = event_data.get('output', '')
                    if 'authorization_required' in str(event_data.get('error', '')):
                        yield f"data: {json.dumps({'type': 'authorization_required', 'action': event_data.get('tool')})}\n\n"
                
                elif event_type == "answer":
                    # 最终答案
                    full_content = event_data
                    yield f"data: {json.dumps({'type': 'answer', 'content': event_data})}\n\n"
                
                elif event_type == "error":
                    # 错误
                    yield f"data: {json.dumps({'type': 'error', 'error': event_data})}\n\n"
                
                elif event_type == "start":
                    # 开始
                    yield f"data: {json.dumps({'type': 'start', 'provider': event_data.get('provider'), 'model': event_data.get('model')})}\n\n"
            
            # 提取代码块
            import re
            code_pattern = r'```(\w+)?\n(.*?)```'
            matches = re.findall(code_pattern, full_content, re.DOTALL)
            for lang, code in matches:
                code_blocks.append({
                    "language": lang or "python",
                    "code": code.strip()
                })
            
            # 保存助手消息
            save_agent_message(notebook_id, {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": full_content,
                "code_blocks": code_blocks,
                "timestamp": datetime.utcnow().isoformat(),
                "metadata": {}
            })
            
            # 发送完成事件
            yield f"data: {json.dumps({'type': 'done', 'code_blocks': code_blocks})}\n\n"
            
        except Exception as e:
            logger.error(f"[NotebookAgent] Error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/notebooks/{notebook_id}/agent/history", response_model=NotebookAgentHistoryResponse)
async def get_notebook_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """获取 Notebook Agent 对话历史"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    history = get_agent_history(notebook_id)
    
    # 获取上下文信息
    context = {
        "notebook_title": notebook.get("title"),
        "cell_count": len(notebook.get("cells", [])),
        "execution_count": notebook.get("execution_count", 0)
    }
    
    # 获取变量信息
    kernel = kernel_manager.get_kernel(notebook_id)
    if kernel:
        context["variables"] = kernel.get_variables()
    
    return NotebookAgentHistoryResponse(
        notebook_id=notebook_id,
        messages=[NotebookAgentMessage(**msg) for msg in history],
        context=context
    )


@router.delete("/notebooks/{notebook_id}/agent/history")
async def clear_notebook_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """清空 Notebook Agent 对话历史"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    clear_agent_history(notebook_id)
    
    return {"message": "对话历史已清空"}


@router.get("/notebooks/{notebook_id}/agent/tools")
async def get_available_tools(
    notebook_id: str,
    current_user: User = Depends(get_current_user)
):
    """获取可用工具列表"""
    notebook = get_notebook(notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    
    # 创建工具注册表
    tool_registry = NotebookToolRegistry(
        db=None,
        user_id=current_user.id,
        notebook_id=notebook_id,
        kernel_manager=kernel_manager,
        notebooks_store=_notebooks,
        user_authorized=False
    )
    
    # 获取工具列表
    tools = []
    for tool_info in tool_registry.list_tools():
        func = tool_info.get("function", {})
        tools.append({
            "name": func.get("name"),
            "description": func.get("description"),
            "parameters": func.get("parameters"),
            "requires_authorization": "notebook_execute" in func.get("name", "") or 
                                     "notebook_cell" in func.get("name", "") or
                                     "pip_install" in func.get("name", "")
        })
    
    return {"tools": tools}
