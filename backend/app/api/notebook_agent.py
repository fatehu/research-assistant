"""
Notebook Agent API

提供：
1. Agent 流式聊天
2. Agent 历史查询/清空
3. Notebook Agent 可用工具查询
"""

import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.codelab import _notebooks, get_notebook_cached, kernel_manager
from app.core.database import async_session_factory, get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.agent_tools import ToolRegistry
from app.services.llm_service import get_llm_service
from app.services.notebook_agent_history_service import (
    append_history_message,
    clear_history as clear_history_in_db,
    load_history,
)
from app.services.notebook_tools import create_notebook_tools
from app.services.react_agent import AgentRuntimeContext, create_react_agent

router = APIRouter()

AGENT_HISTORY_CHANNEL = "notebook_agent"
_agent_histories: Dict[str, List[Dict[str, Any]]] = {}


class NotebookAgentChatRequest(BaseModel):
    """Notebook Agent 聊天请求"""

    message: str
    include_context: bool = True
    include_variables: bool = True
    user_authorized: bool = False
    stream: bool = True


class NotebookAgentMessage(BaseModel):
    """Notebook Agent 消息"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str  # user | assistant | system
    content: str
    code_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NotebookAgentHistoryResponse(BaseModel):
    """Notebook Agent 历史响应"""

    notebook_id: str
    messages: List[NotebookAgentMessage]
    context: Optional[Dict[str, Any]] = None


def _history_key(notebook_id: str, user_id: int) -> str:
    return f"{user_id}:{notebook_id}"


async def get_agent_history(notebook_id: str, user_id: int) -> List[Dict[str, Any]]:
    """读取 Agent 历史（内存缓存 + DB）"""

    key = _history_key(notebook_id, user_id)
    if key not in _agent_histories:
        history = await load_history(
            notebook_id=notebook_id,
            user_id=user_id,
            channel=AGENT_HISTORY_CHANNEL,
        )
        _agent_histories[key] = history.get("messages", [])
    return _agent_histories.get(key, [])


async def save_agent_message(notebook_id: str, user_id: int, message: Dict[str, Any]) -> None:
    """保存 Agent 消息（落库）"""

    key = _history_key(notebook_id, user_id)
    current_history = await load_history(
        notebook_id=notebook_id,
        user_id=user_id,
        channel=AGENT_HISTORY_CHANNEL,
    )
    persisted_history = await append_history_message(
        notebook_id=notebook_id,
        user_id=user_id,
        channel=AGENT_HISTORY_CHANNEL,
        history=current_history,
        message=message,
    )
    _agent_histories[key] = persisted_history.get("messages", [])


async def clear_agent_history(notebook_id: str, user_id: int) -> None:
    """清空 Agent 历史（清缓存 + 清 DB）"""

    key = _history_key(notebook_id, user_id)
    await clear_history_in_db(
        notebook_id=notebook_id,
        user_id=user_id,
        channel=AGENT_HISTORY_CHANNEL,
    )
    _agent_histories[key] = []


class NotebookToolRegistry(ToolRegistry):
    """带 Notebook 上下文的 ToolRegistry"""

    def __init__(
        self,
        db: Any,
        user_id: int,
        db_session_factory: Any = None,
        notebook_id: Optional[str] = None,
        kernel_manager: Any = None,
        notebooks_store: Optional[dict] = None,
        user_authorized: bool = False,
    ) -> None:
        super().__init__(db=db, user_id=user_id, db_session_factory=db_session_factory)

        self.notebook_id = notebook_id
        self.kernel_manager = kernel_manager
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized

        if notebook_id and kernel_manager and notebooks_store is not None:
            self._register_notebook_tools()

    def _register_notebook_tools(self) -> None:
        notebook_tools = create_notebook_tools(
            self.kernel_manager,
            self.notebooks_store,
            self.notebook_id,
            self.user_authorized,
        )
        for tool in notebook_tools:
            self.register(tool)
        logger.info(f"[NotebookToolRegistry] registered tools: {len(notebook_tools)}")


@router.post("/notebooks/{notebook_id}/agent/chat")
async def notebook_agent_chat(
    notebook_id: str,
    request: NotebookAgentChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Notebook AI Agent 聊天接口（SSE）
    """

    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    logger.info(
        f"[NotebookAgent] chat request: notebook_id={notebook_id}, authorized={request.user_authorized}"
    )

    tool_registry = NotebookToolRegistry(
        db=None,
        db_session_factory=async_session_factory,
        user_id=current_user.id,
        notebook_id=notebook_id,
        kernel_manager=kernel_manager,
        notebooks_store=_notebooks,
        user_authorized=request.user_authorized,
    )

    llm_service = await get_llm_service()
    agent = create_react_agent(
        llm_service,
        tool_registry,
        runtime_context=AgentRuntimeContext(
            user_id=current_user.id,
            channel="notebook_agent",
            notebook_id=notebook_id,
        ),
    )

    context_parts: List[str] = []
    if request.include_context:
        cells_info: List[str] = []
        for i, cell in enumerate(notebook.get("cells", [])):
            cell_type = cell.get("cell_type", "code")
            source = cell.get("source", "")
            short_source = source[:100] + ("..." if len(source) > 100 else "")
            exec_count = cell.get("execution_count")
            has_output = bool(cell.get("outputs"))
            cell_desc = f"Cell {i + 1} ({cell_type})"
            if exec_count is not None:
                cell_desc += f" [执行次数: {exec_count}]"
            if has_output:
                cell_desc += " [有输出]"
            cell_desc += f": {short_source}"
            cells_info.append(cell_desc)

        context_parts.append(
            f"## Notebook 状态\n- 标题: {notebook.get('title')}\n- 单元格数: {len(notebook.get('cells', []))}"
        )
        if cells_info:
            context_parts.append("### 单元格列表\n" + "\n".join(cells_info[:10]))

    if request.include_variables:
        kernel = kernel_manager.get_kernel(notebook_id)
        if kernel:
            variables = kernel.get_variables()
            if variables:
                vars_info = []
                for name, info in list(variables.items())[:10]:
                    if isinstance(info, dict):
                        var_type = info.get("type", "unknown")
                        shape = info.get("shape", info.get("length", ""))
                        vars_info.append(f"- {name}: {var_type}" + (f" ({shape})" if shape else ""))
                    else:
                        vars_info.append(f"- {name}: {info}")
                context_parts.append("### 当前变量\n" + "\n".join(vars_info))

    system_context = "\n\n".join(context_parts) if context_parts else ""

    history = await get_agent_history(notebook_id, current_user.id)
    messages: List[Dict[str, str]] = []
    for msg in history[-10:]:
        messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    user_content = request.message
    if system_context:
        user_content = f"{system_context}\n\n---\n\n用户问题: {request.message}"
    messages.append({"role": "user", "content": user_content})

    await save_agent_message(
        notebook_id,
        current_user.id,
        {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": request.message,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": {"authorized": request.user_authorized},
        },
    )

    async def event_generator():
        full_content = ""
        code_blocks: List[Dict[str, str]] = []
        rag_metrics = None

        try:
            async for event in agent.run(messages, stream=True):
                event_type = event.get("type")
                event_data = event.get("data")

                if event_type == "content":
                    full_content += event_data
                    yield f"data: {json.dumps({'type': 'content', 'content': event_data})}\n\n"
                elif event_type == "thought":
                    yield f"data: {json.dumps({'type': 'thought', 'content': event_data})}\n\n"
                elif event_type == "action":
                    tool_name = event_data.get("tool", "") if isinstance(event_data, dict) else ""
                    tool_input = event_data.get("input", {}) if isinstance(event_data, dict) else {}
                    yield f"data: {json.dumps({'type': 'action', 'tool': tool_name, 'input': tool_input})}\n\n"
                elif event_type == "observation":
                    output_raw = event_data.get("output", "") if isinstance(event_data, dict) else event_data
                    output = output_raw if isinstance(output_raw, str) else str(output_raw)
                    payload = {
                        "type": "observation",
                        "tool": event_data.get("tool") if isinstance(event_data, dict) else "",
                        "success": event_data.get("success") if isinstance(event_data, dict) else False,
                        "output": output,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

                    if isinstance(event_data, dict) and "authorization_required" in str(
                        event_data.get("error", "")
                    ):
                        yield f"data: {json.dumps({'type': 'authorization_required', 'action': event_data.get('tool')})}\n\n"
                elif event_type == "answer":
                    full_content = event_data if isinstance(event_data, str) else str(event_data)
                    yield f"data: {json.dumps({'type': 'answer', 'content': full_content})}\n\n"
                elif event_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'error': event_data})}\n\n"
                elif event_type == "start":
                    provider = event_data.get("provider", "") if isinstance(event_data, dict) else ""
                    model = event_data.get("model", "") if isinstance(event_data, dict) else ""
                    yield f"data: {json.dumps({'type': 'start', 'provider': provider, 'model': model})}\n\n"
                elif event_type == "done":
                    if isinstance(event_data, dict) and event_data.get("answer"):
                        full_content = event_data.get("answer", full_content)
                    if isinstance(event_data, dict) and isinstance(event_data.get("rag_metrics"), dict):
                        rag_metrics = event_data["rag_metrics"]

            matches = re.findall(r"```(\w+)?\n(.*?)```", full_content, re.DOTALL)
            for lang, code in matches:
                code_blocks.append({"language": lang or "python", "code": code.strip()})

            await save_agent_message(
                notebook_id,
                current_user.id,
                {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": full_content,
                    "code_blocks": code_blocks,
                    "timestamp": datetime.utcnow().isoformat(),
                    "metadata": {"rag_metrics": rag_metrics} if isinstance(rag_metrics, dict) else {},
                },
            )

            done_payload = {"type": "done", "code_blocks": code_blocks}
            if isinstance(rag_metrics, dict):
                done_payload["rag_metrics"] = rag_metrics
            yield f"data: {json.dumps(done_payload)}\n\n"
        except Exception as exc:
            logger.error(f"[NotebookAgent] error: {exc}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/notebooks/{notebook_id}/agent/history", response_model=NotebookAgentHistoryResponse)
