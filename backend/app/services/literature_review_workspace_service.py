from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class LiteratureReviewWorkspaceService:
    """Read-only manager for literature-review skill workspaces."""

    PREVIEW_SUFFIXES = {".md", ".json", ".txt"}

    def __init__(self, upload_root: Optional[Path] = None) -> None:
        self.upload_root = upload_root or self._default_upload_root()
        self.reviews_root = self.upload_root / "literature_reviews"

    @staticmethod
    def _default_upload_root() -> Path:
        configured = str(os.getenv("UPLOAD_DIR") or "").strip()
        if configured:
            return Path(os.path.abspath(configured))
        mounted = Path("/app/uploads")
        if mounted.exists():
            return mounted.resolve()
        return Path(os.path.abspath("./uploads"))

    @staticmethod
    def safe_id(value: Any) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")[:180]

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload or {}) if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_user_id(value: Any) -> Optional[int]:
        try:
            raw = str(value or "").strip()
            return int(raw) if raw else None
        except Exception:
            return None

    @staticmethod
    def _format_datetime_from_timestamp(value: float) -> str:
        return datetime.utcfromtimestamp(value).isoformat()

    def _workspace_root(self, review_id: str) -> Path:
        return self.reviews_root / self.safe_id(review_id)

    def _manifest_path(self, root: Path) -> Path:
        return root / "manifest.json"

    def _load_manifest(self, root: Path) -> Dict[str, Any]:
        return self._read_json(self._manifest_path(root))

    def _can_access(self, manifest: Dict[str, Any], *, user_id: Optional[int]) -> bool:
        owner_id = self._parse_user_id(manifest.get("user_id"))
        if owner_id is None or user_id is None:
            return True
        return owner_id == int(user_id)

    def _file_payload(self, path: Path, *, root: Path) -> Dict[str, Any]:
        resolved = path.resolve()
        root_resolved = root.resolve()
        relative_path = resolved.relative_to(root_resolved).as_posix()
        parts = relative_path.split("/")
        group = parts[0] if parts and len(parts) > 1 else "root"
        suffix = resolved.suffix.lower()
        stat = resolved.stat()
        return {
            "name": resolved.name,
            "relative_path": relative_path,
            "group": group if group in {"root", "pdf", "md", "review", "searches"} else "other",
            "suffix": suffix,
            "size": stat.st_size,
            "modified_at": self._format_datetime_from_timestamp(stat.st_mtime),
            "media_type": mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            "previewable": suffix in self.PREVIEW_SUFFIXES,
            "download_path": relative_path,
        }

    def _list_files(self, root: Path, *, limit: int = 1200) -> List[Dict[str, Any]]:
        if not root.is_dir():
            return []
        files: List[Dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            files.append(self._file_payload(path, root=root))
            if len(files) >= limit:
                break

        group_order = {"root": 0, "pdf": 1, "md": 2, "review": 3, "searches": 4, "other": 5}
        return sorted(
            files,
            key=lambda item: (
                group_order.get(str(item.get("group") or "other"), 99),
                str(item.get("relative_path") or ""),
            ),
        )

    def _modified_at(self, root: Path, files: List[Dict[str, Any]], manifest: Dict[str, Any]) -> str:
        candidates = [str(manifest.get("updated_at") or manifest.get("created_at") or "").strip()]
        candidates.extend(str(item.get("modified_at") or "") for item in files)
        candidates = [item for item in candidates if item]
        if candidates:
            return max(candidates)
        return self._format_datetime_from_timestamp(root.stat().st_mtime)

    @staticmethod
    def _workspace_status(files: List[Dict[str, Any]]) -> str:
        paths = {str(item.get("relative_path") or "") for item in files}
        if "review/final.md" in paths:
            return "final_ready"
        if any(path.startswith("review/") and path.endswith(".md") for path in paths):
            return "reviewing"
        if any(path.startswith("md/") and path.endswith(".md") for path in paths):
            return "reading"
        if any(path.startswith("pdf/") and path.endswith(".pdf") for path in paths):
            return "downloaded"
        return "created"

    @staticmethod
    def _counts(files: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            "pdf": sum(1 for item in files if str(item.get("relative_path") or "").startswith("pdf/") and item.get("suffix") == ".pdf"),
            "md": sum(1 for item in files if str(item.get("relative_path") or "").startswith("md/") and item.get("suffix") == ".md"),
            "json": sum(1 for item in files if item.get("suffix") == ".json"),
            "review": sum(1 for item in files if str(item.get("relative_path") or "").startswith("review/") and item.get("suffix") == ".md"),
        }

    def _workspace_payload(self, root: Path, *, include_manifest: bool = False) -> Dict[str, Any]:
        manifest = self._load_manifest(root)
        files = self._list_files(root)
        counts = self._counts(files)
        payload = {
            "literature_review_id": str(manifest.get("literature_review_id") or root.name),
            "topic": str(manifest.get("topic") or ""),
            "notes": str(manifest.get("notes") or ""),
            "target_paper_count": int(manifest.get("target_paper_count") or 0),
            "user_id": self._parse_user_id(manifest.get("user_id")),
            "created_at": str(manifest.get("created_at") or ""),
            "updated_at": str(manifest.get("updated_at") or ""),
            "modified_at": self._modified_at(root, files, manifest),
            "root_path": str(root),
            "status": self._workspace_status(files),
            "paper_count": len(dict(manifest.get("papers") or {})) if isinstance(manifest.get("papers"), dict) else 0,
            "counts": counts,
            "files": files,
            "has_final": any(str(item.get("relative_path") or "") == "review/final.md" for item in files),
        }
        if include_manifest:
            payload["manifest"] = manifest
        return payload

    def list_workspaces(self, *, user_id: Optional[int]) -> Dict[str, Any]:
        if not self.reviews_root.is_dir():
            return {"reviews_root": str(self.reviews_root), "workspaces": []}

        workspaces: List[Dict[str, Any]] = []
        for root in sorted(self.reviews_root.iterdir()):
            if not root.is_dir():
                continue
            manifest = self._load_manifest(root)
            if not self._can_access(manifest, user_id=user_id):
                continue
            workspaces.append(self._workspace_payload(root, include_manifest=False))

        workspaces.sort(key=lambda item: str(item.get("modified_at") or ""), reverse=True)
        return {"reviews_root": str(self.reviews_root), "workspaces": workspaces}

    def get_workspace(self, review_id: str, *, user_id: Optional[int]) -> Optional[Dict[str, Any]]:
        root = self._workspace_root(review_id)
        if not root.is_dir():
            return None
        manifest = self._load_manifest(root)
        if not self._can_access(manifest, user_id=user_id):
            return None
        return self._workspace_payload(root, include_manifest=True)

    def resolve_file(self, review_id: str, relative_path: str, *, user_id: Optional[int]) -> Optional[Path]:
        workspace = self.get_workspace(review_id, user_id=user_id)
        if workspace is None:
            return None
        raw = str(relative_path or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or "\x00" in raw:
            return None
        if any(part in {"", ".", ".."} for part in raw.split("/")):
            return None
        root = self._workspace_root(review_id).resolve()
        try:
            candidate = (root / raw).resolve()
            candidate.relative_to(root)
        except Exception:
            return None
        return candidate if candidate.is_file() else None

    def read_preview_file(
        self,
        review_id: str,
        relative_path: str,
        *,
        user_id: Optional[int],
        max_chars: int = 1_200_000,
    ) -> Optional[Dict[str, Any]]:
        path = self.resolve_file(review_id, relative_path, user_id=user_id)
        if path is None or path.suffix.lower() not in self.PREVIEW_SUFFIXES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        if path.suffix.lower() == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except Exception:
                pass
        stat = path.stat()
        return {
            "name": path.name,
            "relative_path": str(relative_path).replace("\\", "/"),
            "suffix": path.suffix.lower(),
            "size": stat.st_size,
            "modified_at": self._format_datetime_from_timestamp(stat.st_mtime),
            "media_type": mimetypes.guess_type(path.name)[0] or "text/plain",
            "content": text,
            "truncated": truncated,
        }

    def resolve_download_file(
        self,
        review_id: str,
        relative_path: str,
        *,
        user_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        path = self.resolve_file(review_id, relative_path, user_id=user_id)
        if path is None:
            return None
        return {
            "path": path,
            "filename": path.name,
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
