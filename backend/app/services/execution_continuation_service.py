from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc, select

from app.config import settings
from app.core.database import async_session_factory
from app.models.conversation import Conversation, Message, MessageRole, MessageType
from app.models.user import User
from app.services.agent_runtime_service import get_agent_runtime_service
from app.services.agent_tools import get_tool_registry
from app.services.conversation_context_compaction_service import get_conversation_context_compaction_service
from app.services.llm_service import LLMService
from app.services.project_runtime_service import ProjectRuntimeService
from app.services.react_agent import AgentRuntimeContext, create_react_agent


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def _assistant_summary_text(content: str, *, fallback: Optional[str] = None, limit: int = 160) -> Optional[str]:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        text = " ".join(str(fallback or "").split()).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _json_safe_payload(payload: Any) -> Any:
    try:
        return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        return str(payload)


@dataclass
class ExecutionContinuationRecord:
    key: str
    user_id: int
    conversation_id: int
    project_id: int
    execution_id: str
    stage: str
    purpose: str
    scheduled_at: str = field(default_factory=_utcnow_iso)
    active_skill_names: List[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)


class ExecutionContinuationManager:
    _TERMINAL_STATUSES = {"completed", "failed", "blocked", "cancelled", "completed_or_unknown"}
    _MAX_AUTO_RESUME_COUNT = 8
    _POLL_INTERVAL_SECONDS = 2.0

    def __init__(self) -> None:
        self._tasks: Dict[str, ExecutionContinuationRecord] = {}
        self._lock = asyncio.Lock()

    async def schedule(
        self,
        *,
        user_id: int,
        conversation_id: Optional[int],
        project_id: int,
        execution_id: str,
        stage: Optional[str],
        purpose: Optional[str],
        active_skill_names: Optional[List[str]] = None,
    ) -> None:
        normalized_execution_id = str(execution_id or "").strip()
        normalized_stage = str(stage or "").strip().lower()
        if int(user_id or 0) <= 0 or int(project_id or 0) <= 0 or not normalized_execution_id:
            return
        if conversation_id is None or int(conversation_id) <= 0:
            return

        key = f"{int(conversation_id)}:{normalized_execution_id}"
        async with self._lock:
            existing = self._tasks.get(key)
            if existing and existing.task is not None and not existing.task.done():
                return
            record = ExecutionContinuationRecord(
                key=key,
                user_id=int(user_id),
                conversation_id=int(conversation_id),
                project_id=int(project_id),
                execution_id=normalized_execution_id,
                stage=normalized_stage,
                purpose=str(purpose or "").strip(),
                active_skill_names=[
                    str(item or "").strip()
                    for item in list(active_skill_names or [])
                    if str(item or "").strip()
                ],
            )
            record.task = asyncio.create_task(
                self._watch_and_continue(record),
                name=f"execution-continuation:{key}",
            )
            self._tasks[key] = record

    async def _watch_and_continue(self, record: ExecutionContinuationRecord) -> None:
        runtime_service = ProjectRuntimeService()
        try:
            workspace_dir = await self._resolve_workspace_dir(record.project_id, record.user_id)
            if workspace_dir is None:
                logger.warning(
                    "[ExecutionContinuation] workspace missing: conversation_id={}, project_id={}, execution_id={}",
                    record.conversation_id,
                    record.project_id,
                    record.execution_id,
                )
                return

            execution_payload: Optional[Dict[str, Any]] = None
            while True:
                execution_payload = await runtime_service.get_execution(
                    workspace_dir=workspace_dir,
                    project_id=record.project_id,
                    execution_id=record.execution_id,
                    include_logs=True,
                    max_log_chars=24000,
                )
                status = self._normalized_execution_status(execution_payload)
                if status in self._TERMINAL_STATUSES:
                    break
                await asyncio.sleep(self._POLL_INTERVAL_SECONDS)

            if execution_payload is None:
                return
            if await self._should_skip_due_to_newer_user_message(record):
                logger.info(
                    "[ExecutionContinuation] skip because newer user message exists: conversation_id={}, execution_id={}",
                    record.conversation_id,
                    record.execution_id,
                )
                return
            await self._run_continuation(record, execution_payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "[ExecutionContinuation] failed: conversation_id={}, execution_id={}, error={}",
                record.conversation_id,
                record.execution_id,
                exc,
            )
        finally:
            async with self._lock:
                self._tasks.pop(record.key, None)

    async def _run_continuation(self, record: ExecutionContinuationRecord, execution_payload: Dict[str, Any]) -> None:
        runtime_service = get_agent_runtime_service()
        state = dict(await runtime_service.get_conversation_context_state(record.conversation_id) or {})
        resume_count = int(state.get("auto_execution_resume_count") or 0)
        if resume_count >= self._MAX_AUTO_RESUME_COUNT:
            logger.warning(
                "[ExecutionContinuation] skip because max resume count reached: conversation_id={}, execution_id={}, count={}",
                record.conversation_id,
                record.execution_id,
                resume_count,
            )
            return

        state["auto_execution_resume_count"] = resume_count + 1
        state["auto_execution_resumed_at"] = _utcnow_iso()
        active_skill_names = [
            str(item or "").strip()
            for item in list(state.get("active_skill_names") or record.active_skill_names or [])
            if str(item or "").strip()
        ]
        if "paper-reproduction" not in active_skill_names:
            active_skill_names.append("paper-reproduction")
        state["active_skill_names"] = active_skill_names
        await runtime_service.upsert_conversation_context_state(record.conversation_id, state)

        async with async_session_factory() as db:
            conversation = await db.get(Conversation, int(record.conversation_id))
            user = await db.get(User, int(record.user_id))
            if conversation is None or user is None or int(conversation.user_id) != int(record.user_id):
                return

            llm_provider = str(conversation.llm_provider or user.preferred_llm_provider or "").strip()
            llm_service = LLMService(llm_provider)
            tool_registry = get_tool_registry(
                db=None,
                user_id=int(record.user_id),
                db_session_factory=async_session_factory,
                conversation_id=int(record.conversation_id),
                route_profile="chat",
                initialize_mcp=False,
            )
            turn_id = f"turn:auto_exec:{record.execution_id}:{uuid.uuid4().hex[:8]}"
            prompt = self._build_continuation_prompt(record=record, execution_payload=execution_payload)

            await runtime_service.upsert_conversation_turn_entry(
                int(record.conversation_id),
                {
                    "turn_id": turn_id,
                    "status": "running",
                    "user_message_id": None,
                    "user_content": prompt,
                    "started_at": _utcnow_iso(),
                    "source": "auto_execution_continuation",
                    "trigger_execution_id": record.execution_id,
                },
            )
            await runtime_service.append_conversation_item_entries(
                int(record.conversation_id),
                [
                    {
                        "kind": "history_event",
                        "turn_id": turn_id,
                        "role": "system",
                        "summary": f"自动 continuation：{record.execution_id}",
                        "content": prompt,
                        "metadata": {
                            "source": "auto_execution_continuation",
                            "trigger_execution_id": record.execution_id,
                            "execution_status": self._normalized_execution_status(execution_payload),
                        },
                        "created_at": _utcnow_iso(),
                    }
                ],
            )

            agent = create_react_agent(
                llm_service,
                tool_registry,
                max_iterations=settings.react_max_iterations,
                runtime_context=AgentRuntimeContext(
                    user_id=int(record.user_id),
                    channel="chat",
                    conversation_id=int(record.conversation_id),
                    turn_id=turn_id,
                    active_skill_names=active_skill_names,
                ),
            )

            full_content = ""
            thought = ""
            reasoning_summary = None
            current_iteration = 0
            run_id: Optional[str] = None
            rag_metrics: Optional[Dict[str, Any]] = None
            citation_index: Optional[Dict[str, Any]] = None
            last_error_text: Optional[str] = None

            try:
                async for event in agent.run([{"role": "user", "content": prompt}], stream=True):
                    event_type = str(event.get("type") or "")
                    event_data = event.get("data")
                    if event_type == "thinking_start":
                        current_iteration += 1
                    elif event_type == "thought":
                        thought = str(event_data or "")
                    elif event_type == "content":
                        full_content += str(event_data or "")
                    elif event_type == "answer":
                        full_content = str(event_data or "")
                    elif event_type == "done" and isinstance(event_data, dict):
                        run_id = str(event_data.get("run_id") or "").strip() or run_id
                        if event_data.get("thought"):
                            thought = str(event_data.get("thought") or "")
                        if event_data.get("answer") and not full_content:
                            full_content = str(event_data.get("answer") or "")
                        if isinstance(event_data.get("reasoning_summary"), str):
                            reasoning_summary = str(event_data.get("reasoning_summary") or "").strip() or None
                        if isinstance(event_data.get("rag_metrics"), dict):
                            rag_metrics = dict(event_data.get("rag_metrics") or {})
                        if isinstance(event_data.get("citation_index"), dict):
                            citation_index = dict(event_data.get("citation_index") or {})
                    elif event_type == "error":
                        last_error_text = str(event_data or "").strip() or None

                if not str(full_content or "").strip():
                    if last_error_text:
                        full_content = f"自动 continuation 读取 execution `{record.execution_id}` 时出错：{last_error_text}"
                    else:
                        full_content = (
                            f"已读取 execution `{record.execution_id}` 的终态结果，但本轮没有生成可见结论。"
                            "请继续当前论文任务，我会基于 execution 结果继续处理。"
                        )

                message_metadata = _json_safe_payload(
                    {
                        "auto_continuation": {
                            "trigger_execution_id": record.execution_id,
                            "execution_stage": record.stage,
                            "execution_status": self._normalized_execution_status(execution_payload),
                        },
                        "rag_metrics": rag_metrics,
                        "citation_index": citation_index,
                    }
                )
                assistant_message = Message(
                    conversation_id=int(record.conversation_id),
                    role=MessageRole.ASSISTANT,
                    content=full_content,
                    message_type=MessageType.TEXT,
                    thought=(reasoning_summary or thought) if (reasoning_summary or thought) else None,
                    metadata_=message_metadata if isinstance(message_metadata, dict) else None,
                )
                db.add(assistant_message)
                await db.commit()
                await db.refresh(assistant_message)
                await runtime_service.append_conversation_item_entries(
                    int(record.conversation_id),
                    [
                        {
                            "kind": "assistant_message",
                            "turn_id": turn_id,
                            "role": "assistant",
                            "content": full_content,
                            "message_id": int(assistant_message.id),
                            "created_at": assistant_message.created_at.isoformat() if assistant_message.created_at else _utcnow_iso(),
                            "thought": (reasoning_summary or thought) if (reasoning_summary or thought) else None,
                            "metadata": message_metadata if isinstance(message_metadata, dict) else {},
                        }
                    ],
                )
                summary_text = _assistant_summary_text(reasoning_summary or full_content, fallback=thought, limit=240)
                if summary_text:
                    await runtime_service.append_conversation_item_entries(
                        int(record.conversation_id),
                        [
                            {
                                "kind": "reasoning_summary",
                                "turn_id": turn_id,
                                "role": "assistant",
                                "message_id": int(assistant_message.id),
                                "run_id": run_id,
                                "iteration": max(int(current_iteration or 0), 0),
                                "summary": summary_text,
                                "content": summary_text,
                                "created_at": assistant_message.created_at.isoformat() if assistant_message.created_at else _utcnow_iso(),
                            }
                        ],
                    )
                await runtime_service.upsert_conversation_turn_entry(
                    int(record.conversation_id),
                    {
                        "turn_id": turn_id,
                        "status": "completed",
                        "assistant_message_id": int(assistant_message.id),
                        "assistant_summary": _assistant_summary_text(full_content, fallback=(reasoning_summary or thought)),
                        "run_id": run_id,
                        "iteration_count": current_iteration,
                        "completed_at": assistant_message.created_at.isoformat() if assistant_message.created_at else _utcnow_iso(),
                    },
                )
                get_conversation_context_compaction_service().enqueue_conversation(int(record.conversation_id))
            except Exception as exc:
                await runtime_service.upsert_conversation_turn_entry(
                    int(record.conversation_id),
                    {
                        "turn_id": turn_id,
                        "status": "error",
                        "assistant_summary": f"自动 continuation 失败: {type(exc).__name__}",
                        "error_message": str(exc),
                        "completed_at": _utcnow_iso(),
                    },
                )
                raise

    async def _resolve_workspace_dir(self, project_id: int, user_id: int) -> Optional[Path]:
        from app.services.project_service import ProjectService
        from app.services.notebook_workspace_service import get_notebook_workspace_dir

        project_service = ProjectService()
        payload = await project_service.get_project_payload(project_id=int(project_id), user_id=int(user_id))
        if not isinstance(payload, dict):
            return None
        notebook = payload.get("notebook")
        notebook_id = str((notebook or {}).get("id") or "").strip()
        if not notebook_id:
            return None
        return Path(get_notebook_workspace_dir(notebook_id, int(user_id)))

    async def _should_skip_due_to_newer_user_message(self, record: ExecutionContinuationRecord) -> bool:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Message.id)
                .where(
                    Message.conversation_id == int(record.conversation_id),
                    Message.role == MessageRole.USER,
                    Message.created_at > datetime.fromisoformat(record.scheduled_at),
                )
                .order_by(desc(Message.created_at), desc(Message.id))
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    @staticmethod
    def _normalized_execution_status(payload: Dict[str, Any]) -> str:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        status = str(result.get("status") or payload.get("status") or "").strip().lower()
        return status or "unknown"

    @staticmethod
    def _build_continuation_prompt(
        *,
        record: ExecutionContinuationRecord,
        execution_payload: Dict[str, Any],
    ) -> str:
        status = ExecutionContinuationManager._normalized_execution_status(execution_payload)
        result = execution_payload.get("result") if isinstance(execution_payload.get("result"), dict) else {}
        result_exists = bool(result.get("result_exists"))
        lines = [
            "继续当前 paper-reproduction 任务。",
            f"- project_id: {record.project_id}",
            f"- execution_id: {record.execution_id}",
            f"- stage: {record.stage or 'execution'}",
            f"- purpose: {record.purpose or 'execution'}",
            f"- status: {status}",
        ]
        if result_exists:
            lines.append("- 执行结果和日志已经可读。")
        lines.extend(
            [
                "先读取这次 execution 的 result 和 log，再决定下一步。",
                "如果是可修复的前置阻塞，先修复并继续原任务。",
                "如果需要用户决策，再清楚说明当前阻塞和建议方案。",
                "不要重新 prepare / intake / implementation planning，除非现有 artifact 明确缺失或损坏。",
            ]
        )
        return "\n".join(lines)


_execution_continuation_manager = ExecutionContinuationManager()


def get_execution_continuation_manager() -> ExecutionContinuationManager:
    return _execution_continuation_manager