async def get_notebook_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取 Notebook Agent 历史"""

    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    history = await get_agent_history(notebook_id, current_user.id)
    context = {
        "notebook_title": notebook.get("title"),
        "cell_count": len(notebook.get("cells", [])),
        "execution_count": notebook.get("execution_count", 0),
    }

    kernel = kernel_manager.get_kernel(notebook_id)
    if kernel:
        context["variables"] = kernel.get_variables()

    return NotebookAgentHistoryResponse(
        notebook_id=notebook_id,
        messages=[NotebookAgentMessage(**msg) for msg in history],
        context=context,
    )


@router.delete("/notebooks/{notebook_id}/agent/history")
async def clear_notebook_agent_history(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清空 Notebook Agent 历史"""

    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    await clear_agent_history(notebook_id, current_user.id)
    return {"message": "对话历史已清空"}


@router.get("/notebooks/{notebook_id}/agent/tools")
async def get_available_tools(
    notebook_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取 Notebook Agent 可用工具列表"""

    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    tool_registry = NotebookToolRegistry(
        db=None,
        db_session_factory=async_session_factory,
        user_id=current_user.id,
        notebook_id=notebook_id,
        kernel_manager=kernel_manager,
        notebooks_store=_notebooks,
        user_authorized=False,
    )
    await tool_registry.refresh_mcp_tools()

    tools = []
    for tool_info in tool_registry.list_tools():
        func = tool_info.get("function", {})
        name = func.get("name", "")
        tools.append(
            {
                "name": name,
                "description": func.get("description"),
                "parameters": func.get("parameters"),
                "requires_authorization": (
                    "notebook_execute" in name or "notebook_cell" in name or "pip_install" in name
                ),
            }
        )

    return {"tools": tools}
