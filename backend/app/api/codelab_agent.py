"""CodeLab Agent 路由拆分模块。"""
import json
import re
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
    build_history_summary,
    clear_history as clear_history_in_db,
    get_cached_history_summary,
    load_history,
)
from app.services.notebook_service import NotebookService
from app.services.notebook_workspace_service import build_notebook_workspace_context

router = APIRouter()

# 从主模块复用共享上下文，避免破坏既有导入点。
get_notebook_cached = codelab_base.get_notebook_cached
kernel_manager = codelab_base.kernel_manager
_notebooks = codelab_base._notebooks
_notebooks_cache = codelab_base._notebooks

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
    active_cell_id: Optional[str] = None
    active_cell_index: Optional[int] = None  # 0-based index from frontend


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


def _truncate_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text)
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _summarize_code_kind(source: str, cell_type: str) -> str:
    if cell_type != "code":
        return "markdown"

    normalized = str(source or "").lower()
    if not normalized.strip():
        return "empty_code"
    if any(line.strip().startswith(("import ", "from ")) for line in normalized.splitlines()):
        if not normalized.replace("import ", "").replace("from ", "").strip():
            return "imports"
    if any(token in normalized for token in ("read_csv(", "read_excel(", "read_parquet(", "read_json(", "load_iris(", "fetch_", "dataframe(")):
        return "data_loading"
    if any(token in normalized for token in ("train_test_split", "fillna(", "dropna(", "astype(", "get_dummies(", "standardscaler", "labelencoder", "merge(", "concat(", "groupby(")):
        return "feature_processing"
    if any(token in normalized for token in (".fit(", "randomforest", "logisticregression", "svc(", "xgb", "lgbm", "linearregression", "kmeans(")):
        return "model_training"
    if any(token in normalized for token in ("predict(", "score(", "accuracy_score", "classification_report", "confusion_matrix", "mean_squared_error", "roc_auc_score")):
        return "evaluation"
    if any(token in normalized for token in ("plt.", "sns.", ".plot(", "scatter(", "hist(", "bar(", "imshow(", "heatmap(")):
        return "visualization"
    if any(token in normalized for token in ("def ", "class ")):
        return "helper_definition"
    if any(line.strip().startswith(("import ", "from ")) for line in normalized.splitlines()):
        return "imports"
    return "general_python"


def _summarize_outputs(outputs: Any, limit: int = 160) -> List[str]:
    items: List[str] = []
    for output in list(outputs or [])[:2]:
        if not isinstance(output, dict):
            continue
        output_type = str(output.get("output_type") or "")
        content = output.get("content")
        if output_type == "error" and isinstance(content, dict):
            items.append(
                _truncate_text(
                    f"{content.get('ename', 'Error')}: {content.get('evalue', '')}",
                    limit=limit,
                )
            )
        elif output_type == "display_data" and str(output.get("mime_type") or "").startswith("image/"):
            items.append("生成了图像输出")
        else:
            items.append(_truncate_text(content, limit=limit))
    return [item for item in items if item]


def _summarize_cell(cell: Dict[str, Any], index: int, max_source_length: int) -> Dict[str, Any]:
    outputs = list(cell.get("outputs") or [])
    output_summaries = _summarize_outputs(outputs)
    error_summary = next((item for item in output_summaries if ":" in item and ("error" in item.lower() or "warning" in item.lower())), None)
    status = "idle"
    if error_summary:
        status = "error"
    elif outputs:
        status = "has_output"
    elif cell.get("execution_count") is not None:
        status = "executed"

    source = str(cell.get("source") or "")
    return {
        "cell_id": str(cell.get("id") or ""),
        "cell_index": index,
        "label": f"Cell {index + 1}",
        "cell_type": str(cell.get("cell_type") or "code"),
        "kind": _summarize_code_kind(source, str(cell.get("cell_type") or "code")),
        "source_excerpt": _truncate_text(source, limit=max_source_length),
        "execution_count": cell.get("execution_count"),
        "has_output": bool(outputs),
        "status": status,
        "output_summary": output_summaries[0] if output_summaries else "",
        "error_summary": error_summary or "",
    }


