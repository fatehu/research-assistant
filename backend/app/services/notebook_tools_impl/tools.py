"""
Notebook 专用工具集 - 让 Agent 能够直接操作 Notebook

工具列表:
1. NotebookExecuteTool - 在 Notebook 内核中执行代码
2. NotebookVariablesTool - 获取当前变量状态
3. NotebookCellTool - 操作单元格 (添加/删除/更新) - 【增强版：支持索引操作】
4. NotebookCellCleanupTool - 智能清理单元格 【新增】
5. PipInstallTool - 安装 Python 包
6. WebScrapeTool - 爬取网页内容
7. CodeAnalysisTool - 代码分析和优化建议
"""
import json
import re
import sys
import asyncio
import math
import subprocess
import time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime
from loguru import logger
import httpx
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from difflib import SequenceMatcher

# 尝试导入 bs4，如果失败则在使用时报错
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

from app.config import settings
from app.core.database import async_session_factory
from app.services.agent_tools import Tool, ToolResult
from app.services.codelab_sandbox_policy import (
    ALLOWED_PACKAGES,
    SANDBOX_FORBIDDEN_IMPORT_ROOTS,
    extract_sandbox_blocked_imports,
)
from app.services.notebook_background_execution_service import (
    NotebookBackgroundExecutionBusyError,
    get_notebook_background_execution_manager,
)
from app.services.notebook_service import NotebookService
from app.services.notebook_workspace_service import build_notebook_workspace_context


# ========== 安全配置 ==========

# 网页爬取黑名单域名
BLOCKED_DOMAINS = {
    'localhost', '127.0.0.1', '0.0.0.0',
    'internal', 'intranet', 'corp', 'private',
}


