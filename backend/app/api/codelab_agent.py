"""CodeLab Agent 路由拆分模块。"""
import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.api import codelab as codelab_base
from app.config import settings
from app.core.database import async_session_factory, get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.agent_runtime_service import get_agent_runtime_service
from app.services.notebook_agent_history_service import (
    append_history_message,
    clear_history as clear_history_in_db,
    load_history,
)
from app.services.notebook_service import NotebookService

router = APIRouter()

# 从主模块复用共享上下文，避免破坏既有导入点。
get_notebook_cached = codelab_base.get_notebook_cached
kernel_manager = codelab_base.kernel_manager
_notebooks = codelab_base._notebooks

# ========== Notebook Agent API ==========

# Agent 对话历史存储 (内存中)
_agent_histories: Dict[str, Dict[str, Any]] = {}
AGENT_HISTORY_CHANNEL = "codelab"


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


class AgentMemorySettingsResponse(BaseModel):
    system_enabled: bool
    user_enabled: bool
    effective_enabled: bool
    enabled_channels: List[str]
    retention_days: int
    max_items_per_user_channel: int
    top_k: int
    updated_at: Optional[str] = None


class AgentMemorySettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    enabled_channels: Optional[List[str]] = None


_AGENT_MEMORY_PREF_KEY = "agent_memory"
_AGENT_MEMORY_ALLOWED_CHANNELS = {"chat", "codelab_agent", "notebook_agent", "literature_agent"}


def _default_memory_channels() -> List[str]:
    raw = str(getattr(settings, "agent_memory_default_channels", "") or "").strip()
    values: List[str] = []
    for item in raw.split(","):
        channel = str(item or "").strip()
        if channel and channel not in values and channel in _AGENT_MEMORY_ALLOWED_CHANNELS:
            values.append(channel)
    if values:
        return values
    return ["chat", "codelab_agent", "notebook_agent", "literature_agent"]


def _normalize_memory_channels(channels: Optional[List[str]]) -> List[str]:
    if not isinstance(channels, list) or not channels:
        return _default_memory_channels()
    normalized: List[str] = []
    for item in channels:
        channel = str(item or "").strip()
        if channel and channel not in normalized and channel in _AGENT_MEMORY_ALLOWED_CHANNELS:
            normalized.append(channel)
    return normalized or _default_memory_channels()


def _build_memory_settings_response(user: User) -> AgentMemorySettingsResponse:
    preferences = user.preferences if isinstance(user.preferences, dict) else {}
    raw = preferences.get(_AGENT_MEMORY_PREF_KEY) if isinstance(preferences, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    user_enabled = bool(raw.get("enabled", False))
    enabled_channels = _normalize_memory_channels(raw.get("enabled_channels"))
    system_enabled = bool(getattr(settings, "agent_longterm_memory_enabled", False))
    current_channel_allowed = "codelab_agent" in enabled_channels
    return AgentMemorySettingsResponse(
        system_enabled=system_enabled,
        user_enabled=user_enabled,
        effective_enabled=bool(system_enabled and user_enabled and current_channel_allowed),
        enabled_channels=enabled_channels,
        retention_days=max(int(getattr(settings, "agent_memory_retention_days", 180)), 1),
        max_items_per_user_channel=max(int(getattr(settings, "agent_memory_max_items_per_user_channel", 2000)), 100),
        top_k=max(int(getattr(settings, "agent_memory_top_k", 3)), 1),
        updated_at=raw.get("updated_at"),
    )


async def get_agent_history(notebook_id: str, user_id: int) -> Dict[str, Any]:
    """获取 Agent 对话历史"""
    key = f"{user_id}:{notebook_id}"
    if key not in _agent_histories:
        _agent_histories[key] = await load_history(
            notebook_id=notebook_id,
            user_id=user_id,
            channel=AGENT_HISTORY_CHANNEL,
        )
    return _agent_histories[key]


async def save_agent_message(notebook_id: str, user_id: int, message: AgentMessage):
    """保存 Agent 消息"""
    key = f"{user_id}:{notebook_id}"
    history = await get_agent_history(notebook_id, user_id)
    _agent_histories[key] = await append_history_message(
        notebook_id=notebook_id,
        user_id=user_id,
        channel=AGENT_HISTORY_CHANNEL,
        history=history,
        message=message.model_dump(),
    )


async def clear_agent_history_state(notebook_id: str, user_id: int) -> None:
    """清空 Agent 对话历史"""
    key = f"{user_id}:{notebook_id}"
    _agent_histories[key] = await clear_history_in_db(
        notebook_id=notebook_id,
        user_id=user_id,
        channel=AGENT_HISTORY_CHANNEL,
    )


@router.get("/agent/memory/settings", response_model=AgentMemorySettingsResponse)
async def get_agent_memory_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取 Agent 长期记忆设置（系统开关 + 用户开关双门控）。"""
    db_user = await db.get(User, int(current_user.id))
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _build_memory_settings_response(db_user)


@router.put("/agent/memory/settings", response_model=AgentMemorySettingsResponse)
async def update_agent_memory_settings(
    payload: AgentMemorySettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户的 Agent 长期记忆设置。"""
    if payload.enabled is None and payload.enabled_channels is None:
        raise HTTPException(status_code=400, detail="至少提供一个可更新字段")

    db_user = await db.get(User, int(current_user.id))
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    preferences = dict(db_user.preferences) if isinstance(db_user.preferences, dict) else {}
    memory_cfg = preferences.get(_AGENT_MEMORY_PREF_KEY)
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}

    if payload.enabled is not None:
        memory_cfg["enabled"] = bool(payload.enabled)
    if payload.enabled_channels is not None:
        memory_cfg["enabled_channels"] = _normalize_memory_channels(payload.enabled_channels)
    elif "enabled_channels" not in memory_cfg:
        memory_cfg["enabled_channels"] = _default_memory_channels()
    memory_cfg["updated_at"] = datetime.utcnow().isoformat()

    preferences[_AGENT_MEMORY_PREF_KEY] = memory_cfg
    db_user.preferences = preferences
    await db.commit()
    await db.refresh(db_user)
    return _build_memory_settings_response(db_user)


