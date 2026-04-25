from __future__ import annotations

import asyncio
import difflib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.config import settings


def _normalize_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return ""
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, indent=2, default=str)


class AiderCliService:
    """Run aider as an isolated CLI tool and persist run artifacts in the workspace."""

    RUNS_DIRNAME = "aider_runs"
    MAX_ARTIFACT_CHARS = 120_000
    _ARTIFACT_ALIAS_MAP = {
        "paper_pdf2md": "reference/paper/paper_pdf2md.md",
        "paper_interpretation": "reference/paper/paper_interpretation.md",
        "paper_interpretation_json": "reference/paper/paper_interpretation.json",
        "readme_intake": "reference/repo/readme_intake.json",
    }

    @classmethod
    def _resolve_binary(cls) -> Optional[str]:
        configured = str(getattr(settings, "aider_binary", "aider") or "aider").strip() or "aider"
        if os.path.isabs(configured):
            return configured if Path(configured).is_file() else None
        return shutil.which(configured)

    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        normalized = str(value or "code").strip().lower()
        return normalized if normalized in {"code", "architect", "ask"} else "code"

    @classmethod
    def _normalize_target_root(cls, value: Any) -> str:
        normalized = str(value or "repo").strip().lower()
        return normalized if normalized in {"repo", "workspace"} else "repo"

    @classmethod
    def _normalize_paths(cls, values: Sequence[Any]) -> List[str]:
        seen: Dict[str, None] = {}
        for raw in list(values or []):
            normalized = _normalize_relative_path(raw)
            if normalized:
                seen.setdefault(normalized, None)
        return list(seen.keys())

    @classmethod
    def _normalize_artifact_refs(cls, values: Sequence[Any]) -> List[str]:
        refs: Dict[str, None] = {}
        for raw in list(values or []):
            text = str(raw or "").strip()
            if not text:
                continue
            mapped = cls._ARTIFACT_ALIAS_MAP.get(text, text)
            normalized = _normalize_relative_path(mapped)
            if normalized:
                refs.setdefault(normalized, None)
        return list(refs.keys())

    @classmethod
    def _run_dir(cls, workspace_dir: Path, run_id: str) -> Path:
        return workspace_dir / cls.RUNS_DIRNAME / run_id

    @classmethod
    def _resolve_target_dir(cls, workspace_dir: Path, target_root: str) -> Path:
        return workspace_dir / "paper_repo" if target_root == "repo" else workspace_dir

    @classmethod
    def _resolve_model_identifier(cls, *, provider: str, model_name: Optional[str]) -> Dict[str, str]:
        provider_key = str(provider or "").strip() or str(settings.default_llm_provider or "openai")
        config = dict(settings.get_llm_config(provider_key) or {})
        resolved_model = str(model_name or config.get("model") or "").strip()
        api_key = str(config.get("api_key") or "").strip()
        api_base = str(config.get("base_url") or "").strip()
        if not resolved_model or not api_key or not api_base:
            raise ValueError(f"aider provider config incomplete: provider={provider_key}")
        if "/" in resolved_model:
            aider_model = resolved_model
        else:
            aider_model = f"openai/{resolved_model}"
        return {
            "provider": provider_key,
            "model": resolved_model,
            "aider_model": aider_model,
            "api_key": api_key,
            "api_base": api_base,
        }

    @classmethod
    async def _run_subprocess(
        cls,
        *,
        command: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *list(command),
            cwd=str(cwd),
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, _ = await process.communicate()
            return {
                "timeout": True,
                "returncode": None,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "command": list(command),
            }
        return {
            "timeout": False,
            "returncode": int(process.returncode or 0),
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "command": list(command),
        }

    @classmethod
    async def _git_status_porcelain(cls, repo_dir: Path) -> Dict[str, str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "--no-pager",
            "status",
            "--short",
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        return {
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        }

    @classmethod
    async def _git_diff_patch(cls, repo_dir: Path) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "--no-pager",
            "diff",
            "--binary",
            "--no-ext-diff",
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await process.communicate()
        return stdout_bytes.decode("utf-8", errors="replace")

    @classmethod
    def _snapshot_files(cls, root_dir: Path, relative_paths: Sequence[str]) -> Dict[str, Optional[str]]:
        snapshots: Dict[str, Optional[str]] = {}
        for relative_path in list(relative_paths or []):
            normalized = _normalize_relative_path(relative_path)
            if not normalized:
                continue
            path = root_dir / normalized
            if path.is_file():
                snapshots[normalized] = path.read_text(encoding="utf-8", errors="replace")
            else:
                snapshots[normalized] = None
        return snapshots

    @classmethod
    def _workspace_diff(
        cls,
        *,
        root_dir: Path,
        before: Mapping[str, Optional[str]],
    ) -> tuple[List[str], str]:
        changed_files: List[str] = []
        diff_chunks: List[str] = []
        for relative_path, before_text in before.items():
            path = root_dir / relative_path
            after_text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            if before_text == after_text:
                continue
            changed_files.append(relative_path)
            before_lines = [] if before_text is None else before_text.splitlines(keepends=True)
            after_lines = [] if after_text is None else after_text.splitlines(keepends=True)
            diff_chunks.append(
                "".join(
                    difflib.unified_diff(
                        before_lines,
                        after_lines,
                        fromfile=f"a/{relative_path}",
                        tofile=f"b/{relative_path}",
                    )
                )
            )
        return changed_files, "".join(diff_chunks)

    @classmethod
    def _load_context_blocks(
        cls,
        *,
        workspace_dir: Path,
        artifact_refs: Sequence[str],
    ) -> List[Dict[str, str]]:
        blocks: List[Dict[str, str]] = []
        for relative_path in list(artifact_refs or []):
            path = workspace_dir / relative_path
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > cls.MAX_ARTIFACT_CHARS:
                content = content[: cls.MAX_ARTIFACT_CHARS]
            blocks.append(
                {
                    "relative_path": relative_path,
                    "content": content,
                }
            )
        return blocks

    @classmethod
    def _build_prompt(
        cls,
        *,
        instruction: str,
        target_root: str,
        editable_files: Sequence[str],
        read_only_files: Sequence[str],
        context_blocks: Sequence[Mapping[str, str]],
        mode: str,
    ) -> str:
        lines = [
            "You are running through a controlled aider wrapper inside a paper-reproduction workspace.",
            f"Mode: {mode}",
            f"Target root: {target_root}",
            "",
            "Primary task:",
            instruction.strip(),
            "",
            "Hard constraints:",
            "- Make the smallest defensible change.",
            "- Preserve unrelated behavior, formatting, comments, and JSON key structure whenever possible.",
            "- If editing JSON/YAML/TOML/Markdown, update only the relevant local sections instead of rewriting the whole file.",
            "- Do not create commits; the wrapper has disabled aider auto-commit behavior.",
            "- If the request is blocked or ambiguous, explain that in your final output instead of inventing changes.",
        ]
        if editable_files:
            lines.extend(["", "Editable files:"])
            lines.extend([f"- {item}" for item in editable_files])
        else:
            lines.extend(
                [
                    "",
                    "Editable files:",
                    "- No explicit editable file list was provided. Select the minimal file set yourself.",
                ]
            )
        if read_only_files:
            lines.extend(["", "Read-only files:"])
            lines.extend([f"- {item}" for item in read_only_files])
        if context_blocks:
            lines.append("")
            lines.append("Workspace context artifacts:")
            for block in context_blocks:
                lines.append(f"--- BEGIN {block['relative_path']} ---")
                lines.append(str(block["content"]))
                lines.append(f"--- END {block['relative_path']} ---")
        lines.append("")
        lines.append("If no code or file change is needed, say so clearly and leave files untouched.")
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _build_command(
        cls,
        *,
        binary: str,
        model_identifier: Mapping[str, str],
        prompt_path: Path,
        run_dir: Path,
        target_root: str,
        mode: str,
        editable_files: Sequence[str],
        read_only_files: Sequence[str],
        dry_run: bool,
        map_tokens: int,
        api_timeout_seconds: int,
        editor_model: Optional[str],
        weak_model: Optional[str],
        edit_format: Optional[str],
        editor_edit_format: Optional[str],
        reasoning_effort: Optional[str],
        auto_test: bool,
        test_cmd: Optional[str],
        auto_lint: bool,
        lint_cmds: Sequence[str],
    ) -> List[str]:
        command = [
            binary,
            "--model",
            str(model_identifier["aider_model"]),
            "--message-file",
            str(prompt_path),
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-attribute-author",
            "--no-attribute-committer",
            "--no-attribute-co-authored-by",
            "--no-analytics",
            "--no-check-update",
            "--no-show-release-notes",
            "--no-show-model-warnings",
            "--no-check-model-accepts-settings",
            "--no-fancy-input",
            "--encoding",
            "utf-8",
            "--input-history-file",
            str(run_dir / ".aider.input.history"),
            "--chat-history-file",
            str(run_dir / ".aider.chat.history.md"),
            "--llm-history-file",
            str(run_dir / ".aider.llm.history.jsonl"),
            "--map-tokens",
            str(int(map_tokens)),
            "--timeout",
            str(int(api_timeout_seconds)),
        ]
        if target_root == "workspace":
            command.extend(["--no-git", "--skip-sanity-check-repo"])
        if mode == "architect":
            command.append("--architect")
        elif mode == "ask":
            command.extend(["--chat-mode", "ask"])
        if dry_run:
            command.append("--dry-run")
        if editor_model:
            command.extend(["--editor-model", cls._normalize_model_arg(editor_model)])
        if weak_model:
            command.extend(["--weak-model", cls._normalize_model_arg(weak_model)])
        if edit_format:
            command.extend(["--edit-format", str(edit_format).strip()])
        if editor_edit_format:
            command.extend(["--editor-edit-format", str(editor_edit_format).strip()])
        if reasoning_effort:
            command.extend(["--reasoning-effort", str(reasoning_effort).strip()])
        if auto_test and test_cmd:
            command.extend(["--auto-test", "--test-cmd", test_cmd])
        elif test_cmd:
            command.extend(["--test-cmd", test_cmd])
        if auto_lint:
            command.append("--auto-lint")
        else:
            command.append("--no-auto-lint")
        for lint_cmd in list(lint_cmds or []):
            text = str(lint_cmd or "").strip()
            if text:
                command.extend(["--lint-cmd", text])
        for relative_path in list(editable_files or []):
            command.extend(["--file", relative_path])
        for relative_path in list(read_only_files or []):
            command.extend(["--read", relative_path])
        return command

    @classmethod
    def _normalize_model_arg(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text if "/" in text else f"openai/{text}"

    @classmethod
    def _redact_command(cls, command: Sequence[str]) -> List[str]:
        redacted: List[str] = []
        skip_next = False
        sensitive_flags = {"--openai-api-key", "--api-key"}
        for item in list(command or []):
            text = str(item)
            if skip_next:
                redacted.append("***redacted***")
                skip_next = False
                continue
            if text in sensitive_flags:
                redacted.append(text)
                skip_next = True
                continue
            redacted.append(text)
        return redacted

    @classmethod
    async def run(
        cls,
        *,
        workspace_dir: Path,
        instruction: str,
        target_root: str = "repo",
        editable_files: Sequence[Any] = (),
        read_only_files: Sequence[Any] = (),
        context_artifacts: Sequence[Any] = (),
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        editor_model: Optional[str] = None,
        weak_model: Optional[str] = None,
        mode: str = "code",
        edit_format: Optional[str] = None,
        editor_edit_format: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        dry_run: bool = False,
        map_tokens: Optional[int] = None,
        api_timeout_seconds: Optional[int] = None,
        auto_test: bool = False,
        test_cmd: Optional[str] = None,
        auto_lint: bool = False,
        lint_cmds: Sequence[Any] = (),
        allow_dirty_repo: bool = False,
    ) -> Dict[str, Any]:
        binary = cls._resolve_binary()
        normalized_target_root = cls._normalize_target_root(target_root)
        normalized_mode = cls._normalize_mode(mode)
        normalized_editable = cls._normalize_paths(editable_files)
        normalized_read_only = cls._normalize_paths(read_only_files)
        normalized_context_artifacts = cls._normalize_artifact_refs(context_artifacts)
        normalized_provider = str(provider or settings.default_llm_provider or "openai").strip()
        timeout_seconds = int(api_timeout_seconds or getattr(settings, "aider_api_timeout_seconds", 300) or 300)
        process_timeout = int(getattr(settings, "aider_timeout_seconds", 900) or 900)
        map_budget = int(map_tokens if map_tokens is not None else getattr(settings, "aider_map_tokens", 1024) or 1024)
        lint_commands = [str(item or "").strip() for item in list(lint_cmds or []) if str(item or "").strip()]
        run_id = uuid.uuid4().hex[:12]
        run_dir = cls._run_dir(workspace_dir, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = run_dir / "prompt.md"
        stdout_path = run_dir / "stdout.log"
        metadata_path = run_dir / "run.json"
        diff_path = run_dir / "changes.patch"

        if binary is None:
            payload = {
                "success": False,
                "error": "aider_binary_not_installed",
                "message": "aider binary not found; rebuild backend image to install the isolated aider tool.",
                "run_id": run_id,
                "run_dir": str(run_dir),
            }
            metadata_path.write_text(_json_dumps(payload), encoding="utf-8")
            return payload

        if not str(instruction or "").strip():
            payload = {
                "success": False,
                "error": "instruction_required",
                "message": "instruction is required",
                "run_id": run_id,
                "run_dir": str(run_dir),
            }
            metadata_path.write_text(_json_dumps(payload), encoding="utf-8")
            return payload

        target_dir = cls._resolve_target_dir(workspace_dir, normalized_target_root)
        if not target_dir.is_dir():
            payload = {
                "success": False,
                "error": "target_root_missing",
                "message": f"target root does not exist: {normalized_target_root}",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "target_root": normalized_target_root,
            }
            metadata_path.write_text(_json_dumps(payload), encoding="utf-8")
            return payload

        if normalized_target_root == "workspace" and not normalized_editable:
            payload = {
                "success": False,
                "error": "workspace_editable_files_required",
                "message": "workspace mode requires explicit editable_files to avoid broad accidental rewrites.",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "target_root": normalized_target_root,
            }
            metadata_path.write_text(_json_dumps(payload), encoding="utf-8")
            return payload

        if normalized_target_root == "repo" and not allow_dirty_repo:
            status = await cls._git_status_porcelain(target_dir)
            if str(status.get("stdout") or "").strip():
                payload = {
                    "success": False,
                    "error": "repo_dirty",
                    "message": "repo has uncommitted changes; rerun with allow_dirty_repo=true if you really want aider to edit this dirty repo.",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "target_root": normalized_target_root,
                    "git_status": str(status.get("stdout") or ""),
                }
                metadata_path.write_text(_json_dumps(payload), encoding="utf-8")
                return payload

        model_identifier = cls._resolve_model_identifier(provider=normalized_provider, model_name=model_name)
        before_snapshots = cls._snapshot_files(target_dir, normalized_editable)
        context_blocks = cls._load_context_blocks(workspace_dir=workspace_dir, artifact_refs=normalized_context_artifacts)
        prompt_text = cls._build_prompt(
            instruction=str(instruction or ""),
            target_root=normalized_target_root,
            editable_files=normalized_editable,
            read_only_files=normalized_read_only,
            context_blocks=context_blocks,
            mode=normalized_mode,
        )
        prompt_path.write_text(prompt_text, encoding="utf-8")

        command = cls._build_command(
            binary=binary,
            model_identifier=model_identifier,
            prompt_path=prompt_path,
            run_dir=run_dir,
            target_root=normalized_target_root,
            mode=normalized_mode,
            editable_files=normalized_editable,
            read_only_files=normalized_read_only,
            dry_run=bool(dry_run),
            map_tokens=map_budget,
            api_timeout_seconds=timeout_seconds,
            editor_model=editor_model,
            weak_model=weak_model,
            edit_format=edit_format,
            editor_edit_format=editor_edit_format,
            reasoning_effort=reasoning_effort,
            auto_test=bool(auto_test),
            test_cmd=str(test_cmd or "").strip() or None,
            auto_lint=bool(auto_lint),
            lint_cmds=lint_commands,
        )

        env = {
            **os.environ,
            "AIDER_DISABLE_PLAYWRIGHT": "1",
            "AIDER_OPENAI_API_KEY": str(model_identifier["api_key"]),
            "AIDER_OPENAI_API_BASE": str(model_identifier["api_base"]),
            "OPENAI_API_KEY": str(model_identifier["api_key"]),
            "OPENAI_API_BASE": str(model_identifier["api_base"]),
        }
        run_result = await cls._run_subprocess(
            command=command,
            cwd=target_dir,
            env=env,
            timeout_seconds=process_timeout,
        )
        stdout_text = str(run_result.get("stdout") or "")
        stdout_path.write_text(stdout_text, encoding="utf-8")

        if normalized_target_root == "repo":
            diff_text = await cls._git_diff_patch(target_dir)
            changed_files = [
                line.rstrip("\n")
                for line in str((await cls._git_status_porcelain(target_dir)).get("stdout") or "").splitlines()
                if line.strip()
            ]
            changed_file_paths = sorted(
                {
                    item[3:].strip()
                    for item in changed_files
                    if len(item) >= 4 and item[3:].strip()
                }
            )
        else:
            changed_file_paths, diff_text = cls._workspace_diff(root_dir=target_dir, before=before_snapshots)

        if diff_text.strip():
            diff_path.write_text(diff_text, encoding="utf-8")

        payload = {
            "success": not bool(run_result.get("timeout")) and int(run_result.get("returncode") or 0) == 0,
            "error": "aider_timeout" if bool(run_result.get("timeout")) else (None if int(run_result.get("returncode") or 0) == 0 else "aider_failed"),
            "message": "aider run completed" if not bool(run_result.get("timeout")) else "aider run timed out",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "target_root": normalized_target_root,
            "target_dir": str(target_dir),
            "mode": normalized_mode,
            "provider": normalized_provider,
            "model_name": model_identifier["model"],
            "aider_model": model_identifier["aider_model"],
            "command": cls._redact_command(command),
            "returncode": run_result.get("returncode"),
            "timed_out": bool(run_result.get("timeout")),
            "dry_run": bool(dry_run),
            "editable_files": normalized_editable,
            "read_only_files": normalized_read_only,
            "context_artifacts": normalized_context_artifacts,
            "changed_files": changed_file_paths,
            "changed_file_count": len(changed_file_paths),
            "stdout_path": str(stdout_path),
            "prompt_path": str(prompt_path),
            "metadata_path": str(metadata_path),
            "diff_path": str(diff_path) if diff_path.is_file() else "",
            "stdout_excerpt": stdout_text[-4000:] if len(stdout_text) > 4000 else stdout_text,
        }
        metadata_path.write_text(_json_dumps(payload), encoding="utf-8")
        return payload

    @classmethod
    def read_run(
        cls,
        *,
        workspace_dir: Path,
        run_id: str,
        include_stdout: bool = True,
        include_prompt: bool = False,
        include_diff: bool = False,
        max_chars: int = 20_000,
    ) -> Dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        run_dir = cls._run_dir(workspace_dir, normalized_run_id)
        metadata_path = run_dir / "run.json"
        if not metadata_path.is_file():
            return {
                "success": False,
                "error": "aider_run_not_found",
                "message": f"aider run not found: {normalized_run_id}",
                "run_id": normalized_run_id,
            }
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if include_stdout:
            stdout_path = Path(str(payload.get("stdout_path") or ""))
            if stdout_path.is_file():
                text = stdout_path.read_text(encoding="utf-8", errors="replace")
                payload["stdout"] = text[-int(max_chars):] if max_chars > 0 else text
        if include_prompt:
            prompt_path = Path(str(payload.get("prompt_path") or ""))
            if prompt_path.is_file():
                text = prompt_path.read_text(encoding="utf-8", errors="replace")
                payload["prompt"] = text[-int(max_chars):] if max_chars > 0 else text
        if include_diff:
            diff_path = Path(str(payload.get("diff_path") or ""))
            if diff_path.is_file():
                text = diff_path.read_text(encoding="utf-8", errors="replace")
                payload["diff"] = text[-int(max_chars):] if max_chars > 0 else text
        payload["success"] = bool(payload.get("success"))
        return payload

    @classmethod
    def tail_log(
        cls,
        *,
        workspace_dir: Path,
        run_id: str,
        max_chars: int = 12_000,
    ) -> Dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        run_dir = cls._run_dir(workspace_dir, normalized_run_id)
        metadata_path = run_dir / "run.json"
        if not metadata_path.is_file():
            return {
                "success": False,
                "error": "aider_run_not_found",
                "message": f"aider run not found: {normalized_run_id}",
                "run_id": normalized_run_id,
            }
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        stdout_path = Path(str(payload.get("stdout_path") or ""))
        if not stdout_path.is_file():
            return {
                "success": False,
                "error": "aider_stdout_missing",
                "message": f"aider stdout missing: {normalized_run_id}",
                "run_id": normalized_run_id,
            }
        text = stdout_path.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "run_id": normalized_run_id,
            "tail": text[-int(max_chars):] if max_chars > 0 else text,
            "stdout_path": str(stdout_path),
        }
