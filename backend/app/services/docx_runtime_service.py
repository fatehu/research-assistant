from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import httpx

from app.config import settings


class DocxRuntimeWorkerClient:
    """Runtime-worker client for docx generation workspaces.

    This is intentionally separate from Project runtime semantics: callers pass
    a docx workspace and docx_id, not a project_id.
    """

    def __init__(self) -> None:
        self.base_url = str(getattr(settings, "project_runtime_worker_url", "") or "").rstrip("/")
        self.token = str(getattr(settings, "project_runtime_worker_token", "") or "")

    @staticmethod
    def enabled() -> bool:
        return bool(getattr(settings, "project_runtime_worker_enabled", False))

    def _headers(self) -> Dict[str, str]:
        return {"X-Runtime-Worker-Token": self.token} if self.token else {}

    async def claude(
        self,
        *,
        docx_id: str,
        workspace_dir: Path,
        prompt: str,
        continue_session: bool,
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("docx runtime worker url is empty")
        payload = {
            "docx_id": str(docx_id or ""),
            "workspace_dir": str(Path(workspace_dir)),
            "prompt": str(prompt or ""),
            "continue_session": bool(continue_session),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=3.0)) as client:
            response = await client.post(
                f"{self.base_url}/docx/claude/run",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            return dict(response.json() or {})

    async def claude_stream(
        self,
        *,
        docx_id: str,
        workspace_dir: Path,
        prompt: str,
        continue_session: bool,
    ):
        if not self.base_url:
            raise RuntimeError("docx runtime worker url is empty")
        payload = {
            "docx_id": str(docx_id or ""),
            "workspace_dir": str(Path(workspace_dir)),
            "prompt": str(prompt or ""),
            "continue_session": bool(continue_session),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=3.0)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/docx/claude/run_stream",
                json=payload,
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        yield payload
