from __future__ import annotations

import os
import shutil
from pathlib import Path


def _resolve_upload_root() -> Path:
    configured = str(os.getenv("UPLOAD_DIR") or "").strip()
    if configured:
        return Path(os.path.abspath(configured))

    mounted_upload_root = Path("/app/uploads")
    if mounted_upload_root.exists():
        return mounted_upload_root.resolve()

    return Path(os.path.abspath("./uploads"))


def _maybe_migrate_legacy_project_dir(project_dir: Path) -> None:
    configured = str(os.getenv("UPLOAD_DIR") or "").strip()
    if configured:
        return

    legacy_dir = Path("/tmp/uploads") / "projects" / str(project_dir.name)
    if not legacy_dir.exists() or legacy_dir.resolve() == project_dir.resolve():
        return

    if project_dir.exists():
        try:
            if any(project_dir.iterdir()):
                return
        except OSError:
            return
        shutil.rmtree(project_dir, ignore_errors=True)

    project_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_dir), str(project_dir))


def get_project_root_dir(project_id: int, *, ensure_exists: bool = True) -> Path:
    upload_root = _resolve_upload_root()
    project_dir = upload_root / "projects" / str(int(project_id))
    _maybe_migrate_legacy_project_dir(project_dir)
    if ensure_exists:
        project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir
