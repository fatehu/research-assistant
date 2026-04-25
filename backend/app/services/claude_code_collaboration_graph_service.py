from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict

from app.config import settings
from app.services.project_runtime_service import ProjectRuntimeService, _safe_slug


class ClaudeCodeCollaborationState(TypedDict, total=False):
    project_id: int
    workspace_id: int
    workspace_dir: str
    project_title: str
    execution_id: str
    task: str
    model: str
    max_turns: int
    add_dirs: List[str]
    allowed_tools: List[str]
    disallowed_tools: List[str]
    append_system_prompt: str
    permission_mode: str
    dangerously_skip_permissions: bool
    repo_root_relative_path: str
    paper_summary: Dict[str, Any]
    readme_intake: Dict[str, Any]
    task_brief_relative_path: str
    task_brief_content: str
    execution_spec: Dict[str, Any]
    execution_spec_relative_path: str
    launch_result: Dict[str, Any]
    launch_summary: str


class ClaudeCodeCollaborationGraphService:
    def __init__(self, runtime_service: Optional[ProjectRuntimeService] = None) -> None:
        self.runtime_service = runtime_service or ProjectRuntimeService()

    async def launch(
        self,
        *,
        project_id: int,
        workspace_id: int,
        workspace_dir: Path,
        project_title: str = "",
        task: str,
        execution_id: Optional[str] = None,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        add_dirs: Optional[List[str]] = None,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        append_system_prompt: str = "",
        permission_mode: str = "",
        dangerously_skip_permissions: bool = True,
    ) -> Dict[str, Any]:
        initial_state: ClaudeCodeCollaborationState = {
            "project_id": int(project_id),
            "workspace_id": int(workspace_id),
            "workspace_dir": str(Path(workspace_dir)),
            "project_title": str(project_title or "").strip(),
            "execution_id": _safe_slug(
                execution_id or f"claude-code-{uuid.uuid4().hex[:8]}",
                fallback="claude-code",
            ),
            "task": str(task or "").strip(),
            "model": str(model or settings.claude_code_default_model or "").strip(),
            "max_turns": int(max_turns or settings.claude_code_default_max_turns or 24),
            "add_dirs": list(add_dirs or []),
            "allowed_tools": [str(item).strip() for item in list(allowed_tools or []) if str(item).strip()],
            "disallowed_tools": [str(item).strip() for item in list(disallowed_tools or []) if str(item).strip()],
            "append_system_prompt": str(append_system_prompt or "").strip(),
            "permission_mode": str(permission_mode or "").strip(),
            "dangerously_skip_permissions": bool(dangerously_skip_permissions),
        }
        graph = self._build_launch_graph()
        if graph is None:
            return await self._run_launch_pipeline(initial_state)
        return await graph.ainvoke(initial_state)

    def _build_launch_graph(self):  # type: ignore[no-untyped-def]
        try:
            from langgraph.graph import END, START, StateGraph
        except Exception:
            return None
        graph = StateGraph(ClaudeCodeCollaborationState)
        graph.add_node("load_context", self._load_context_node)
        graph.add_node("write_task_brief", self._write_task_brief_node)
        graph.add_node("write_execution_spec", self._write_execution_spec_node)
        graph.add_node("start_execution", self._start_execution_node)
        graph.add_node("summarize_launch", self._summarize_launch_node)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "write_task_brief")
        graph.add_edge("write_task_brief", "write_execution_spec")
        graph.add_edge("write_execution_spec", "start_execution")
        graph.add_edge("start_execution", "summarize_launch")
        graph.add_edge("summarize_launch", END)
        return graph.compile()

    async def _run_launch_pipeline(
        self,
        state: ClaudeCodeCollaborationState,
    ) -> ClaudeCodeCollaborationState:
        merged = dict(state)
        for step in (
            self._load_context_node,
            self._write_task_brief_node,
            self._write_execution_spec_node,
        ):
            merged.update(step(merged))
        merged.update(await self._start_execution_node(merged))
        merged.update(self._summarize_launch_node(merged))
        return merged

    def _load_context_node(self, state: ClaudeCodeCollaborationState) -> ClaudeCodeCollaborationState:
        workspace_dir = Path(str(state["workspace_dir"]))
        repo_root_relative_path = self.runtime_service._to_workspace_relative(  # noqa: SLF001
            workspace_dir,
            self.runtime_service.resolve_repo_root(workspace_dir),
        ) or "repo/source"
        return {
            "repo_root_relative_path": repo_root_relative_path,
            "paper_summary": self._read_json(workspace_dir / "paper_summary.json"),
            "readme_intake": self._read_json(workspace_dir / "repo_readme_reproduction_intake.json"),
        }

    def _write_task_brief_node(self, state: ClaudeCodeCollaborationState) -> ClaudeCodeCollaborationState:
        workspace_dir = Path(str(state["workspace_dir"]))
        execution_id = str(state["execution_id"])
        relative_path = f"executions/{execution_id}/claude_task_brief.md"
        target = workspace_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = self._render_task_brief(state)
        target.write_text(content, encoding="utf-8")
        return {
            "task_brief_relative_path": relative_path,
            "task_brief_content": content,
        }

    def _write_execution_spec_node(self, state: ClaudeCodeCollaborationState) -> ClaudeCodeCollaborationState:
        workspace_dir = Path(str(state["workspace_dir"]))
        execution_spec = {
            "execution_id": str(state["execution_id"]),
            "label": str(state.get("project_title") or state["execution_id"]),
            "runtime_type": "claude_code",
            "repo_root_relative_path": str(state.get("repo_root_relative_path") or "repo/source"),
            "cwd": str(state.get("repo_root_relative_path") or "repo/source"),
            "task_brief_relative_path": str(state["task_brief_relative_path"]),
            "model": str(state.get("model") or settings.claude_code_default_model or "").strip(),
            "max_turns": int(state.get("max_turns") or settings.claude_code_default_max_turns or 24),
            "append_system_prompt": str(state.get("append_system_prompt") or "").strip(),
            "permission_mode": str(state.get("permission_mode") or "").strip(),
            "dangerously_skip_permissions": bool(state.get("dangerously_skip_permissions", True)),
            "add_dirs": list(state.get("add_dirs") or []),
            "allowed_tools": list(state.get("allowed_tools") or []),
            "disallowed_tools": list(state.get("disallowed_tools") or []),
        }
        saved = self.runtime_service.write_execution_spec(
            workspace_dir=workspace_dir,
            project_id=int(state["project_id"]),
            workspace_id=int(state["workspace_id"]),
            notebook_id="",
            execution_spec=execution_spec,
        )
        return {
            "execution_spec": dict(saved.get("content") or {}),
            "execution_spec_relative_path": str(saved.get("relative_path") or ""),
        }

    async def _start_execution_node(self, state: ClaudeCodeCollaborationState) -> ClaudeCodeCollaborationState:
        workspace_dir = Path(str(state["workspace_dir"]))
        launch_result = await self.runtime_service.start_execution(
            project_id=int(state["project_id"]),
            workspace_id=int(state["workspace_id"]),
            workspace_dir=workspace_dir,
            execution_id=str(state["execution_id"]),
        )
        return {"launch_result": launch_result}

    def _summarize_launch_node(self, state: ClaudeCodeCollaborationState) -> ClaudeCodeCollaborationState:
        launch = dict(state.get("launch_result") or {})
        lines = [
            "Claude Code collaboration launched.",
            f"- Execution ID: {state.get('execution_id')}",
            f"- Runtime type: claude_code",
            f"- Status: {launch.get('status')}",
            f"- Task brief: {state.get('task_brief_relative_path')}",
            f"- Execution spec: {state.get('execution_spec_relative_path')}",
        ]
        if launch.get("message"):
            lines.append(f"- Message: {launch.get('message')}")
        if launch.get("error"):
            lines.append(f"- Error: {launch.get('error')}")
        return {"launch_summary": "\n".join(lines)}

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")) or {})
        except Exception:
            return {}

    def _render_task_brief(self, state: ClaudeCodeCollaborationState) -> str:
        paper_summary = dict(state.get("paper_summary") or {})
        readme_intake = dict(state.get("readme_intake") or {})
        lines = [
            "# Claude Code Task Brief",
            "",
            f"Project ID: {state['project_id']}",
            f"Execution ID: {state['execution_id']}",
            f"Project title: {state.get('project_title') or 'Unknown project'}",
            "",
            "## Primary task",
            str(state.get("task") or "").strip() or "Read the repository, determine the minimal runnable path, and execute the requested repo-backed reproduction or implementation task.",
            "",
            "## Repo-first expectations",
            "- Work directly in the repository root unless the task brief says otherwise.",
            "- Prefer running the simplest documented path first.",
            "- Fix concrete runtime or code issues based on real failures instead of speculative refactors.",
            "- Keep edits within the project workspace.",
            "",
        ]
        if paper_summary:
            lines.extend(
                [
                    "## Paper summary signals",
                    f"- Problem: {paper_summary.get('problem_definition') or 'unknown'}",
                    f"- Method: {paper_summary.get('core_method') or 'unknown'}",
                    f"- Reproduction risks: {', '.join(list(paper_summary.get('reproduction_risks') or [])[:4]) or 'none recorded'}",
                    "",
                ]
            )
        if readme_intake:
            lines.extend(
                [
                    "## README reproduction intake",
                    f"- Goal: {readme_intake.get('reproduction_goal') or 'unknown'}",
                    f"- Installation: {self._join_items(readme_intake.get('installation_steps'))}",
                    f"- Run scripts: {self._join_items(readme_intake.get('run_scripts'))}",
                    f"- Entrypoints: {self._join_items(readme_intake.get('entrypoints'))}",
                    f"- Environment: {self._join_items(readme_intake.get('environment_requirements'))}",
                    "",
                ]
            )
        lines.extend(
            [
                "## Expected output back to controller",
                "- Use the repository directly and make the smallest necessary changes.",
                "- Leave a clear execution trail in logs and files.",
                "- If blocked, surface the blocker with the exact command/error context.",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _join_items(value: Any) -> str:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return ", ".join(items[:6]) if items else "none"
        if isinstance(value, dict):
            items = [f"{key}={val}" for key, val in list(value.items())[:6]]
            return ", ".join(items) if items else "none"
        text = str(value or "").strip()
        return text or "none"