def _workspace_context_for_notebook_payload(notebook: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    notebook_id = str(notebook.get("id") or "").strip()
    user_id = notebook.get("user_id")
    if not notebook_id or user_id is None:
        return None
    return build_notebook_workspace_context(notebook_id, int(user_id))


# ========== 工具实现 ==========

class NotebookExecuteTool(Tool):
    """
    在 Notebook 内核中执行 Python 代码
    
    这是最核心的工具，让 Agent 能够直接操控 Notebook 环境
    执行后可追加新 Cell，或直接覆盖已有 Cell 并保存结果
    """
    name = "notebook_execute"
    description = """Execute Python code in CodeLab's persistent notebook kernel.
Variables persist across calls.

Injected uploaded-file helpers:
- list_uploaded_files(): returns uploaded file names for the current notebook.
- uploaded_file_path(name): returns a readable path for an uploaded file.
- read_uploaded_text(name, encoding='utf-8'): reads an uploaded text file.
- NOTEBOOK_FILES / NOTEBOOK_FILE_PATHS / NOTEBOOK_FILES_DIR: uploaded names, path map, and workspace directory.

Hard file-access rules:
- Do not import os, pathlib, glob, shutil, subprocess, or sys to discover uploaded files.
- Do not use os.listdir, os.path, pathlib.Path, glob.glob, subprocess, or shell commands for uploaded files.
- For uploaded files, use relative file names or the injected helpers, for example:
  files = list_uploaded_files(); df = pd.read_csv(uploaded_file_path(files[0])).
- Do not fake file names, columns, execution results, metrics, or plots.

Use this tool to run data analysis code, create charts, test code snippets, or fix an existing failing cell.
For long ML training / hyperparameter search / multi-model evaluation, prefer run_mode=background. If the returned observation contains completed outputs, use those outputs and continue the notebook task; only treat a still-running background execution as a parked long task.
If fixing the current/latest failing cell, prefer updating it with cell_id; cell_index is compatibility-only.
Do not create duplicate code cells. If the code already exists in an unexecuted AI-created draft cell, execute/update that cell with cell_id or let write_mode=auto reuse the matching draft.
This action requires user authorization."""
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
            },
            "cell_id": {
                "type": "string",
                "description": "可选：要覆盖的目标单元格 ID。首选此字段；提供后会直接更新该单元格。"
            },
            "cell_index": {
                "type": "integer",
                "description": "可选：要覆盖的目标单元格位置索引，从1开始。仅兼容旧调用；如果同时提供 cell_id 和 cell_index，以 cell_id 为准。"
            },
            "write_mode": {
                "type": "string",
                "enum": ["auto", "append", "replace"],
                "description": "写回模式。append=总是新增，replace=优先覆盖目标/最近报错单元格，auto=自动判断，默认 auto。"
            },
            "run_mode": {
                "type": "string",
                "enum": ["auto", "foreground", "background"],
                "description": "执行模式。background 会将长任务转为后台执行并稍后写回 cell；auto 会对训练/搜索类长任务自动走后台。"
            }
        },
        "required": ["code"]
    }
    
    def __init__(self, kernel_manager, notebook_id: str, notebooks_store: dict = None, user_authorized: bool = False):
        self.kernel_manager = kernel_manager
        self.notebook_id = notebook_id
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized
    
    @staticmethod
    def _resolve_target_cell(notebook: dict, cell_id: str = None, cell_index: int = None):
        cells = list(notebook.get('cells', []) or [])
        if cell_id:
            for idx, cell in enumerate(cells):
                if cell.get('id') == cell_id:
                    return cell, idx, cell_id
        if cell_index is not None:
            actual_index = int(cell_index) - 1
            if 0 <= actual_index < len(cells):
                cell = cells[actual_index]
                return cell, actual_index, cell.get('id')
            return None, None, None
        return None, None, None

    @staticmethod
    def _find_recent_error_cell(notebook: dict):
        cells = list(notebook.get('cells', []) or [])
        for idx in range(len(cells) - 1, -1, -1):
            cell = cells[idx]
            outputs = list(cell.get('outputs') or [])
            for output in outputs:
                if str(output.get('output_type') or '') == 'error':
                    return cell, idx
        return None, None

    @staticmethod
    def _normalize_code_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @classmethod
    def _code_similarity(cls, source: Any, candidate: Any) -> float:
        source_text = cls._normalize_code_text(source)
        candidate_text = cls._normalize_code_text(candidate)
        if not source_text or not candidate_text:
            return 0.0
        return float(SequenceMatcher(None, source_text, candidate_text).ratio())

    @classmethod
    def _identifier_overlap(cls, source: Any, candidate: Any) -> float:
        source_ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(source or "")))
        candidate_ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(candidate or "")))
        source_ids = {item for item in source_ids if len(item) > 1}
        candidate_ids = {item for item in candidate_ids if len(item) > 1}
        if not source_ids or not candidate_ids:
            return 0.0
        return len(source_ids & candidate_ids) / max(1, len(source_ids | candidate_ids))

    @classmethod
    def _looks_like_fix(cls, code: str, source: str, description: Optional[str] = None) -> bool:
        desc = str(description or "").lower()
        if any(token in desc for token in ("修复", "fix", "debug", "报错", "错误", "replace", "覆盖", "改这个", "update cell")):
            return True

        similarity = cls._code_similarity(source, code)
        overlap = cls._identifier_overlap(source, code)
        return similarity >= 0.55 or (similarity >= 0.35 and overlap >= 0.45) or overlap >= 0.75

    @staticmethod
    def _is_ai_draft_code_cell(cell: dict) -> bool:
        if str(cell.get("cell_type") or "code") != "code":
            return False
        if cell.get("execution_count") is not None:
            return False
        if list(cell.get("outputs") or []):
            return False
        metadata = dict(cell.get("metadata") or {})
        if str(metadata.get("created_by") or "") != "ai_agent":
            return False
        return bool(str(cell.get("source") or "").strip())

    @classmethod
    def _find_recent_ai_draft_cell(cls, notebook: dict, code: str):
        cells = list(notebook.get("cells", []) or [])
        for idx in range(len(cells) - 1, -1, -1):
            cell = cells[idx]
            if not cls._is_ai_draft_code_cell(cell):
                continue
            source = str(cell.get("source") or "")
            similarity = cls._code_similarity(source, code)
            overlap = cls._identifier_overlap(source, code)
            if similarity >= 0.8 or (similarity >= 0.55 and overlap >= 0.55) or overlap >= 0.85:
                return cell, idx
        return None, None

    @classmethod
    def _select_write_target(
        cls,
        notebook: dict,
        *,
        code: str,
        description: Optional[str],
        cell_id: Optional[str],
        cell_index: Optional[int],
        write_mode: str,
    ) -> tuple[Optional[dict], Optional[int], Optional[str], Optional[str]]:
        target_cell = None
        target_index = None
        target_cell_id = None
        explicit_target = bool(cell_id) or cell_index is not None
        if explicit_target:
            target_cell, target_index, target_cell_id = cls._resolve_target_cell(
                notebook,
                cell_id=cell_id,
                cell_index=cell_index,
            )
            if target_cell is None:
                return None, None, None, "cell_not_found"
            return target_cell, target_index, target_cell_id, None

        normalized_write_mode = str(write_mode or "auto").strip().lower()
        recent_error_cell, recent_error_index = cls._find_recent_error_cell(notebook)
        if normalized_write_mode == "replace" and recent_error_cell is not None:
            return recent_error_cell, recent_error_index, recent_error_cell.get("id"), None
        if (
            normalized_write_mode == "auto"
            and recent_error_cell is not None
            and cls._looks_like_fix(code, recent_error_cell.get("source", ""), description=description)
        ):
            # 自动模式只会在新代码明显像修复时覆盖最近的错误单元格；
            # 否则追加为新的草稿。
            return recent_error_cell, recent_error_index, recent_error_cell.get("id"), None
        if normalized_write_mode in {"auto", "replace"}:
            draft_cell, draft_index = cls._find_recent_ai_draft_cell(notebook, code)
            if draft_cell is not None:
                return draft_cell, draft_index, draft_cell.get("id"), None
        return None, None, None, None

    @staticmethod
    def _serialize_outputs(raw_outputs: List[Any]) -> List[Dict[str, Any]]:
        serialized_outputs: List[Dict[str, Any]] = []
        for output in list(raw_outputs or []):
            if hasattr(output, "model_dump"):
                serialized_outputs.append(output.model_dump())
            elif hasattr(output, "dict"):
                serialized_outputs.append(output.dict())
            elif isinstance(output, dict):
                serialized_outputs.append(output)
            else:
                serialized_outputs.append({"output_type": "stream", "content": str(output), "mime_type": "text/plain"})
        return serialized_outputs

    @staticmethod
    def _format_serialized_outputs(serialized_outputs: List[Dict[str, Any]], description: Optional[str] = None) -> str:
        output_parts: List[str] = []
        if description:
            output_parts.append(f"📝 {description}\n")
        for output in serialized_outputs:
            output_type = output.get("output_type", "")
            content = output.get("content", "")
            mime_type = output.get("mime_type", "")
            if output_type == "stream":
                output_parts.append(f"📤 输出:\n{content}")
            elif output_type == "execute_result":
                output_parts.append(f"✅ 结果:\n{content}")
            elif output_type == "error":
                output_parts.append(f"❌ 错误:\n{content}")
            elif output_type == "display_data":
                if mime_type and "image" in mime_type:
                    output_parts.append("📊 [图表已生成并显示在 Notebook 中]")
                else:
                    output_parts.append(f"📋 显示数据:\n{str(content)[:500]}")
        if not output_parts:
            output_parts.append("✅ 代码执行成功（无输出）")
        return "\n".join(output_parts)

    @staticmethod
    def _should_run_in_background(code: str, description: Optional[str], run_mode: str) -> bool:
        normalized_mode = str(run_mode or "auto").strip().lower()
        if normalized_mode == "background":
            return True
        if normalized_mode != "auto":
            return False
        lowered = f"{description or ''}\n{code or ''}".lower()
        fit_count = lowered.count(".fit(")
        # 启发式会刻意把长训练/搜索任务倾向放到后台执行，让 notebook UI
        # 显示占位状态，而不是卡死等待。
        background_tokens = (
            "gridsearchcv",
            "randomizedsearchcv",
            "cross_val",
            "study.optimize",
            "optuna",
            "epochs",
            "epoch",
            "xgboost",
            "lightgbm",
            "catboost",
            "tensorflow",
            "torch",
            "训练多个模型",
            "模型优化",
            "交叉验证",
            "网格搜索",
        )
        if any(token in lowered for token in background_tokens):
            return True
        if fit_count >= 3:
            return True
        if fit_count >= 1 and len(str(code or "")) >= 1800:
            return True
        return False

    @staticmethod
    def _background_placeholder_outputs(description: Optional[str], execution_id: str) -> List[Dict[str, Any]]:
        content = "后台执行已启动。"
        if description:
            content += f"\n任务: {description}"
        content += f"\nexecution_id={execution_id}"
        return [{"output_type": "stream", "content": content, "mime_type": "text/plain"}]

    @staticmethod
    def _background_metadata(
        *,
        execution: Dict[str, Any],
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "execution_id": str(execution.get("execution_id") or "").strip(),
            "status": str(status or execution.get("status") or "").strip() or "running",
            "description": str(execution.get("description") or "").strip(),
            "created_at": execution.get("created_at"),
            "started_at": execution.get("started_at"),
            "completed_at": execution.get("completed_at"),
            "cancel_requested": bool(execution.get("cancel_requested")),
            "terminated_reason": execution.get("terminated_reason"),
            "policy_violation_code": execution.get("policy_violation_code"),
            "error": error if error is not None else execution.get("error"),
        }
        return {key: value for key, value in payload.items() if value not in (None, "", False)}

    async def execute(
        self,
        code: str,
        description: str = None,
        cell_id: str = None,
        cell_index: int = None,
        write_mode: str = "auto",
        run_mode: str = "auto",
        **kwargs,
    ) -> ToolResult:
        """执行代码并创建或更新 Cell"""
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

        blocked_imports = extract_sandbox_blocked_imports(code)
        if blocked_imports:
            imports_text = ", ".join(blocked_imports)
            return ToolResult(
                success=False,
                output=(
                    f"代码未执行：检测到沙箱禁用导入 {imports_text}。\n"
                    "不要使用 os/pathlib/glob/shutil/subprocess/sys 查找上传文件；"
                    "请改用 list_uploaded_files() 和 uploaded_file_path(name)。\n"
                    "最小示例：files = list_uploaded_files(); df = pd.read_csv(uploaded_file_path(files[0]))"
                ),
                error="sandbox_forbidden_import",
                data={
                    "blocked_imports": blocked_imports,
                    "notebook_updated": False,
                    "operation": "rejected",
                },
            )

        try:
            compile(str(code or ""), "<notebook_execute>", "exec")
        except SyntaxError as exc:
            return ToolResult(
                success=False,
                output=f"代码未执行：Python 语法错误，第 {exc.lineno or '?'} 行：{exc.msg}",
                error="syntax_error",
                data={
                    "lineno": exc.lineno,
                    "offset": exc.offset,
                    "notebook_updated": False,
                    "operation": "rejected",
                },
            )
        
        try:
            kernel = self.kernel_manager.get_or_create_kernel(self.notebook_id)
            notebook = None
            workspace_context = None
            if self.notebooks_store is not None and self.notebook_id in self.notebooks_store:
                notebook = self.notebooks_store[self.notebook_id]
                if "cells" not in notebook:
                    notebook["cells"] = []
                workspace_context = _workspace_context_for_notebook_payload(notebook)

            target_cell = None
            target_index = None
            target_cell_id = None
            if notebook is not None:
                target_cell, target_index, target_cell_id, target_error = self._select_write_target(
                    notebook,
                    code=code,
                    description=description,
                    cell_id=cell_id,
                    cell_index=cell_index,
                    write_mode=write_mode,
                )
                if target_error == "cell_not_found":
                    return ToolResult(
                        success=False,
                        output=f"未找到目标单元格 (cell_id={cell_id}, cell_index={cell_index})",
                        error="cell_not_found",
                    )

            should_background = bool(
                notebook is not None
                and self._should_run_in_background(code, description, run_mode)
            )
            if should_background:
                user_id = notebook.get("user_id") if isinstance(notebook, dict) else None
                if user_id is not None:
                    background_manager = get_notebook_background_execution_manager()
                    if target_cell is None and not target_cell_id:
                        target_cell_id = str(uuid.uuid4())

                    async def _finalize_background(snapshot: Dict[str, Any], result_payload: Optional[Dict[str, Any]]) -> None:
                        result_dict = dict(result_payload or {})
                        serialized_outputs = self._serialize_outputs(list(result_dict.get("outputs") or []))
                        if not serialized_outputs:
                            status_name = str(snapshot.get("status") or "failed")
                            message = str(snapshot.get("error") or "后台执行未返回结果")
                            if status_name == "cancelled":
                                message = "执行已被用户停止"
                            serialized_outputs = [
                                {
                                    "output_type": "error",
                                    "content": {"ename": "BackgroundExecution", "evalue": message, "traceback": []},
                                }
                            ]

                        metadata_payload = self._background_metadata(
                            execution=snapshot,
                            status=str(snapshot.get("status") or ""),
                            error=str(snapshot.get("error") or "") or None,
                        )
                        cell_metadata = {"background_execution": metadata_payload}
                        execution_count_value = int(
                            result_dict.get("execution_count")
                            or (notebook.get("execution_count") if isinstance(notebook, dict) else 0)
                            or 0
                        )

                        if self.notebooks_store is not None and self.notebook_id in self.notebooks_store:
                            cached_notebook = self.notebooks_store[self.notebook_id]
                            for cached_cell in list(cached_notebook.get("cells") or []):
                                if cached_cell.get("id") != target_cell_id:
                                    continue
                                cached_cell["outputs"] = serialized_outputs
                                cached_cell["execution_count"] = execution_count_value
                                merged_metadata = dict(cached_cell.get("metadata") or {})
                                merged_metadata["background_execution"] = metadata_payload
                                cached_cell["metadata"] = merged_metadata
                                break
                            cached_notebook["execution_count"] = execution_count_value
                            cached_notebook["updated_at"] = datetime.utcnow()

                        async with async_session_factory() as db_session:
                            service = NotebookService(db_session)
                            await service.save_cell_execution(
                                self.notebook_id,
                                int(user_id),
                                str(target_cell_id),
                                serialized_outputs,
                                execution_count_value,
                                metadata=cell_metadata,
                            )

                    try:
                        execution = await background_manager.start(
                            notebook_id=self.notebook_id,
                            user_id=int(user_id),
                            cell_id=str(target_cell_id or ""),
                            description=str(description or "").strip(),
                            execute_fn=lambda: kernel.execute(code, timeout=0, workspace_context=workspace_context),
                            cancel_fn=getattr(kernel, "interrupt", None),
                            finalize_fn=_finalize_background,
                        )
                    except NotebookBackgroundExecutionBusyError as exc:
                        return ToolResult(
                            success=False,
                            output=(
                                "当前 notebook 已有后台任务在运行，请先等待它完成或手动停止后再启动新的长任务。"
                                f"\n详情: {exc}"
                            ),
                            error="background_execution_busy",
                            data={"notebook_updated": False, "operation": "rejected"},
                        )

                    placeholder_outputs = self._background_placeholder_outputs(description, str(execution.get("execution_id") or ""))
                    placeholder_metadata = {
                        "background_execution": self._background_metadata(execution=execution, status="running")
                    }

                    new_cell_id = None
                    new_cell = None
                    updated_cell = None
                    operation = "append"
                    if target_cell is not None:
                        target_cell["cell_type"] = "code"
                        target_cell["source"] = code
                        target_cell["outputs"] = placeholder_outputs
                        target_cell["metadata"] = {
                            **dict(target_cell.get("metadata") or {}),
                            **placeholder_metadata,
                            "updated_by": "ai_agent",
                            "updated_at": datetime.utcnow().isoformat(),
                        }
                        if description:
                            target_cell["metadata"]["description"] = description
                        updated_cell = target_cell
                        operation = "update"
                    else:
                        new_cell_id = str(target_cell_id)
                        new_cell = {
                            "id": new_cell_id,
                            "cell_type": "code",
                            "source": code,
                            "outputs": placeholder_outputs,
                            "execution_count": notebook.get("execution_count"),
                            "metadata": {
                                "created_by": "ai_agent",
                                "created_at": datetime.utcnow().isoformat(),
                                "description": description,
                                **placeholder_metadata,
                            },
                        }
                        notebook["cells"].append(new_cell)

                    execution["cell_id"] = target_cell_id
                    if updated_cell is not None:
                        updated_cell["metadata"]["background_execution"] = self._background_metadata(
                            execution=execution,
                            status="running",
                        )
                    if new_cell is not None:
                        new_cell["metadata"]["background_execution"] = self._background_metadata(
                            execution=execution,
                            status="running",
                        )
                    notebook["updated_at"] = datetime.utcnow()

                    wait_seconds = max(
                        float(getattr(settings, "codelab_background_inline_wait_seconds", 8.0) or 0.0),
                        0.0,
                    )
                    completed_execution = None
                    if wait_seconds > 0:
                        completed_execution = await background_manager.wait(
                            execution_id=str(execution.get("execution_id") or ""),
                            user_id=int(user_id),
                            notebook_id=self.notebook_id,
                            timeout_seconds=wait_seconds,
                            include_result=True,
                        )

                    completed_status = str((completed_execution or {}).get("status") or "").strip().lower()
                    if completed_execution and completed_status not in {"", "pending", "running"}:
                        result_payload = dict(completed_execution.pop("result_payload") or {})
                        completed_execution["cell_id"] = target_cell_id
                        completion_outputs = self._serialize_outputs(list(result_payload.get("outputs") or []))
                        if not completion_outputs:
                            message = str(completed_execution.get("error") or "后台执行未返回结果")
                            if completed_status == "cancelled":
                                message = "执行已被用户停止"
                            completion_outputs = [
                                {
                                    "output_type": "error",
                                    "content": {
                                        "ename": "BackgroundExecution",
                                        "evalue": message,
                                        "traceback": [],
                                    },
                                }
                            ]

                        execution_count_value = int(
                            result_payload.get("execution_count")
                            or notebook.get("execution_count")
                            or 0
                        )
                        completion_metadata = {
                            "background_execution": self._background_metadata(
                                execution=completed_execution,
                                status=completed_status,
                                error=str(completed_execution.get("error") or "") or None,
                            )
                        }
                        if target_cell is not None:
                            target_cell["cell_type"] = "code"
                            target_cell["source"] = code
                            target_cell["outputs"] = completion_outputs
                            target_cell["execution_count"] = execution_count_value
                            target_cell["metadata"] = {
                                **dict(target_cell.get("metadata") or {}),
                                **completion_metadata,
                                "updated_by": "ai_agent",
                                "updated_at": datetime.utcnow().isoformat(),
                            }
                            if description:
                                target_cell["metadata"]["description"] = description
                            updated_cell = target_cell
                        elif new_cell is not None:
                            new_cell["outputs"] = completion_outputs
                            new_cell["execution_count"] = execution_count_value
                            new_cell["metadata"] = {
                                **dict(new_cell.get("metadata") or {}),
                                **completion_metadata,
                            }
                        notebook["execution_count"] = execution_count_value
                        notebook["updated_at"] = datetime.utcnow()

                        execution_success = (
                            bool(result_payload.get("success"))
                            if "success" in result_payload
                            else completed_status == "completed"
                        )
                        return ToolResult(
                            success=execution_success,
                            output=self._format_serialized_outputs(completion_outputs, description=description),
                            data={
                                "cell_id": target_cell_id,
                                "execution_count": execution_count_value,
                                "execution_time_ms": result_payload.get("execution_time_ms"),
                                "outputs": completion_outputs,
                                "notebook_updated": True,
                                "new_cell": new_cell if new_cell_id else None,
                                "updated_cell": updated_cell if target_cell is not None else None,
                                "operation": operation,
                                "background_execution": completed_execution,
                                "background_execution_completed": True,
                            },
                        )

                    return ToolResult(
                        success=True,
                        output=(
                            "长任务已转入后台执行。你可以继续对话或稍后回来看结果；如果不需要了，可以手动停止。"
                            f"\nexecution_id={execution.get('execution_id')}"
                        ),
                        data={
                            "cell_id": target_cell_id,
                            "execution_count": notebook.get("execution_count"),
                            "execution_time_ms": 0,
                            "outputs": placeholder_outputs,
                            "notebook_updated": True,
                            "new_cell": new_cell if new_cell_id else None,
                            "updated_cell": updated_cell if target_cell is not None else None,
                            "operation": operation,
                            "background_execution": execution,
                            "background_execution_started": True,
                        },
                    )

            result = kernel.execute(code, timeout=60, workspace_context=workspace_context)
            serialized_outputs = self._serialize_outputs(list(result.get("outputs") or []))
            execution_success = bool(result.get("success", True))

            new_cell_id = None
            new_cell = None
            updated_cell = None
            operation = "append"
            if execution_success and notebook is not None:
                if target_cell is not None:
                    target_cell["cell_type"] = "code"
                    target_cell["source"] = code
                    target_cell["outputs"] = serialized_outputs
                    target_cell["execution_count"] = result.get("execution_count")
                    target_cell["metadata"] = target_cell.get("metadata", {})
                    target_cell["metadata"].pop("background_execution", None)
                    target_cell["metadata"]["updated_by"] = "ai_agent"
                    target_cell["metadata"]["updated_at"] = datetime.utcnow().isoformat()
                    if description:
                        target_cell["metadata"]["description"] = description
                    updated_cell = target_cell
                    operation = "update"
                    logger.info(
                        f"[NotebookExecute] 更新现有 Cell: {target_cell_id}, index={target_index + 1 if target_index is not None else 'unknown'}"
                    )
                else:
                    new_cell_id = str(uuid.uuid4())
                    target_cell_id = new_cell_id
                    new_cell = {
                        "id": new_cell_id,
                        "cell_type": "code",
                        "source": code,
                        "outputs": serialized_outputs,
                        "execution_count": result.get("execution_count"),
                        "metadata": {
                            "created_by": "ai_agent",
                            "description": description,
                            "created_at": datetime.utcnow().isoformat(),
                        },
                    }
                    notebook["cells"].append(new_cell)
                    logger.info(f"[NotebookExecute] 创建新 Cell: {new_cell_id}")

                notebook["updated_at"] = datetime.utcnow()
                notebook["execution_count"] = result.get("execution_count", notebook.get("execution_count", 0))
            elif not execution_success:
                operation = "rejected"

            return ToolResult(
                success=execution_success,
                output=self._format_serialized_outputs(serialized_outputs, description=description),
                data={
                    "cell_id": target_cell_id or new_cell_id,
                    "execution_count": result.get("execution_count"),
                    "execution_time_ms": result.get("execution_time_ms"),
                    "outputs": serialized_outputs,
                    "notebook_updated": bool(target_cell_id or new_cell_id) and execution_success,
                    "new_cell": new_cell if new_cell_id else None,
                    "updated_cell": updated_cell if target_cell is not None else None,
                    "operation": operation,
                },
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
                        preview_getter = getattr(kernel, "get_variable_preview", None)
                        preview = preview_getter(name) if callable(preview_getter) else None
                        if preview:
                            line += f"\n   预览: {preview}"
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
    操作 Notebook 单元格 - 【增强版】
    
    支持添加、删除、更新、获取、移动单元格
    【增强】以 cell_id 主定位，索引仅兼容旧调用
    """
    name = "notebook_cell"
    description = """操作 Notebook 的单元格。
支持的操作:
- get: 获取所有单元格列表（显示索引和 cell_id）
- add: 添加新单元格
- delete: 删除单元格（优先用 cell_id，索引仅兼容）
- update: 更新单元格内容（优先用 cell_id，索引仅兼容）
- move: 移动单元格位置（优先用 cell_id，索引仅兼容）
- get_one: 获取单个单元格详情

【重要】删除/更新/移动时可以使用:
1. cell_id: 单元格的唯一标识符（UUID格式，首选）
2. cell_index: 单元格位置索引（从1开始，如第一个单元格是1，仅兼容）

不要先用 add 创建代码单元格、再用 notebook_execute 无目标执行同一段代码；这会造成重复 cell。
需要执行代码时优先直接调用 notebook_execute；如果已经 add 了代码草稿，后续执行必须带上该 cell_id 或先 update 该 cell。

注意：修改操作需要用户授权。"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "delete", "update", "get", "get_one", "move"],
                "description": "操作类型"
            },
            "cell_id": {
                "type": "string",
                "description": "单元格 ID（UUID格式，首选；delete/update/get_one/move 可用）"
            },
            "cell_index": {
                "type": "integer",
                "description": "单元格位置索引，从1开始（仅兼容旧调用；如果同时提供 cell_id 和 cell_index，以 cell_id 为准）"
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
                "description": "插入位置，从1开始（add 时使用，可选）"
            },
            "target_index": {
                "type": "integer",
                "description": "目标位置，从1开始（move 时使用）"
            }
        },
        "required": ["action"]
    }
    
    def __init__(self, notebooks_store: dict, notebook_id: str, user_authorized: bool = False):
        self.notebooks_store = notebooks_store
        self.notebook_id = notebook_id
        self.user_authorized = user_authorized
    
    def _resolve_cell(self, notebook: dict, cell_id: str = None, cell_index: int = None) -> tuple:
        """
        解析单元格引用，返回 (cell, actual_index, cell_id)
        支持通过 cell_id 或 cell_index 定位，优先使用 cell_id
        """
        cells = notebook.get('cells', [])

        if cell_id:
            for i, cell in enumerate(cells):
                if cell.get('id') == cell_id:
                    return cell, i, cell_id
            
            # 尝试将 cell_id 解析为数字索引（兼容旧的调用方式）
            try:
                index = int(cell_id)
                # 支持 1-based 索引
                if index >= 1:
                    actual_index = index - 1
                    if 0 <= actual_index < len(cells):
                        cell = cells[actual_index]
                        return cell, actual_index, cell.get('id')
            except ValueError:
                pass

        if cell_index is not None:
            actual_index = cell_index - 1
            if 0 <= actual_index < len(cells):
                cell = cells[actual_index]
                return cell, actual_index, cell.get('id')
            return None, None, None
        
        return None, None, None
    
    async def execute(
        self,
        action: str,
        cell_id: str = None,
        cell_index: int = None,
        cell_type: str = "code",
        content: str = "",
        index: int = None,
        target_index: int = None,
        **kwargs
    ) -> ToolResult:
        """执行单元格操作"""
        logger.info(f"[NotebookCell] action={action}, notebook_id={self.notebook_id}, cell_id={cell_id}, cell_index={cell_index}")
        
        # 获取 notebook
        notebook = self.notebooks_store.get(self.notebook_id)
        if not notebook:
            return ToolResult(
                success=False,
                output="Notebook 不存在",
                error="notebook_not_found"
            )
        
        # 检查授权（get 和 get_one 操作不需要）
        if action in ['add', 'delete', 'update', 'move'] and not self.user_authorized:
            return ToolResult(
                success=False,
                output=f"操作 '{action}' 需要用户授权。请先启用「允许 AI 操作 Notebook」选项。",
                error="authorization_required",
                data={"requires_authorization": True, "action": f"cell_{action}"}
            )
        
        try:
            if action == "get":
                return self._get_cells(notebook)
            elif action == "get_one":
                return self._get_one_cell(notebook, cell_id, cell_index)
            elif action == "add":
                return self._add_cell(notebook, cell_type, content, index)
            elif action == "delete":
                return self._delete_cell(notebook, cell_id, cell_index)
            elif action == "update":
                return self._update_cell(notebook, cell_id, cell_index, content, cell_type)
            elif action == "move":
                return self._move_cell(notebook, cell_id, cell_index, target_index)
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
        """获取所有单元格摘要 - 【增强版：显示完整 cell_id】"""
        cells = notebook.get('cells', [])
        
        if not cells:
            return ToolResult(
                success=True,
                output="Notebook 没有单元格",
                data={"cells": [], "total": 0}
            )
        
        output_parts = [f"📓 Notebook 共有 {len(cells)} 个单元格:\n"]
        output_parts.append("=" * 60)
        
        cells_data = []
        for i, cell in enumerate(cells):
            cell_id = cell.get('id', 'unknown')
            cell_type = cell.get('cell_type', 'code')
            source = cell.get('source', '')
            exec_count = cell.get('execution_count')
            has_output = bool(cell.get('outputs'))
            metadata = cell.get('metadata', {})
            created_by = metadata.get('created_by', 'user')
            blocked_imports = extract_sandbox_blocked_imports(source)

            icon = "💻" if cell_type == "code" else "📝"
            status = f"[{exec_count}]" if exec_count else "[未执行]"
            output_indicator = " 📤有输出" if has_output else ""
            creator = " 🤖AI创建" if created_by == 'ai_agent' else ""
            risk_indicator = ""
            if blocked_imports:
                imports_text = ",".join(blocked_imports[:2])
                risk_indicator = f" ⚠️禁用导入:{imports_text}"
            
            # 截取代码预览
            preview = source.replace('\n', ' ')[:80]
            if len(source) > 80:
                preview += "..."
            
            # 格式化输出，确保显示 cell_id
            output_parts.append(
                f"\n{icon} 【索引 {i+1}】{status}{output_indicator}{creator}{risk_indicator}\n"
                f"   ID: {cell_id}\n"
                f"   内容: {preview}"
            )
            
            cells_data.append({
                "id": cell_id,
                "index": i + 1,  # 1-based index for user
                "type": cell_type,
                "preview": source[:200],
                "execution_count": exec_count,
                "has_output": has_output,
                "created_by": created_by,
                "blocked_imports": blocked_imports,
                "sandbox_risky": bool(blocked_imports),
            })
        
        output_parts.append("\n" + "=" * 60)
        output_parts.append("\n💡 提示: 删除/更新时优先使用 cell_id；cell_index（如 1, 2, 3）仅兼容旧调用。")
        if any(bool(item.get("sandbox_risky")) for item in cells_data):
            output_parts.append("⚠️ 若单元格包含禁用导入（如 os），不要直接建议执行该单元格。")
        
        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data={
                "cells": cells_data,
                "total": len(cells)
            }
        )
    
    def _get_one_cell(self, notebook: dict, cell_id: str = None, cell_index: int = None) -> ToolResult:
        """获取单个单元格详情"""
        cell, actual_index, resolved_id = self._resolve_cell(notebook, cell_id, cell_index)
        
        if not cell:
            return ToolResult(
                success=False,
                output=f"未找到单元格 (cell_id={cell_id}, cell_index={cell_index})",
                error="cell_not_found"
            )
        
        cell_type = cell.get('cell_type', 'code')
        source = cell.get('source', '')
        exec_count = cell.get('execution_count')
        outputs = cell.get('outputs', [])
        metadata = cell.get('metadata', {})
        blocked_imports = extract_sandbox_blocked_imports(source)
        
        icon = "💻" if cell_type == "code" else "📝"
        
        output_parts = [
            f"{icon} 单元格详情",
            f"=" * 40,
            f"索引: {actual_index + 1}",
            f"ID: {resolved_id}",
            f"类型: {cell_type}",
            f"执行次数: {exec_count if exec_count else '未执行'}",
            f"输出数量: {len(outputs)}",
            f"创建者: {metadata.get('created_by', 'user')}",
            f"=" * 40,
        ]
        if blocked_imports:
            output_parts.extend(
                [
                    f"风险提示: 检测到沙箱禁用导入 {', '.join(blocked_imports)}",
                    "建议: 不要直接建议执行该单元格；如果要修复，先移除这些导入并改用 Notebook 文件 helper。",
                    f"=" * 40,
                ]
            )
        output_parts.append(f"内容:\n{source}")

        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data={
                "id": resolved_id,
                "index": actual_index + 1,
                "type": cell_type,
                "source": source,
                "execution_count": exec_count,
                "outputs": outputs,
                "metadata": metadata,
                "blocked_imports": blocked_imports,
                "sandbox_risky": bool(blocked_imports),
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
        
        # index 是 1-based，转换为 0-based
        if index is not None:
            actual_index = index - 1
            if 0 <= actual_index <= len(cells):
                cells.insert(actual_index, new_cell)
                pos_msg = f"在位置 {index}"
            else:
                cells.append(new_cell)
                pos_msg = "在末尾"
                actual_index = len(cells) - 1
        else:
            cells.append(new_cell)
            pos_msg = "在末尾"
            actual_index = len(cells) - 1
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已{pos_msg}添加{'代码' if cell_type == 'code' else 'Markdown'}单元格\n"
                   f"   新单元格 ID: {new_cell['id']}\n"
                   f"   索引: {actual_index + 1}",
            data={
                "cell_id": new_cell['id'],
                "index": actual_index + 1,
                "notebook_updated": True,
                "new_cell": new_cell
            }
        )
    
    def _delete_cell(self, notebook: dict, cell_id: str = None, cell_index: int = None) -> ToolResult:
        """删除单元格 - 【增强版：支持索引】"""
        if not cell_id and cell_index is None:
            return ToolResult(
                success=False,
                output="需要指定 cell_id 或 cell_index",
                error="missing_identifier"
            )
        
        cell, actual_index, resolved_id = self._resolve_cell(notebook, cell_id, cell_index)
        
        if not cell:
            return ToolResult(
                success=False,
                output=f"未找到单元格 (cell_id={cell_id}, cell_index={cell_index})\n"
                       f"💡 提示: 使用 action='get' 查看所有单元格的索引和ID",
                error="cell_not_found"
            )
        
        cells = notebook.get('cells', [])
        deleted_preview = cell.get('source', '')[:50]
        
        # 删除单元格
        cells.pop(actual_index)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已删除单元格\n"
                   f"   原索引: {actual_index + 1}\n"
                   f"   ID: {resolved_id[:8]}...\n"
                   f"   内容预览: {deleted_preview}...",
            data={
                "deleted_id": resolved_id,
                "deleted_ids": [resolved_id],  # 用于数据库同步
                "deleted_index": actual_index + 1,
                "remaining_cells": len(cells),
                "notebook_updated": True,
                "action": "delete"
            }
        )
    
    def _update_cell(self, notebook: dict, cell_id: str = None, cell_index: int = None, 
                     content: str = None, cell_type: str = None) -> ToolResult:
        """更新单元格 - 【增强版：支持索引】"""
        if not cell_id and cell_index is None:
            return ToolResult(
                success=False,
                output="需要指定 cell_id 或 cell_index",
                error="missing_identifier"
            )
        
        cell, actual_index, resolved_id = self._resolve_cell(notebook, cell_id, cell_index)
        
        if not cell:
            return ToolResult(
                success=False,
                output=f"未找到单元格 (cell_id={cell_id}, cell_index={cell_index})\n"
                       f"💡 提示: 使用 action='get' 查看所有单元格的索引和ID",
                error="cell_not_found"
            )
        
        # 更新内容
        changes = []
        if content is not None:
            cell['source'] = content
            changes.append("内容")
        if cell_type is not None:
            cell['cell_type'] = cell_type
            changes.append("类型")
        
        cell['metadata'] = cell.get('metadata', {})
        cell['metadata']['updated_by'] = 'ai_agent'
        cell['metadata']['updated_at'] = datetime.utcnow().isoformat()
        
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已更新单元格\n"
                   f"   索引: {actual_index + 1}\n"
                   f"   ID: {resolved_id[:8]}...\n"
                   f"   更新项: {', '.join(changes)}",
            data={
                "updated_id": resolved_id,
                "updated_index": actual_index + 1,
                "notebook_updated": True,
                "updated_cell": cell
            }
        )
    
    def _move_cell(self, notebook: dict, cell_id: str = None, cell_index: int = None, 
                   target_index: int = None) -> ToolResult:
        """移动单元格位置"""
        if not cell_id and cell_index is None:
            return ToolResult(
                success=False,
                output="需要指定 cell_id 或 cell_index",
                error="missing_identifier"
            )
        
        if target_index is None:
            return ToolResult(
                success=False,
                output="需要指定 target_index（目标位置）",
                error="missing_target_index"
            )
        
        cell, actual_index, resolved_id = self._resolve_cell(notebook, cell_id, cell_index)
        
        if not cell:
            return ToolResult(
                success=False,
                output=f"未找到单元格 (cell_id={cell_id}, cell_index={cell_index})",
                error="cell_not_found"
            )
        
        cells = notebook.get('cells', [])
        target_actual = target_index - 1  # 转换为 0-based
        
        if target_actual < 0 or target_actual >= len(cells):
            return ToolResult(
                success=False,
                output=f"目标位置无效: {target_index}（有效范围: 1-{len(cells)}）",
                error="invalid_target_index"
            )
        
        # 移动单元格
        cells.pop(actual_index)
        cells.insert(target_actual, cell)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已移动单元格\n"
                   f"   从位置 {actual_index + 1} 移动到位置 {target_index}\n"
                   f"   ID: {resolved_id[:8]}...",
            data={
                "cell_id": resolved_id,
                "from_index": actual_index + 1,
                "to_index": target_index
            }
        )


