"""
Notebook workspace helpers for CodeLab file uploads.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import UploadFile

from app.config import settings


_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_FILE_COUNT = 100
_CHUNK_SIZE = 1024 * 1024


def _upload_root() -> str:
    base = os.path.abspath(os.getenv("UPLOAD_DIR", "./uploads"))
    return os.path.join(base, "codelab", "notebooks")


def get_notebook_workspace_dir(notebook_id: str, user_id: int) -> str:
    return os.path.join(_upload_root(), str(int(user_id)), str(notebook_id))


def get_notebook_workspace_display_path(notebook_id: str, user_id: int) -> str:
    return f"uploads/codelab/notebooks/{int(user_id)}/{notebook_id}"


def ensure_notebook_workspace(notebook_id: str, user_id: int) -> str:
    workspace_dir = get_notebook_workspace_dir(notebook_id, user_id)
    os.makedirs(workspace_dir, exist_ok=True)
    return workspace_dir


def _max_file_bytes() -> int:
    return max(int(getattr(settings, "codelab_workspace_file_max_bytes", _DEFAULT_MAX_FILE_BYTES)), 1024)


def _max_file_count() -> int:
    return max(int(getattr(settings, "codelab_workspace_file_max_count", _DEFAULT_MAX_FILE_COUNT)), 1)


def _sanitize_filename(filename: str) -> str:
    raw = os.path.basename(str(filename or "").strip())
    sanitized = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", raw)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if not sanitized:
        raise ValueError("文件名无效")
    return sanitized[:180]


def _dedupe_path(path: str) -> str:
    if not os.path.exists(path):
        return path

    stem, ext = os.path.splitext(path)
    suffix = 2
    while True:
        candidate = f"{stem}-{suffix}{ext}"
        if not os.path.exists(candidate):
            return candidate
        suffix += 1


def _build_file_entry(path: str, workspace_dir: str, *, content_type: str | None = None) -> Dict[str, Any]:
    stat = os.stat(path)
    name = os.path.basename(path)
    guessed_type, _ = mimetypes.guess_type(name)
    mime = content_type or guessed_type or "application/octet-stream"
    return {
        "name": name,
        "relative_path": os.path.relpath(path, workspace_dir).replace("\\", "/"),
        "runtime_path": path,
        "size_bytes": int(stat.st_size),
        "content_type": mime,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "extension": os.path.splitext(name)[1].lower(),
    }


def list_notebook_workspace_files(notebook_id: str, user_id: int) -> List[Dict[str, Any]]:
    workspace_dir = ensure_notebook_workspace(notebook_id, user_id)
    files: List[Dict[str, Any]] = []
    for item in os.listdir(workspace_dir):
        if item.startswith("."):
            continue
        abs_path = os.path.join(workspace_dir, item)
        if os.path.isfile(abs_path):
            files.append(_build_file_entry(abs_path, workspace_dir))
    files.sort(key=lambda item: (item.get("updated_at") or "", item.get("name") or ""), reverse=True)
    return files


def build_notebook_workspace_context(notebook_id: str, user_id: int) -> Dict[str, Any]:
    workspace_dir = ensure_notebook_workspace(notebook_id, user_id)
    files = list_notebook_workspace_files(notebook_id, user_id)
    file_paths = {str(item["name"]): str(item["runtime_path"]) for item in files}
    return {
        "directory": workspace_dir,
        "display_path": get_notebook_workspace_display_path(notebook_id, user_id),
        "file_count": len(files),
        "files": files,
        "file_names": list(file_paths.keys()),
        "file_paths": file_paths,
    }


async def save_notebook_workspace_upload(notebook_id: str, user_id: int, upload: UploadFile) -> Dict[str, Any]:
    if upload is None or not str(getattr(upload, "filename", "") or "").strip():
        raise ValueError("请选择要上传的文件")

    workspace_dir = ensure_notebook_workspace(notebook_id, user_id)
    existing_files = list_notebook_workspace_files(notebook_id, user_id)
    if len(existing_files) >= _max_file_count():
        raise ValueError(f"当前 Notebook 最多允许 {_max_file_count()} 个文件")

    filename = _sanitize_filename(upload.filename or "")
    target_path = _dedupe_path(os.path.join(workspace_dir, filename))

    written = 0
    try:
        with open(target_path, "wb") as handle:
            while True:
                chunk = await upload.read(_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > _max_file_bytes():
                    raise ValueError(f"单个文件不能超过 {_max_file_bytes() // (1024 * 1024)} MB")
                handle.write(chunk)
    except Exception:
        if os.path.exists(target_path):
            os.remove(target_path)
        raise
    finally:
        await upload.close()

    return _build_file_entry(target_path, workspace_dir, content_type=upload.content_type)


def delete_notebook_workspace_file(notebook_id: str, user_id: int, file_name: str) -> bool:
    workspace_dir = ensure_notebook_workspace(notebook_id, user_id)
    sanitized = _sanitize_filename(file_name)
    target_path = os.path.join(workspace_dir, sanitized)
    if not os.path.isfile(target_path):
        return False
    os.remove(target_path)
    return True


def delete_notebook_workspace(notebook_id: str, user_id: int) -> None:
    workspace_dir = get_notebook_workspace_dir(notebook_id, user_id)
    if os.path.isdir(workspace_dir):
        shutil.rmtree(workspace_dir, ignore_errors=True)
