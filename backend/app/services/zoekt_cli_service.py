from __future__ import annotations

import asyncio
import base64
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings


@dataclass
class ZoektBinarySet:
    search: Optional[str]
    git_index: Optional[str]
    plain_index: Optional[str]


class ZoektCliService:
    """Manage local Zoekt indexing and search for a workspace repo."""

    INDEX_ROOT_DIRNAME = ".zoekt"
    INDEX_DIRNAME = "index"
    MANIFEST_NAME = "manifest.json"

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def workspace_root(cls, workspace_dir: Path) -> Path:
        return Path(workspace_dir) / cls.INDEX_ROOT_DIRNAME

    @classmethod
    def index_dir(cls, workspace_dir: Path) -> Path:
        return cls.workspace_root(workspace_dir) / cls.INDEX_DIRNAME

    @classmethod
    def manifest_path(cls, workspace_dir: Path) -> Path:
        return cls.workspace_root(workspace_dir) / cls.MANIFEST_NAME

    @classmethod
    def _resolve_binary_set(cls) -> ZoektBinarySet:
        search_name = str(getattr(settings, "zoekt_search_binary", "zoekt") or "zoekt").strip()
        git_index_name = str(getattr(settings, "zoekt_git_index_binary", "zoekt-git-index") or "zoekt-git-index").strip()
        plain_index_name = str(getattr(settings, "zoekt_index_binary", "zoekt-index") or "zoekt-index").strip()
        return ZoektBinarySet(
            search=shutil.which(search_name) or None,
            git_index=shutil.which(git_index_name) or None,
            plain_index=shutil.which(plain_index_name) or None,
        )

    @classmethod
    def availability(cls) -> Dict[str, Any]:
        binaries = cls._resolve_binary_set()
        return {
            "available": bool(binaries.search and (binaries.git_index or binaries.plain_index)),
            "search_binary": binaries.search,
            "git_index_binary": binaries.git_index,
            "plain_index_binary": binaries.plain_index,
        }

    @classmethod
    def _read_manifest(cls, workspace_dir: Path) -> Dict[str, Any]:
        path = cls.manifest_path(workspace_dir)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _write_manifest(cls, workspace_dir: Path, payload: Dict[str, Any]) -> None:
        root = cls.workspace_root(workspace_dir)
        root.mkdir(parents=True, exist_ok=True)
        cls.manifest_path(workspace_dir).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def _count_index_files(cls, workspace_dir: Path) -> int:
        index_dir = cls.index_dir(workspace_dir)
        if not index_dir.is_dir():
            return 0
        return sum(1 for path in index_dir.rglob("*.zoekt") if path.is_file())

    @classmethod
    async def _run_command(
        cls,
        *,
        command: List[str],
        cwd: Optional[Path],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return {
                "timeout": True,
                "returncode": None,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "command": command,
            }
        return {
            "timeout": False,
            "returncode": int(process.returncode or 0),
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "command": command,
        }

    @classmethod
    async def _git_head(cls, repo_dir: Path) -> Optional[str]:
        if not shutil.which("git"):
            return None
        result = await cls._run_command(
            command=["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            timeout_seconds=10.0,
        )
        returncode = result.get("returncode")
        if result.get("timeout") or returncode is None or int(returncode) != 0:
            return None
        value = str(result.get("stdout") or "").strip()
        return value or None

    @classmethod
    async def _git_dirty(cls, repo_dir: Path) -> Optional[bool]:
        if not shutil.which("git"):
            return None
        result = await cls._run_command(
            command=["git", "status", "--short", "--untracked-files=no"],
            cwd=repo_dir,
            timeout_seconds=10.0,
        )
        returncode = result.get("returncode")
        if result.get("timeout") or returncode is None or int(returncode) != 0:
            return None
        return bool(str(result.get("stdout") or "").strip())

    @classmethod
    async def build_index(
        cls,
        *,
        repo_dir: Path,
        workspace_dir: Path,
        force_reindex: bool = False,
    ) -> Dict[str, Any]:
        binaries = cls._resolve_binary_set()
        if not (binaries.git_index or binaries.plain_index):
            return {
                "success": False,
                "available": False,
                "error": "zoekt_index_binary_not_installed",
                **cls.availability(),
            }

        repo_dir = Path(repo_dir)
        workspace_dir = Path(workspace_dir)
        index_dir = cls.index_dir(workspace_dir)
        manifest = cls._read_manifest(workspace_dir)
        repo_head = await cls._git_head(repo_dir)
        repo_dirty = await cls._git_dirty(repo_dir)
        existing_index_files = cls._count_index_files(workspace_dir)

        if (
            not force_reindex
            and existing_index_files > 0
            and manifest.get("repo_dir") == str(repo_dir)
            and manifest.get("repo_head") == repo_head
            and not bool(repo_dirty)
        ):
            return {
                "success": True,
                "available": True,
                "status": "reused",
                "index_dir": str(index_dir),
                "manifest_path": str(cls.manifest_path(workspace_dir)),
                "repo_dir": str(repo_dir),
                "repo_head": repo_head,
                "repo_dirty": bool(repo_dirty) if repo_dirty is not None else None,
                "index_file_count": existing_index_files,
                **cls.availability(),
            }

        if index_dir.exists():
            shutil.rmtree(index_dir, ignore_errors=True)
        index_dir.mkdir(parents=True, exist_ok=True)

        if (repo_dir / ".git").exists() and binaries.git_index:
            command = [
                str(binaries.git_index),
                "-disable_ctags",
                "-index",
                str(index_dir),
                str(repo_dir),
            ]
            engine = "zoekt-git-index"
        else:
            plain_index_binary = binaries.plain_index
            if not plain_index_binary:
                return {
                    "success": False,
                    "available": False,
                    "error": "zoekt_plain_index_binary_not_installed",
                    **cls.availability(),
                }
            command = [
                str(plain_index_binary),
                "-disable_ctags",
                "-index",
                str(index_dir),
                str(repo_dir),
            ]
            engine = "zoekt-index"

        result = await cls._run_command(
            command=command,
            cwd=repo_dir,
            timeout_seconds=float(getattr(settings, "zoekt_index_timeout_seconds", 180) or 180),
        )
        if result.get("timeout"):
            return {
                "success": False,
                "available": True,
                "error": "zoekt_index_timeout",
                "engine": engine,
                "index_dir": str(index_dir),
                "stdout": str(result.get("stdout") or ""),
                "stderr": str(result.get("stderr") or ""),
                **cls.availability(),
            }
        returncode = result.get("returncode")
        if returncode is None or int(returncode) != 0:
            return {
                "success": False,
                "available": True,
                "error": "zoekt_index_failed",
                "engine": engine,
                "index_dir": str(index_dir),
                "stdout": str(result.get("stdout") or ""),
                "stderr": str(result.get("stderr") or ""),
                **cls.availability(),
            }

        payload = {
            "schema_version": "zoekt_manifest_v1",
            "engine": engine,
            "repo_dir": str(repo_dir),
            "repo_head": repo_head,
            "repo_dirty": bool(repo_dirty) if repo_dirty is not None else None,
            "index_dir": str(index_dir),
            "index_file_count": cls._count_index_files(workspace_dir),
            "built_at": cls._utcnow_iso(),
            "command": command,
        }
        cls._write_manifest(workspace_dir, payload)
        return {
            "success": True,
            "available": True,
            "status": "rebuilt" if existing_index_files > 0 else "created",
            "manifest_path": str(cls.manifest_path(workspace_dir)),
            **payload,
            **cls.availability(),
        }

    @staticmethod
    def _decode_bytes_field(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            return str(value)
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception:
            return value
        return decoded.decode("utf-8", errors="replace")

    @classmethod
    def _flatten_file_match(cls, file_match: Dict[str, Any]) -> List[Dict[str, Any]]:
        repo_relative_path = str(file_match.get("FileName") or "").strip()
        if not repo_relative_path:
            return []
        line_matches = list(file_match.get("LineMatches") or [])
        if not line_matches:
            return [
                {
                    "repo_relative_path": repo_relative_path,
                    "relative_path": f"repo/source/{repo_relative_path}",
                    "repository": str(file_match.get("Repository") or "").strip() or None,
                    "branches": [str(item).strip() for item in list(file_match.get("Branches") or []) if str(item).strip()],
                    "line_number": 0,
                    "line_text": repo_relative_path,
                    "line_fragments": [],
                    "match_source": "filename",
                    "score": float(file_match.get("Score") or 0.0),
                }
            ]

        flattened: List[Dict[str, Any]] = []
        for line_match in line_matches:
            decoded_line = cls._decode_bytes_field(line_match.get("Line"))
            fragments: List[Dict[str, Any]] = []
            for fragment in list(line_match.get("LineFragments") or []):
                line_offset = int(fragment.get("LineOffset") or 0)
                match_length = int(fragment.get("MatchLength") or 0)
                fragments.append(
                    {
                        "line_offset": line_offset,
                        "match_length": match_length,
                        "text": decoded_line[line_offset : line_offset + match_length] if match_length > 0 else "",
                    }
                )
            flattened.append(
                {
                    "repo_relative_path": repo_relative_path,
                    "relative_path": f"repo/source/{repo_relative_path}",
                    "repository": str(file_match.get("Repository") or "").strip() or None,
                    "branches": [str(item).strip() for item in list(file_match.get("Branches") or []) if str(item).strip()],
                    "line_number": int(line_match.get("LineNumber") or 0),
                    "line_text": decoded_line.strip("\n"),
                    "line_fragments": fragments,
                    "match_source": "content",
                    "score": float(line_match.get("Score") or file_match.get("Score") or 0.0),
                }
            )
        return flattened

    @classmethod
    async def search(
        cls,
        *,
        workspace_dir: Path,
        query: str,
        max_results: int,
    ) -> Dict[str, Any]:
        binaries = cls._resolve_binary_set()
        if not binaries.search:
            return {
                "success": False,
                "available": False,
                "error": "zoekt_search_binary_not_installed",
                **cls.availability(),
            }

        workspace_dir = Path(workspace_dir)
        index_dir = cls.index_dir(workspace_dir)
        if cls._count_index_files(workspace_dir) <= 0:
            return {
                "success": False,
                "available": True,
                "error": "zoekt_index_missing",
                "index_dir": str(index_dir),
                **cls.availability(),
            }

        command = [str(binaries.search), "-jsonl", "-index_dir", str(index_dir), str(query)]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        matches: List[Dict[str, Any]] = []
        matched_files: List[str] = []
        parse_errors = 0
        truncated = False
        stderr_bytes: bytes = b""
        search_timeout = float(getattr(settings, "zoekt_search_timeout_seconds", 20) or 20)

        async def _collect_output() -> None:
            nonlocal truncated, parse_errors, stderr_bytes
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8", errors="ignore"))
                except Exception:
                    parse_errors += 1
                    continue
                flattened = cls._flatten_file_match(payload if isinstance(payload, dict) else {})
                if not flattened:
                    continue
                relative_path = str(flattened[0].get("repo_relative_path") or "").strip()
                if relative_path:
                    matched_files.append(relative_path)
                for item in flattened:
                    matches.append(item)
                    if len(matches) >= max_results:
                        truncated = True
                        process.terminate()
                        break
                if truncated:
                    break
            try:
                stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=2.0) if process.stderr else b""
            except asyncio.TimeoutError:
                stderr_bytes = b""
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        try:
            await asyncio.wait_for(_collect_output(), timeout=search_timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "success": False,
                "available": True,
                "error": "zoekt_search_timeout",
                "query": query,
                "matches": matches[:max_results],
                "matched_files": list(dict.fromkeys(path for path in matched_files if path)),
                "returned_matches": min(len(matches), max_results),
                "parse_errors": parse_errors,
                "index_dir": str(index_dir),
                "manifest_path": str(cls.manifest_path(workspace_dir)),
                **cls.availability(),
            }
        except Exception:
            process.kill()
            await process.wait()
            raise

        return {
            "success": True,
            "available": True,
            "engine": "zoekt",
            "query": query,
            "matches": matches[:max_results],
            "matched_files": list(dict.fromkeys(path for path in matched_files if path)),
            "returned_matches": min(len(matches), max_results),
            "truncated": truncated,
            "parse_errors": parse_errors,
            "stderr": stderr_bytes.decode("utf-8", errors="replace") if isinstance(stderr_bytes, (bytes, bytearray)) else "",
            "index_dir": str(index_dir),
            "manifest_path": str(cls.manifest_path(workspace_dir)),
            **cls.availability(),
        }