class NotebookCellCleanupTool(Tool):
    """
    智能清理 Notebook 单元格 - 【新增工具】
    
    支持多种清理策略，帮助用户快速整理 Notebook
    """
    name = "notebook_cleanup"
    description = """智能清理 Notebook 单元格。
支持的清理策略:
- duplicates: 删除重复内容的单元格（保留第一个或已执行的）
- empty: 删除空白单元格
- unexecuted: 删除未执行的代码单元格
- ai_created: 删除 AI 创建的单元格
- by_indices: 批量删除指定索引的单元格
- preview: 预览将被清理的单元格（不实际删除）

注意：此操作需要用户授权。"""
    parameters = {
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["duplicates", "empty", "unexecuted", "ai_created", "by_indices", "preview"],
                "description": "清理策略"
            },
            "indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要删除的单元格索引列表（by_indices 策略使用，索引从1开始）"
            },
            "keep_executed": {
                "type": "boolean",
                "description": "在清理重复单元格时，是否优先保留已执行的版本（默认 True）",
                "default": True
            },
            "similarity_threshold": {
                "type": "number",
                "description": "判定内容相似的阈值（0-1，默认 0.9）",
                "default": 0.9
            },
            "dry_run": {
                "type": "boolean",
                "description": "是否只预览不实际删除（默认 False）",
                "default": False
            }
        },
        "required": ["strategy"]
    }
    
    def __init__(self, notebooks_store: dict, notebook_id: str, user_authorized: bool = False):
        self.notebooks_store = notebooks_store
        self.notebook_id = notebook_id
        self.user_authorized = user_authorized
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度"""
        return SequenceMatcher(None, text1.strip(), text2.strip()).ratio()
    
    async def execute(
        self,
        strategy: str,
        indices: List[int] = None,
        keep_executed: bool = True,
        similarity_threshold: float = 0.9,
        dry_run: bool = False,
        **kwargs
    ) -> ToolResult:
        """执行清理操作"""
        logger.info(f"[NotebookCleanup] strategy={strategy}, notebook_id={self.notebook_id}")
        
        # 获取 notebook
        notebook = self.notebooks_store.get(self.notebook_id)
        if not notebook:
            return ToolResult(
                success=False,
                output="Notebook 不存在",
                error="notebook_not_found"
            )
        
        # preview 策略不需要授权
        if strategy != 'preview' and not dry_run and not self.user_authorized:
            return ToolResult(
                success=False,
                output="清理操作需要用户授权。请先启用「允许 AI 操作 Notebook」选项。",
                error="authorization_required",
                data={"requires_authorization": True, "action": "cleanup"}
            )
        
        cells = notebook.get('cells', [])
        if not cells:
            return ToolResult(
                success=True,
                output="Notebook 没有单元格，无需清理",
                data={"cleaned": 0}
            )
        
        try:
            if strategy == "preview":
                return self._preview_all(cells)
            elif strategy == "duplicates":
                return self._clean_duplicates(notebook, cells, keep_executed, similarity_threshold, dry_run)
            elif strategy == "empty":
                return self._clean_empty(notebook, cells, dry_run)
            elif strategy == "unexecuted":
                return self._clean_unexecuted(notebook, cells, dry_run)
            elif strategy == "ai_created":
                return self._clean_ai_created(notebook, cells, dry_run)
            elif strategy == "by_indices":
                return self._clean_by_indices(notebook, cells, indices or [], dry_run)
            else:
                return ToolResult(
                    success=False,
                    output=f"未知清理策略: {strategy}",
                    error="invalid_strategy"
                )
        except Exception as e:
            logger.error(f"[NotebookCleanup] 清理失败: {e}")
            return ToolResult(
                success=False,
                output=f"清理失败: {str(e)}",
                error=str(e)
            )
    
    def _preview_all(self, cells: List[dict]) -> ToolResult:
        """预览所有单元格，分析可清理项"""
        analysis = {
            "total": len(cells),
            "empty": [],
            "unexecuted": [],
            "ai_created": [],
            "potential_duplicates": []
        }
        
        output_parts = [f"📊 Notebook 分析报告 (共 {len(cells)} 个单元格)\n"]
        output_parts.append("=" * 50)
        
        # 分析各类单元格
        for i, cell in enumerate(cells):
            source = cell.get('source', '').strip()
            cell_type = cell.get('cell_type', 'code')
            exec_count = cell.get('execution_count')
            metadata = cell.get('metadata', {})
            
            # 空单元格
            if not source:
                analysis['empty'].append(i + 1)
            
            # 未执行的代码单元格
            if cell_type == 'code' and not exec_count:
                analysis['unexecuted'].append(i + 1)
            
            # AI 创建的单元格
            if metadata.get('created_by') == 'ai_agent':
                analysis['ai_created'].append(i + 1)
        
        # 检测重复
        for i, cell1 in enumerate(cells):
            source1 = cell1.get('source', '').strip()
            if not source1:
                continue
            for j, cell2 in enumerate(cells[i+1:], start=i+1):
                source2 = cell2.get('source', '').strip()
                if not source2:
                    continue
                similarity = self._calculate_similarity(source1, source2)
                if similarity >= 0.9:
                    analysis['potential_duplicates'].append({
                        'cells': [i + 1, j + 1],
                        'similarity': f"{similarity:.1%}"
                    })
        
        # 格式化输出
        output_parts.append(f"\n🗑️ 空单元格: {len(analysis['empty'])} 个")
        if analysis['empty']:
            output_parts.append(f"   索引: {analysis['empty']}")
        
        output_parts.append(f"\n⏸️ 未执行的代码单元格: {len(analysis['unexecuted'])} 个")
        if analysis['unexecuted']:
            output_parts.append(f"   索引: {analysis['unexecuted']}")
        
        output_parts.append(f"\n🤖 AI 创建的单元格: {len(analysis['ai_created'])} 个")
        if analysis['ai_created']:
            output_parts.append(f"   索引: {analysis['ai_created']}")
        
        output_parts.append(f"\n📋 可能重复的单元格: {len(analysis['potential_duplicates'])} 组")
        for dup in analysis['potential_duplicates'][:5]:  # 最多显示5组
            output_parts.append(f"   单元格 {dup['cells'][0]} 和 {dup['cells'][1]} (相似度: {dup['similarity']})")
        
        output_parts.append("\n" + "=" * 50)
        output_parts.append("\n💡 使用建议:")
        if analysis['empty']:
            output_parts.append(f"   - strategy='empty' 可删除 {len(analysis['empty'])} 个空单元格")
        if analysis['potential_duplicates']:
            output_parts.append(f"   - strategy='duplicates' 可清理 {len(analysis['potential_duplicates'])} 组重复")
        if analysis['unexecuted']:
            output_parts.append(f"   - strategy='unexecuted' 可删除 {len(analysis['unexecuted'])} 个未执行单元格")
        
        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data=analysis
        )
    
    def _clean_duplicates(self, notebook: dict, cells: List[dict], 
                          keep_executed: bool, threshold: float, dry_run: bool) -> ToolResult:
        """清理重复单元格"""
        to_delete = set()
        duplicate_groups = []
        
        for i, cell1 in enumerate(cells):
            if i in to_delete:
                continue
            source1 = cell1.get('source', '').strip()
            if not source1:
                continue
            
            group = [i]
            for j, cell2 in enumerate(cells[i+1:], start=i+1):
                if j in to_delete:
                    continue
                source2 = cell2.get('source', '').strip()
                if not source2:
                    continue
                
                if self._calculate_similarity(source1, source2) >= threshold:
                    group.append(j)
            
            if len(group) > 1:
                duplicate_groups.append(group)
                
                # 决定保留哪个
                if keep_executed:
                    # 优先保留已执行的
                    executed = [idx for idx in group if cells[idx].get('execution_count')]
                    if executed:
                        keep = executed[0]  # 保留第一个已执行的
                    else:
                        keep = group[0]  # 都没执行就保留第一个
                else:
                    keep = group[0]  # 保留第一个
                
                for idx in group:
                    if idx != keep:
                        to_delete.add(idx)
        
        if not to_delete:
            return ToolResult(
                success=True,
                output="没有发现重复的单元格",
                data={"cleaned": 0}
            )
        
        # 收集要删除的 cell_id（在删除前）
        deleted_ids = [cells[idx].get('id') for idx in to_delete if cells[idx].get('id')]
        
        if dry_run:
            return ToolResult(
                success=True,
                output=f"🔍 预览模式：发现 {len(duplicate_groups)} 组重复，将删除 {len(to_delete)} 个单元格\n"
                       f"   待删除索引: {sorted([i+1 for i in to_delete])}",
                data={"to_delete": sorted([i+1 for i in to_delete]), "groups": len(duplicate_groups)}
            )
        
        # 实际删除（从后往前删除，避免索引变化）
        for idx in sorted(to_delete, reverse=True):
            cells.pop(idx)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已清理 {len(to_delete)} 个重复单元格（{len(duplicate_groups)} 组重复）\n"
                   f"   剩余单元格: {len(cells)} 个",
            data={
                "cleaned": len(to_delete), 
                "remaining": len(cells),
                "deleted_ids": deleted_ids,
                "notebook_updated": True,
                "action": "cleanup_duplicates"
            }
        )
    
    def _clean_empty(self, notebook: dict, cells: List[dict], dry_run: bool) -> ToolResult:
        """清理空单元格"""
        to_delete = []
        for i, cell in enumerate(cells):
            if not cell.get('source', '').strip():
                to_delete.append(i)
        
        if not to_delete:
            return ToolResult(
                success=True,
                output="没有空单元格",
                data={"cleaned": 0}
            )
        
        # 收集要删除的 cell_id（在删除前）
        deleted_ids = [cells[idx].get('id') for idx in to_delete if cells[idx].get('id')]
        
        if dry_run:
            return ToolResult(
                success=True,
                output=f"🔍 预览模式：发现 {len(to_delete)} 个空单元格\n"
                       f"   待删除索引: {[i+1 for i in to_delete]}",
                data={"to_delete": [i+1 for i in to_delete]}
            )
        
        for idx in sorted(to_delete, reverse=True):
            cells.pop(idx)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已删除 {len(to_delete)} 个空单元格\n"
                   f"   剩余单元格: {len(cells)} 个",
            data={
                "cleaned": len(to_delete), 
                "remaining": len(cells),
                "deleted_ids": deleted_ids,
                "notebook_updated": True,
                "action": "cleanup_empty"
            }
        )
    
    def _clean_unexecuted(self, notebook: dict, cells: List[dict], dry_run: bool) -> ToolResult:
        """清理未执行的代码单元格"""
        to_delete = []
        for i, cell in enumerate(cells):
            if cell.get('cell_type') == 'code' and not cell.get('execution_count'):
                to_delete.append(i)
        
        if not to_delete:
            return ToolResult(
                success=True,
                output="没有未执行的代码单元格",
                data={"cleaned": 0}
            )
        
        # 收集要删除的 cell_id（在删除前）
        deleted_ids = [cells[idx].get('id') for idx in to_delete if cells[idx].get('id')]
        
        if dry_run:
            return ToolResult(
                success=True,
                output=f"🔍 预览模式：发现 {len(to_delete)} 个未执行的代码单元格\n"
                       f"   待删除索引: {[i+1 for i in to_delete]}",
                data={"to_delete": [i+1 for i in to_delete]}
            )
        
        for idx in sorted(to_delete, reverse=True):
            cells.pop(idx)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已删除 {len(to_delete)} 个未执行的代码单元格\n"
                   f"   剩余单元格: {len(cells)} 个",
            data={
                "cleaned": len(to_delete), 
                "remaining": len(cells),
                "deleted_ids": deleted_ids,
                "notebook_updated": True,
                "action": "cleanup_unexecuted"
            }
        )
    
    def _clean_ai_created(self, notebook: dict, cells: List[dict], dry_run: bool) -> ToolResult:
        """清理 AI 创建的单元格"""
        to_delete = []
        for i, cell in enumerate(cells):
            if cell.get('metadata', {}).get('created_by') == 'ai_agent':
                to_delete.append(i)
        
        if not to_delete:
            return ToolResult(
                success=True,
                output="没有 AI 创建的单元格",
                data={"cleaned": 0}
            )
        
        # 收集要删除的 cell_id（在删除前）
        deleted_ids = [cells[idx].get('id') for idx in to_delete if cells[idx].get('id')]
        
        if dry_run:
            return ToolResult(
                success=True,
                output=f"🔍 预览模式：发现 {len(to_delete)} 个 AI 创建的单元格\n"
                       f"   待删除索引: {[i+1 for i in to_delete]}",
                data={"to_delete": [i+1 for i in to_delete]}
            )
        
        for idx in sorted(to_delete, reverse=True):
            cells.pop(idx)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已删除 {len(to_delete)} 个 AI 创建的单元格\n"
                   f"   剩余单元格: {len(cells)} 个",
            data={
                "cleaned": len(to_delete), 
                "remaining": len(cells),
                "deleted_ids": deleted_ids,
                "notebook_updated": True,
                "action": "cleanup_ai_created"
            }
        )
    
    def _clean_by_indices(self, notebook: dict, cells: List[dict], 
                          indices: List[int], dry_run: bool) -> ToolResult:
        """批量删除指定索引的单元格"""
        if not indices:
            return ToolResult(
                success=False,
                output="请指定要删除的单元格索引列表",
                error="missing_indices"
            )
        
        # 转换为 0-based 并验证
        valid_indices = []
        invalid_indices = []
        for idx in indices:
            actual = idx - 1  # 转换为 0-based
            if 0 <= actual < len(cells):
                valid_indices.append(actual)
            else:
                invalid_indices.append(idx)
        
        if invalid_indices:
            return ToolResult(
                success=False,
                output=f"以下索引无效: {invalid_indices}（有效范围: 1-{len(cells)}）",
                error="invalid_indices"
            )
        
        # 收集要删除的 cell_id（在删除前）
        deleted_ids = [cells[idx].get('id') for idx in valid_indices if cells[idx].get('id')]
        
        if dry_run:
            preview_cells = []
            for idx in valid_indices:
                cell = cells[idx]
                preview_cells.append({
                    "index": idx + 1,
                    "preview": cell.get('source', '')[:50] + "..."
                })
            
            return ToolResult(
                success=True,
                output=f"🔍 预览模式：将删除 {len(valid_indices)} 个单元格\n"
                       f"   待删除索引: {[i+1 for i in valid_indices]}",
                data={"to_delete": [i+1 for i in valid_indices], "cells": preview_cells}
            )
        
        # 实际删除
        for idx in sorted(valid_indices, reverse=True):
            cells.pop(idx)
        
        notebook['cells'] = cells
        notebook['updated_at'] = datetime.utcnow()
        self.notebooks_store[self.notebook_id] = notebook
        
        return ToolResult(
            success=True,
            output=f"✅ 已删除 {len(valid_indices)} 个单元格\n"
                   f"   剩余单元格: {len(cells)} 个",
            data={
                "cleaned": len(valid_indices), 
                "remaining": len(cells),
                "deleted_ids": deleted_ids,
                "notebook_updated": True,
                "action": "cleanup_by_indices"
            }
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
        """安装 Python 包"""
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
        allowed = []
        blocked = []
        
        for pkg in packages:
            # 提取包名（去除版本号）
            pkg_name = re.split(r'[<>=!]', pkg)[0].strip().lower()
            if pkg_name in ALLOWED_PACKAGES:
                allowed.append(pkg)
            else:
                blocked.append(pkg)
        
        if blocked:
            return ToolResult(
                success=False,
                output=f"以下包不在白名单中，无法安装: {blocked}",
                error="blocked_packages",
                data={"blocked": blocked, "allowed_packages": list(ALLOWED_PACKAGES)}
            )
        
        if not allowed:
            return ToolResult(
                success=False,
                output="没有有效的包可以安装",
                error="no_valid_packages"
            )
        
        try:
            # 构建 pip 命令
            cmd = [sys.executable, '-m', 'pip', 'install']
            if upgrade:
                cmd.append('--upgrade')
            cmd.extend(allowed)
            
            # 执行安装
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0:
                return ToolResult(
                    success=True,
                    output=f"✅ 成功安装: {', '.join(allowed)}\n\n{result.stdout[-1000:]}",
                    data={"installed": allowed}
                )
            else:
                return ToolResult(
                    success=False,
                    output=f"安装失败:\n{result.stderr[-1000:]}",
                    error=result.stderr
                )
                
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="安装超时（限制 120 秒）",
                error="timeout"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"安装出错: {str(e)}",
                error=str(e)
            )


class WebScrapeTool(Tool):
    """
    网页爬取工具
    
    获取网页内容，支持提取文本或 HTML
    """
    name = "web_scrape"
    description = """爬取网页内容。