def _resolve_focus_context(
    notebook: Dict[str, Any],
    *,
    active_cell_id: Optional[str],
    active_cell_index: Optional[int],
    max_source_length: int,
) -> Dict[str, Optional[Dict[str, Any]]]:
    cells = list(notebook.get("cells") or [])
    active: Optional[Dict[str, Any]] = None

    if active_cell_id:
        for idx, cell in enumerate(cells):
            if str(cell.get("id") or "") == str(active_cell_id):
                active = _summarize_cell(cell, idx, max_source_length)
                break

    if active is None and isinstance(active_cell_index, int) and 0 <= active_cell_index < len(cells):
        active = _summarize_cell(cells[active_cell_index], active_cell_index, max_source_length)

    recent_error = None
    recent_output = None
    recent_executed = None
    for idx in range(len(cells) - 1, -1, -1):
        summary = _summarize_cell(cells[idx], idx, max_source_length)
        if recent_error is None and summary["status"] == "error":
            recent_error = summary
        if recent_output is None and summary["has_output"]:
            recent_output = summary
        if recent_executed is None and summary["execution_count"] is not None:
            recent_executed = summary
        if recent_error and recent_output and recent_executed:
            break

    return {
        "active_cell": active,
        "recent_error": recent_error,
        "recent_output": recent_output,
        "recent_executed": recent_executed,
    }


def _extract_import_roots(cells: List[Dict[str, Any]]) -> List[str]:
    roots: List[str] = []
    seen = set()
    for cell in cells:
        source = str(cell.get("source") or "")
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            root = parts[1].split(".")[0]
            if root and root not in seen:
                seen.add(root)
                roots.append(root)
    return roots


def _build_stage_summary(cells: List[Dict[str, Any]], focus: Dict[str, Optional[Dict[str, Any]]]) -> str:
    kinds = {
        _summarize_code_kind(str(cell.get("source") or ""), str(cell.get("cell_type") or "code"))
        for cell in cells
        if str(cell.get("cell_type") or "code") == "code"
    }
    imports = _extract_import_roots(cells)
    parts: List[str] = []
    if imports:
        parts.append(f"已引入 {', '.join(imports[:4])}")
    if "data_loading" in kinds:
        parts.append("已有数据加载/构造步骤")
    if "feature_processing" in kinds:
        parts.append("已有数据预处理逻辑")
    if "model_training" in kinds:
        parts.append("已有模型训练逻辑")
    if "evaluation" in kinds:
        parts.append("已有评估逻辑")
    if "visualization" in kinds:
        parts.append("已有可视化逻辑")
    if focus.get("recent_error"):
        parts.append("当前更适合优先处理最近报错")
    elif focus.get("recent_executed"):
        parts.append("最近执行结果可作为下一步依据")
    return "；".join(parts) if parts else "Notebook 仍处于较早阶段，建议先确认当前焦点单元格和任务目标。"


def _build_history_context(history: Optional[Dict[str, Any]], recent_limit: int = 4) -> Dict[str, Any]:
    raw_messages = list((history or {}).get("messages", [])) if isinstance(history, dict) else []
    normalized: List[Dict[str, str]] = []
    for item in raw_messages:
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _truncate_text(item.get("content"), limit=220)
        if not content:
            continue
        normalized.append({"role": role, "content": content})

    recent = normalized[-recent_limit:] if len(normalized) > recent_limit else normalized
    summary = get_cached_history_summary(history, recent_limit=recent_limit) if isinstance(history, dict) else ""
    if not summary:
        summary = build_history_summary(raw_messages, recent_limit=recent_limit)

    recent_messages = [{"role": item["role"], "content": item["content"]} for item in recent]
    return {
        "summary": summary,
        "recent_messages": recent_messages,
    }


def _build_tool_hints(
    focus: Dict[str, Optional[Dict[str, Any]]],
    *,
    include_variables: bool,
    user_authorized: bool,
    workspace: Optional[Dict[str, Any]] = None,
) -> List[str]:
    hints = [
        "需要核对单元格源码、输出或报错细节时，先用 notebook_cell(get_one/get)。",
        "在 observation 返回前，不要假设代码已经执行过或变量一定存在。",
    ]
    recent_error = focus.get("recent_error")
    if isinstance(recent_error, dict):
        hints.insert(
            0,
            f"修复最近报错时，优先直接覆盖 {recent_error.get('label')}#{int(recent_error.get('cell_index', 0)) + 1}，不要再创建一个重复的修复版 cell。",
        )
    active_cell = focus.get("active_cell")
    if isinstance(active_cell, dict):
        hints.insert(
            1 if isinstance(recent_error, dict) else 0,
            f"当前焦点可先查 notebook_cell(action='get_one', cell_index={int(active_cell.get('cell_index', 0)) + 1})。",
        )
    if include_variables:
        hints.append("需要确认 DataFrame、模型或数组状态时，再用 notebook_variables。")
    workspace_files = list((workspace or {}).get("file_names") or [])
    if workspace_files:
        hints.append(
            f"当前工作区已有 {len(workspace_files)} 个上传文件；先用 list_uploaded_files() 或直接用相对路径确认文件名，再用 uploaded_file_path('{workspace_files[0]}') / read_uploaded_text(...)。不要导入 os 去枚举目录。"
        )
        hints.append(
            f"处理上传文件时先写最小可验证代码，例如 `df = pd.read_csv('{workspace_files[0]}')`、`print(df.shape)`、`print(df.head())`；不要一开始就写多层 try/except 或备用文件名猜测。"
        )
        hints.append(
            f"`uploaded_file_path('{workspace_files[0]}')` 必须作为函数调用使用，不要把它写成字符串 `'uploaded_file_path({workspace_files[0]})'`。"
        )
    if user_authorized:
        hints.append("只有在确实需要验证结果或修改 Notebook 时，再调用 notebook_execute；修复已有单元格时优先传 cell_index/cell_id 覆盖原单元格。")
    return hints