@router.delete("/agent/memory")
async def clear_agent_memory(
    channel: Optional[str] = Query(default=None, description="可选：仅清理指定 channel"),
    scope_type: Optional[str] = Query(default=None, description="可选：仅清理指定 scope_type"),
    scope_id: Optional[str] = Query(default=None, description="可选：仅清理指定 scope_id"),
    current_user: User = Depends(get_current_user),
):
    """清理当前用户的 Agent 长期记忆条目。"""
    if channel is not None and str(channel).strip() not in _AGENT_MEMORY_ALLOWED_CHANNELS:
        raise HTTPException(status_code=400, detail="非法 channel")

    runtime_service = get_agent_runtime_service()
    deleted_count = await runtime_service.clear_memories(
        user_id=int(current_user.id),
        channel=str(channel).strip() if channel else None,
        scope_type=str(scope_type).strip() if scope_type else None,
        scope_id=str(scope_id).strip() if scope_id else None,
    )
    return {
        "deleted": int(deleted_count),
        "channel": str(channel).strip() if channel else None,
        "scope_type": str(scope_type).strip() if scope_type else None,
        "scope_id": str(scope_id).strip() if scope_id else None,
    }


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
    
    return await get_agent_history(notebook_id, current_user.id)


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
    
    await clear_agent_history_state(notebook_id, current_user.id)
    
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
    await save_agent_message(notebook_id, current_user.id, user_message)
    
    async def generate_response():
        """生成流式响应"""
        try:
            from app.services.agent_tools import ToolRegistry
            from app.services.react_agent import AgentRuntimeContext, create_react_agent
            from app.services.llm_service import get_llm_service
            
            # 创建带 Notebook 上下文的工具注册表
            tool_registry = ToolRegistry(
                db=None,
                db_session_factory=async_session_factory,
                user_id=current_user.id,
                notebook_id=notebook_id,
                kernel_manager=kernel_manager,
                notebooks_store=_notebooks,
                user_authorized=request.user_authorized
            )
            
            # 获取 LLM 服务 (异步)
            llm_service = await get_llm_service()
            
            # 创建统一 Agent Core（兼容 ReActAgent 入口）
            agent = create_react_agent(
                llm_service=llm_service,
                tool_registry=tool_registry,
                max_iterations=settings.react_max_iterations,
                runtime_context=AgentRuntimeContext(
                    user_id=current_user.id,
                    channel="codelab_agent",
                    notebook_id=notebook_id,
                ),
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
            rag_metrics = None
            react_steps: List[Dict[str, Any]] = []
            current_iteration = 1
            
            # 调用 Agent - 注意: messages 构建已包含 system context
            messages = [
                {"role": "system", "content": system_context}
            ]
            
            # 获取对话历史
            history = await get_agent_history(notebook_id, current_user.id)
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
                    react_steps.append(
                        {
                            "type": "thought",
                            "iteration": current_iteration,
                            "content": event_data if isinstance(event_data, str) else str(event_data),
                        }
                    )
                    yield f"data: {json.dumps({'type': 'thought', 'content': event_data, 'iteration': current_iteration})}\n\n"
                
                elif event_type == "thinking":
                    # 流式思考内容
                    yield f"data: {json.dumps({'type': 'content', 'content': event_data})}\n\n"
                
                elif event_type == "action":
                    # data 是字典 {"tool": "...", "input": {...}}
                    tool_name = event_data.get("tool", "") if isinstance(event_data, dict) else ""
                    tool_input = event_data.get("input", {}) if isinstance(event_data, dict) else {}
                    iteration = event_data.get("iteration", current_iteration) if isinstance(event_data, dict) else current_iteration
                    try:
                        current_iteration = max(current_iteration, int(iteration))
                    except (TypeError, ValueError):
                        iteration = current_iteration
                    react_steps.append(
                        {
                            "type": "action",
                            "iteration": int(iteration),
                            "tool": tool_name,
                            "input": tool_input if isinstance(tool_input, dict) else {},
                        }
                    )
                    yield f"data: {json.dumps({'type': 'action', 'tool': tool_name, 'input': tool_input, 'iteration': int(iteration)})}\n\n"
                
                elif event_type == "observation":
                    # data 是字典 {"tool": "...", "success": ..., "output": ..., "data": ...}
                    success = event_data.get("success", False) if isinstance(event_data, dict) else False
                    output_raw = event_data.get("output", "") if isinstance(event_data, dict) else event_data
                    output = output_raw if isinstance(output_raw, str) else str(output_raw)
                    tool_data = event_data.get("data", {}) if isinstance(event_data, dict) else {}
                    iteration = event_data.get("iteration", current_iteration) if isinstance(event_data, dict) else current_iteration
                    try:
                        current_iteration = max(current_iteration, int(iteration))
                    except (TypeError, ValueError):
                        iteration = current_iteration
                    
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
                                
                                # 【新增】处理删除操作
                                deleted_ids = tool_data.get('deleted_ids', []) if isinstance(tool_data, dict) else []
                                if deleted_ids:
                                    for del_id in deleted_ids:
                                        try:
                                            await service.delete_cell(notebook_id, user_id, del_id)
                                            logger.info(f"[Agent] Cell 删除已同步到数据库: {del_id}")
                                        except Exception as del_e:
                                            logger.warning(f"删除 cell {del_id} 失败: {del_e}")
                                    
                        except Exception as e:
                            logger.warning(f"同步到数据库失败: {e}")
                    
                    react_steps.append(
                        {
                            "type": "observation",
                            "iteration": int(iteration),
                            "tool": event_data.get("tool", "") if isinstance(event_data, dict) else "",
                            "output": output,
                            "success": success,
                        }
                    )

                    yield f"data: {json.dumps({'type': 'observation', 'success': success, 'output': output, 'notebook_updated': notebook_updated, 'cell_id': cell_id, 'new_cell': new_cell, 'updated_cell': updated_cell, 'iteration': int(iteration)})}\n\n"
                
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
                    if isinstance(event_data, dict) and isinstance(event_data.get("rag_metrics"), dict):
                        rag_metrics = event_data["rag_metrics"]
                
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
                metadata={"rag_metrics": rag_metrics} if isinstance(rag_metrics, dict) else {},
            )
            if react_steps:
                assistant_message.metadata["react_steps"] = react_steps
            await save_agent_message(notebook_id, current_user.id, assistant_message)
            
            # 发送完成事件
            done_payload = {"type": "done", "code_blocks": code_blocks}
            if isinstance(rag_metrics, dict):
                done_payload["rag_metrics"] = rag_metrics
            if react_steps:
                done_payload["react_steps"] = react_steps
            yield f"data: {json.dumps(done_payload)}\n\n"
            
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