可以获取网页的文本内容、HTML、或提取特定元素。
出于安全考虑，某些域名被禁止访问。"""
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要爬取的网页 URL"
            },
            "extract": {
                "type": "string",
                "enum": ["text", "html", "title", "links", "images"],
                "description": "提取内容类型",
                "default": "text"
            },
            "selector": {
                "type": "string",
                "description": "CSS 选择器，用于提取特定元素（可选）"
            },
            "max_length": {
                "type": "integer",
                "description": "最大返回长度",
                "default": 5000
            }
        },
        "required": ["url"]
    }
    _USER_AGENT = "ResearchBot/1.0 (+https://localhost)"
    _domain_last_request_ts: Dict[str, float] = {}
    _domain_rate_lock = asyncio.Lock()

    async def _check_robots(self, url: str, scheme: str, domain: str) -> tuple[bool, Optional[str]]:
        if not bool(getattr(settings, "web_scrape_enforce_robots", True)):
            return True, None

        robots_url = f"{scheme}://{domain}/robots.txt"
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": self._USER_AGENT},
            ) as client:
                response = await client.get(robots_url)
            if response.status_code >= 400:
                return True, None

            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            if parser.can_fetch(self._USER_AGENT, url):
                return True, None
            return False, robots_url
        except Exception as exc:
            logger.warning(f"[WebScrape] robots 检查失败，按允许处理: {exc}")
            return True, None

    async def _check_domain_rate_limit(self, domain: str) -> Optional[float]:
        min_interval = float(getattr(settings, "web_scrape_domain_min_interval_seconds", 1.5) or 0.0)
        if min_interval <= 0:
            return None

        # 限流按域名划分且只在当前进程内生效；它避免 agent 循环意外快速重试，
        # 同时不引入持久状态。
        now = time.monotonic()
        async with self._domain_rate_lock:
            last = self._domain_last_request_ts.get(domain, 0.0)
            wait_seconds = min_interval - (now - last)
            if wait_seconds > 0:
                return round(wait_seconds, 2)
            self._domain_last_request_ts[domain] = now
        return None
    
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
                output="beautifulsoup4 未安装，无法使用网页爬取功能",
                error="bs4_not_available"
            )
        
        # 验证 URL
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return ToolResult(
                    success=False,
                    output=f"无效的 URL: {url}",
                    error="invalid_url"
                )
            
            # 检查域名黑名单
            domain = parsed.netloc.lower()
            for blocked in BLOCKED_DOMAINS:
                if blocked in domain:
                    return ToolResult(
                        success=False,
                        output=f"该域名被禁止访问: {domain}",
                        error="blocked_domain"
                    )

            allowed, robots_url = await self._check_robots(url, parsed.scheme, domain)
            if not allowed:
                return ToolResult(
                    success=False,
                    output=f"robots.txt 禁止抓取该页面: {robots_url}",
                    error="robots_disallowed",
                )

            wait_seconds = await self._check_domain_rate_limit(domain)
            if wait_seconds is not None:
                return ToolResult(
                    success=False,
                    output=f"请求频率过高，请在 {wait_seconds:.2f} 秒后重试",
                    error="rate_limited",
                    data={"retry_after_seconds": wait_seconds, "domain": domain},
                )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"URL 解析失败: {str(e)}",
                error=str(e)
            )
        
        try:
            # 发起请求
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": self._USER_AGENT}
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            
            # 解析 HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 移除脚本和样式
            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()
            
            # 根据选择器过滤
            if selector:
                elements = soup.select(selector)
                if not elements:
                    return ToolResult(
                        success=True,
                        output=f"未找到匹配 '{selector}' 的元素",
                        data={"url": url, "selector": selector, "found": 0}
                    )
                soup = BeautifulSoup(''.join(str(e) for e in elements), 'html.parser')
            
            # 提取内容
            result_content = ""
            result_data = {"url": url}
            
            if extract == "text":
                result_content = soup.get_text(separator='\n', strip=True)
                result_data["type"] = "text"
            elif extract == "html":
                result_content = soup.prettify()
                result_data["type"] = "html"
            elif extract == "title":
                title = soup.find('title')
                result_content = title.get_text() if title else "无标题"
                result_data["type"] = "title"
            elif extract == "links":
                links = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    text = a.get_text(strip=True)
                    links.append({"href": href, "text": text[:100]})
                result_content = json.dumps(links[:50], ensure_ascii=False, indent=2)
                result_data["type"] = "links"
                result_data["count"] = len(links)
            elif extract == "images":
                images = []
                for img in soup.find_all('img', src=True):
                    images.append({
                        "src": img['src'],
                        "alt": img.get('alt', '')[:100]
                    })
                result_content = json.dumps(images[:30], ensure_ascii=False, indent=2)
                result_data["type"] = "images"
                result_data["count"] = len(images)
            
            # 截断内容
            if len(result_content) > max_length:
                result_content = result_content[:max_length] + f"\n\n... (内容已截断，共 {len(result_content)} 字符)"
            
            return ToolResult(
                success=True,
                output=f"📄 网页内容 ({extract}):\n\n{result_content}",
                data=result_data
            )
            
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="请求超时",
                error="timeout"
            )
        except httpx.HTTPStatusError as e:
            return ToolResult(
                success=False,
                output=f"HTTP 错误: {e.response.status_code}",
                error=str(e)
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
    代码分析工具
    
    分析代码质量、提供优化建议
    """
    name = "code_analysis"
    description = """分析 Python 代码，提供优化建议。
可以检查代码风格、潜在问题、性能建议等。"""
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要分析的 Python 代码"
            },
            "analysis_type": {
                "type": "string",
                "enum": ["style", "errors", "performance", "all"],
                "description": "分析类型",
                "default": "all"
            }
        },
        "required": ["code"]
    }
    
    async def execute(self, code: str, analysis_type: str = "all", **kwargs) -> ToolResult:
        """分析代码"""
        logger.info(f"[CodeAnalysis] type={analysis_type}, code_length={len(code)}")
        
        issues = []
        suggestions = []
        
        # 基本语法检查
        try:
            compile(code, '<string>', 'exec')
        except SyntaxError as e:
            issues.append(f"语法错误 (行 {e.lineno}): {e.msg}")
            return ToolResult(
                success=True,
                output=f"❌ 发现语法错误:\n{issues[0]}",
                data={"issues": issues, "has_syntax_error": True}
            )
        
        lines = code.split('\n')
        
        # 代码风格检查
        if analysis_type in ['style', 'all']:
            for i, line in enumerate(lines, 1):
                # 行长度
                if len(line) > 100:
                    issues.append(f"行 {i}: 超过 100 字符 ({len(line)} 字符)")
                
                # 尾随空格
                if line != line.rstrip():
                    issues.append(f"行 {i}: 存在尾随空格")
                
                # Tab vs 空格
                if '\t' in line:
                    issues.append(f"行 {i}: 使用了 Tab，建议使用空格")
        
        # 潜在错误检查
        if analysis_type in ['errors', 'all']:
            # 未使用的导入（简单检测）
            import_pattern = re.compile(r'^(?:from\s+\S+\s+)?import\s+(\w+)')
            imports = []
            for line in lines:
                match = import_pattern.match(line.strip())
                if match:
                    imports.append(match.group(1))
            
            for imp in imports:
                # 简单检测：如果导入的名称只出现一次（在import语句中），可能未使用
                count = code.count(imp)
                if count == 1:
                    suggestions.append(f"模块 '{imp}' 可能未使用")
            
            # 检测常见错误模式
            if 'except:' in code and 'except Exception' not in code:
                suggestions.append("使用裸 except: 可能会捕获所有异常，建议指定具体异常类型")
            
            if '== None' in code or '!= None' in code:
                suggestions.append("建议使用 'is None' 或 'is not None' 代替 '== None'")
        
        # 性能建议
        if analysis_type in ['performance', 'all']:
            if '+=' in code and 'str' in code.lower():
                suggestions.append("字符串拼接建议使用 join() 或 f-string 代替 +=")
            
            if 'for' in code and 'append' in code:
                suggestions.append("循环中使用 append 可以考虑使用列表推导式")
            
            if 'global' in code:
                suggestions.append("使用全局变量可能影响性能和可维护性")
        
        # 格式化输出
        output_parts = ["📋 代码分析报告\n"]
        
        if issues:
            output_parts.append(f"⚠️ 发现 {len(issues)} 个问题:")
            for issue in issues[:10]:  # 最多显示10个
                output_parts.append(f"  • {issue}")
        else:
            output_parts.append("✅ 未发现明显问题")
        
        if suggestions:
            output_parts.append(f"\n💡 优化建议 ({len(suggestions)} 条):")
            for suggestion in suggestions[:10]:
                output_parts.append(f"  • {suggestion}")
        
        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data={
                "issues": issues,
                "suggestions": suggestions,
                "issue_count": len(issues),
                "suggestion_count": len(suggestions)
            }
        )