def _build_notebook_agent_context(
    notebook_id: str,
    notebook: Dict[str, Any],
    *,
    include_variables: bool,
    active_cell_id: Optional[str] = None,
    active_cell_index: Optional[int] = None,
    history: Optional[Dict[str, Any]] = None,
    user_authorized: bool = False,
    workspace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cells = list(notebook.get("cells") or [])
    max_cell_length = max(int(getattr(settings, "notebook_context_cell_max_length", 200)), 80)
    focus = _resolve_focus_context(
        notebook,
        active_cell_id=active_cell_id,
        active_cell_index=active_cell_index,
        max_source_length=max_cell_length,
    )

    code_cells = [cell for cell in cells if str(cell.get("cell_type") or "code") == "code"]
    recent_cell_summaries = [
        _summarize_cell(cell, idx, max_cell_length)
        for idx, cell in list(enumerate(cells))[-max(int(getattr(settings, "notebook_context_cells", 5)), 1):]
    ]

    kernel = kernel_manager.get_kernel(notebook_id)
    variables: Dict[str, str] = {}
    if include_variables and kernel:
        try:
            raw_variables = kernel.get_variables() or {}
            var_items = list(raw_variables.items())[: max(int(getattr(settings, "notebook_context_variables", 15)), 1)]
            variables = {str(key): _truncate_text(value, limit=140) for key, value in var_items}
        except Exception as exc:
            logger.warning(f"[CodeLabContext] load variables failed notebook_id={notebook_id}: {exc}")

    recent_outputs = []
    output_limit = max(int(getattr(settings, "notebook_context_output_cells", 5)), 1)
    for idx, cell in list(enumerate(cells))[-output_limit:]:
        outputs = list(cell.get("outputs") or [])
        if not outputs:
            continue
        recent_outputs.append(
            {
                "cell_id": str(cell.get("id") or ""),
                "cell_index": idx,
                "execution_count": cell.get("execution_count"),
                "outputs": outputs[:2],
                "summary": _summarize_outputs(outputs),
            }
        )

    history_context = _build_history_context(history)
    stage_summary = _build_stage_summary(cells, focus)
    imports = _extract_import_roots(code_cells)
    code_summary_parts = [f"{len(code_cells)} 个代码单元格"]
    if imports:
        code_summary_parts.append(f"主要库: {', '.join(imports[:5])}")
    code_summary_parts.append(stage_summary)

    return {
        "notebook_id": notebook_id,
        "notebook_title": notebook.get("title", "未命名"),
        "cell_count": len(cells),
        "code_cell_count": len(code_cells),
        "execution_count": notebook.get("execution_count", 0),
        "variables": variables,
        "recent_outputs": recent_outputs,
        "recent_cells": recent_cell_summaries,
        "focus": focus,
        "history_summary": history_context["summary"],
        "recent_history_messages": history_context["recent_messages"],
        "stage_summary": stage_summary,
        "code_summary": "；".join([part for part in code_summary_parts if part]),
        "workspace": {
            "directory": str((workspace or {}).get("directory") or ""),
            "display_path": str((workspace or {}).get("display_path") or ""),
            "file_count": int((workspace or {}).get("file_count") or 0),
            "files": list((workspace or {}).get("files") or [])[:8],
        },
        "tool_hints": _build_tool_hints(
            focus,
            include_variables=include_variables,
            user_authorized=user_authorized,
            workspace=workspace,
        ),
    }


def _render_notebook_system_context(
    context_payload: Dict[str, Any],
    *,
    include_context: bool,
    include_variables: bool,
    user_authorized: bool,
) -> str:
    tool_hints = [str(item).strip() for item in list(context_payload.get("tool_hints") or []) if str(item).strip()]
    lines = [
        "你是 CodeLab 的专业数据科学助手。",
        "默认先围绕当前焦点回答；信息不够时先调用 Notebook 工具核实，再下结论。",
        (
            f"Notebook: {context_payload.get('notebook_title')} "
            f"(ID={context_payload.get('notebook_id')}, cells={context_payload.get('cell_count', 0)}, "
            f"code={context_payload.get('code_cell_count', 0)}, exec={context_payload.get('execution_count', 0)})"
        ),
    ]

    if include_context:
        lines.append(f"阶段: {context_payload.get('stage_summary') or context_payload.get('code_summary')}")

        focus = context_payload.get("focus") or {}
        focus_parts: List[str] = []
        recent_error = focus.get("recent_error")
        if isinstance(recent_error, dict):
            focus_parts.append(
                f"最近报错={recent_error.get('label')}#{int(recent_error.get('cell_index', 0)) + 1}: "
                f"{recent_error.get('error_summary') or recent_error.get('source_excerpt')}"
            )
        active_cell = focus.get("active_cell")
        if isinstance(active_cell, dict):
            focus_parts.append(
                f"当前单元格={active_cell.get('label')}#{int(active_cell.get('cell_index', 0)) + 1}"
                f"[{active_cell.get('kind')}]: {active_cell.get('source_excerpt')}"
            )
        recent_output = focus.get("recent_output")
        if isinstance(recent_output, dict):
            focus_parts.append(
                f"最近输出={recent_output.get('label')}#{int(recent_output.get('cell_index', 0)) + 1}: "
                f"{recent_output.get('output_summary') or recent_output.get('source_excerpt')}"
            )
        if focus_parts:
            lines.append("焦点: " + "；".join(focus_parts[:3]))

        history_summary = str(context_payload.get("history_summary") or "").strip()
        if history_summary:
            lines.append("更早任务: " + history_summary)

        workspace = context_payload.get("workspace") or {}
        workspace_files = list(workspace.get("files") or [])
        if workspace_files:
            file_names = ", ".join(str(item.get("name") or "") for item in workspace_files[:5] if str(item.get("name") or "").strip())
            lines.append(
                f"工作区: {int(workspace.get('file_count') or len(workspace_files))} 个上传文件"
                + (f"（{file_names}）" if file_names else "")
                + "；Notebook 内可直接用相对路径、uploaded_file_path(name) 或 read_uploaded_text(name)。"
            )

    if tool_hints:
        lines.append("工具策略: " + " ".join(tool_hints))

    if include_variables and context_payload.get("variables"):
        variable_parts = [
            f"{key}={value}"
            for key, value in list((context_payload.get("variables") or {}).items())[:8]
        ]
        if variable_parts:
            lines.append("变量快照: " + " | ".join(variable_parts))

    lines.append(
        "授权状态: " + (
            "已授权，可直接操作 Notebook"
            if user_authorized
            else "未授权，只能给建议，不能直接执行或改写 Notebook"
        )
    )
    return "\n".join(lines).strip()


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
    active_cell_id: Optional[str] = Query(default=None, description="可选：当前聚焦的 cell id"),
    active_cell_index: Optional[int] = Query(default=None, description="可选：当前聚焦的 0-based cell index"),
    include_variables: bool = Query(default=True, description="是否包含变量快照"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取 Notebook 上下文供 Agent 使用"""
    notebook = await get_notebook_cached(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook 不存在")

    history = await get_agent_history(notebook_id, current_user.id)
    workspace = build_notebook_workspace_context(notebook_id, current_user.id)
    return _build_notebook_agent_context(
        notebook_id,
        notebook,
        include_variables=include_variables,
        active_cell_id=active_cell_id,
        active_cell_index=active_cell_index,
        history=history,
        user_authorized=False,
        workspace=workspace,
    )


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
    
    history_before_user = await get_agent_history(notebook_id, current_user.id)
    workspace = build_notebook_workspace_context(notebook_id, current_user.id)

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
                user_authorized=request.user_authorized,
                route_profile="codelab",
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
            
            context_payload = _build_notebook_agent_context(
                notebook_id,
                notebook,
                include_variables=request.include_variables,
                active_cell_id=request.active_cell_id,
                active_cell_index=request.active_cell_index,
                history=history_before_user,
                user_authorized=request.user_authorized,
                workspace=workspace,
            )
            system_context = _render_notebook_system_context(
                context_payload,
                include_context=request.include_context,
                include_variables=request.include_variables,
                user_authorized=request.user_authorized,
            )
            
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
            
            for msg in context_payload.get("recent_history_messages", []):
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