class ErrorDiagnosisTool(Tool):
    """
    错误诊断工具
    
    分析错误信息，提供解决方案
    """
    name = "error_diagnosis"
    description = """分析 Python 错误信息，提供可能的解决方案。
支持常见错误类型：语法错误、导入错误、类型错误等。"""
    parameters = {
        "type": "object",
        "properties": {
            "error_message": {
                "type": "string",
                "description": "错误信息（包括 traceback）"
            },
            "code_context": {
                "type": "string",
                "description": "相关代码上下文（可选）"
            }
        },
        "required": ["error_message"]
    }
    
    async def execute(self, error_message: str, code_context: str = None, **kwargs) -> ToolResult:
        """诊断错误"""
        logger.info(f"[ErrorDiagnosis] error_length={len(error_message)}")
        
        suggestions = self._analyze_error(error_message)
        
        output_parts = ["🔍 错误诊断\n"]
        
        # 提取错误类型
        error_type = "未知错误"
        for line in error_message.split('\n'):
            if 'Error:' in line or 'Exception:' in line:
                error_type = line.strip()
                break
        
        output_parts.append(f"错误类型: {error_type}\n")
        
        if suggestions:
            output_parts.append("💡 可能的解决方案:")
            for i, suggestion in enumerate(suggestions, 1):
                output_parts.append(f"  {i}. {suggestion}")
        else:
            output_parts.append("暂无具体建议，请检查代码逻辑和数据类型。")
        
        return ToolResult(
            success=True,
            output="\n".join(output_parts),
            data={
                "error_type": error_type,
                "suggestions": suggestions
            }
        )
    
    def _analyze_error(self, error_message: str) -> List[str]:
        """分析错误并返回建议"""
        suggestions = []
        error_lower = error_message.lower()
        
        # NameError
        if 'nameerror' in error_lower:
            match = re.search(r"name '(\w+)' is not defined", error_message)
            if match:
                name = match.group(1)
                suggestions.append(f"变量 '{name}' 未定义，检查是否有拼写错误")
                suggestions.append(f"确保在使用前已经定义了 '{name}'")
                suggestions.append("如果是导入的模块，检查 import 语句")
        
        # TypeError
        elif 'typeerror' in error_lower:
            if 'not subscriptable' in error_lower:
                suggestions.append("对象不支持索引操作，检查对象类型")
            elif 'not callable' in error_lower:
                suggestions.append("对象不可调用，可能把变量当函数用了")
            elif "unsupported operand type" in error_lower:
                suggestions.append("操作数类型不兼容，检查变量类型")
            else:
                suggestions.append("检查函数参数类型是否正确")
        
        # IndexError
        elif 'indexerror' in error_lower:
            suggestions.append("索引超出范围，检查列表/数组长度")
            suggestions.append("使用 len() 检查序列长度")
        
        # KeyError
        elif 'keyerror' in error_lower:
            match = re.search(r"KeyError: ['\"]?(\w+)['\"]?", error_message)
            if match:
                key = match.group(1)
                suggestions.append(f"字典中没有键 '{key}'")
                suggestions.append("使用 dict.get(key, default) 避免 KeyError")
                suggestions.append("使用 'key in dict' 检查键是否存在")
        
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
支持的来源: auto, semantic_scholar, arxiv, pubmed, openalex, crossref, multi。
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
                "enum": ["auto", "semantic_scholar", "arxiv", "pubmed", "openalex", "crossref", "multi"],
                "description": "搜索来源",
                "default": "auto"
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
        source: str = "auto",
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
            if source == "multi":
                multi_source_count = 4
                if hasattr(self.service, "multi_source_count"):
                    try:
                        multi_source_count = int(self.service.multi_source_count())
                    except Exception:
                        multi_source_count = 4
                per_source = max(1, int(math.ceil(max_results / max(1, multi_source_count))))
                result = await self.service.search_multi(
                    query=query,
                    limit_per_source=per_source,
                    year_range=search_kwargs.get("year_range"),
                )
                result["papers"] = result.get("papers", [])[:max_results]
            else:
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
            requested_source = str(source or "auto").strip() or "auto"
            resolved_source = str(result.get("resolved_source") or requested_source).strip() or requested_source
            
            if not papers:
                return ToolResult(
                    success=True,
                    output=f"未找到关于 '{query}' 的学术论文。",
                    data={
                        "papers": [],
                        "query": query,
                        "source": requested_source,
                        "resolved_source": result.get("resolved_source"),
                        "attempted_sources": result.get("attempted_sources", []),
                        "partial_errors": result.get("partial_errors", {}),
                    }
                )
            
            # 格式化输出
            source_names = {
                "auto": "自动学术搜索链",
                "semantic_scholar": "Semantic Scholar",
                "arxiv": "arXiv",
                "pubmed": "PubMed",
                "openalex": "OpenAlex",
                "crossref": "Crossref",
                "multi": "OpenAlex + Semantic Scholar + arXiv + PubMed",
            }
            
            output_parts = [f"📚 在 {source_names.get(resolved_source, resolved_source)} 搜索 '{query}' 的结果:\n"]
            
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
                    "source": requested_source,
                    "resolved_source": resolved_source,
                    "attempted_sources": result.get("attempted_sources", []),
                    "partial_errors": result.get("partial_errors", {}),
                    "sources": result.get("sources", {}),
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
        NotebookExecuteTool(kernel_manager, notebook_id, notebooks_store, user_authorized),
        NotebookVariablesTool(kernel_manager, notebook_id),
        NotebookCellTool(notebooks_store, notebook_id, user_authorized),
        NotebookCellCleanupTool(notebooks_store, notebook_id, user_authorized),  # 新增
        PipInstallTool(user_authorized),
        WebScrapeTool(),
        CodeAnalysisTool(),
        ErrorDiagnosisTool(),  # 新增
        EnhancedLiteratureSearchTool(),
    ]
