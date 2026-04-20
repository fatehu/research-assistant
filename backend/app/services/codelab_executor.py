"""
CodeLab executor client.

By default this client dispatches execution to the dedicated sandbox runner service.
For local development/tests, it can fallback to the in-process local executor by setting
CODELAB_RUNNER_ENABLED=false.
"""

from __future__ import annotations

import time
import traceback
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.config import settings
from app.sandbox_runner.local_executor import CodeLabExecutor as LocalCodeLabExecutor


class RunnerUnavailableError(RuntimeError):
    pass


class CodeLabExecutor:
    def __init__(self, notebook_id: str, hard_timeout_seconds: int) -> None:
        self.notebook_id = notebook_id
        self.hard_timeout_seconds = max(0, int(hard_timeout_seconds))
        self._runner_enabled = bool(getattr(settings, "codelab_runner_enabled", True))
        self._runner_url = str(getattr(settings, "codelab_runner_url", "http://codelab-runner:8099")).rstrip("/")
        self._runner_token = str(getattr(settings, "codelab_runner_token", "") or "").strip()
        self._runner_timeout_seconds = max(1, int(getattr(settings, "codelab_runner_timeout_seconds", 25)))
        self._runner_connect_timeout_seconds = max(
            1,
            int(getattr(settings, "codelab_runner_connect_timeout_seconds", 3)),
        )

        self._local_executor: Optional[LocalCodeLabExecutor] = None
        if not self._runner_enabled:
            self._local_executor = LocalCodeLabExecutor(
                notebook_id=notebook_id,
                hard_timeout_seconds=self.hard_timeout_seconds,
            )

        self._last_variables: Dict[str, str] = {}
        self._last_variable_previews: Dict[str, str] = {}
        self._last_execution_count: int = 0
        self._last_workspace_context: Optional[Dict[str, Any]] = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._runner_token}"}

    def _build_http_timeout(self, request_timeout_seconds: Optional[float] = None) -> httpx.Timeout:
        if request_timeout_seconds is None:
            effective_timeout: Optional[float] = float(self._runner_timeout_seconds)
        elif float(request_timeout_seconds) <= 0:
            # Background notebook jobs use timeout_seconds=0 to mean "let the
            # runner execute until completion/cancel". Do not let the transport
            # read timeout masquerade as a notebook execution timeout.
            effective_timeout = None
        else:
            effective_timeout = max(1.0, float(request_timeout_seconds))

        return httpx.Timeout(
            effective_timeout,
            connect=self._runner_connect_timeout_seconds,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        request_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self._runner_token:
            raise RunnerUnavailableError("CODELAB_RUNNER_TOKEN is missing")

        url = f"{self._runner_url}{path}"
        timeout = self._build_http_timeout(request_timeout_seconds=request_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(method=method, url=url, headers=self._headers(), json=json_payload)
        except httpx.HTTPError as exc:
            raise RunnerUnavailableError(f"runner_unreachable: {exc}") from exc

        if response.status_code == 503:
            raise RunnerUnavailableError("sandbox runner unavailable")

        if response.status_code >= 400:
            raise RuntimeError(f"runner_error status={response.status_code} body={response.text[:300]}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("runner_invalid_payload")
        return payload

    def close(self) -> None:
        if self._local_executor is not None:
            self._local_executor.close()
            return

        try:
            self._request(
                "POST",
                "/internal/codelab/close",
                json_payload={
                    "notebook_id": self.notebook_id,
                    "hard_timeout_seconds": self.hard_timeout_seconds,
                },
            )
        except RunnerUnavailableError:
            logger.warning(f"[CodeLabExecutor] close skipped: runner unavailable notebook_id={self.notebook_id}")
        except Exception:
            logger.debug("[CodeLabExecutor] close异常", exc_info=True)

    def interrupt(self) -> None:
        if self._local_executor is not None:
            self._local_executor.interrupt()
            return

        self._request(
            "POST",
            "/internal/codelab/interrupt",
            json_payload={
                "notebook_id": self.notebook_id,
                "hard_timeout_seconds": max(0, int(self.hard_timeout_seconds)),
            },
        )

    def reset(self, workspace_context: Optional[Dict[str, Any]] = None) -> None:
        if workspace_context is not None:
            self._last_workspace_context = dict(workspace_context)
        effective_workspace = workspace_context if workspace_context is not None else self._last_workspace_context
        if self._local_executor is not None:
            self._local_executor.reset(workspace_context=effective_workspace)
            self._last_variables = dict(self._local_executor.get_variables(workspace_context=effective_workspace) or {})
            return

        payload = self._request(
            "POST",
            "/internal/codelab/reset",
            json_payload={
                "notebook_id": self.notebook_id,
                "hard_timeout_seconds": self.hard_timeout_seconds,
                "workspace": effective_workspace,
            },
        )
        self._last_variables = dict(payload.get("variables", {}) or {})

    def get_variables(self, workspace_context: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        if workspace_context is not None:
            self._last_workspace_context = dict(workspace_context)
        effective_workspace = workspace_context if workspace_context is not None else self._last_workspace_context
        if self._local_executor is not None:
            self._last_variables = dict(self._local_executor.get_variables(workspace_context=effective_workspace) or {})
            return dict(self._last_variables)

        payload = self._request(
            "GET",
            f"/internal/codelab/variables?notebook_id={self.notebook_id}&hard_timeout_seconds={self.hard_timeout_seconds}",
        )
        self._last_variables = dict(payload.get("variables", {}) or {})
        return dict(self._last_variables)

    def get_variable_preview(self, name: str) -> Optional[str]:
        if not name:
            return None
        if self._local_executor is not None:
            return self._local_executor.get_variable_preview(name)
        if name not in self._last_variable_previews:
            self.get_variables()
        return self._last_variable_previews.get(name)

    def has_variable(self, name: str) -> bool:
        if self._local_executor is not None:
            return self._local_executor.has_variable(name)
        if name in self._last_variables:
            return True
        variables = self.get_variables()
        return name in variables

    def execute(
        self,
        code: str,
        timeout_seconds: int,
        workspace_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._local_executor is not None:
            result = self._local_executor.execute(
                code=code,
                timeout_seconds=timeout_seconds,
                workspace_context=workspace_context,
            )
            self._last_variables = dict(result.get("variables", {}) or {})
            self._last_variable_previews = dict(result.get("variable_previews", {}) or {})
            self._last_execution_count = int(result.get("execution_count", self._last_execution_count) or 0)
            return result

        no_timeout = int(timeout_seconds or 0) <= 0
        if no_timeout:
            timeout_value = 0
            hard_timeout_value = 0
        elif self.hard_timeout_seconds > 0:
            timeout_value = max(1, min(int(timeout_seconds or 1), self.hard_timeout_seconds))
            hard_timeout_value = max(0, int(self.hard_timeout_seconds))
        else:
            timeout_value = max(1, int(timeout_seconds or 1))
            hard_timeout_value = 0
        if workspace_context is not None:
            self._last_workspace_context = dict(workspace_context)
        effective_workspace = workspace_context if workspace_context is not None else self._last_workspace_context
        started = time.time()
        request_timeout_seconds: Optional[float]
        if no_timeout:
            request_timeout_seconds = 0
        else:
            request_timeout_seconds = max(
                float(self._runner_timeout_seconds),
                float(timeout_value) + 5.0,
            )

        payload = self._request(
            "POST",
            "/internal/codelab/execute",
            json_payload={
                "notebook_id": self.notebook_id,
                "code": code,
                "timeout_seconds": timeout_value,
                "hard_timeout_seconds": hard_timeout_value,
                "workspace": effective_workspace,
            },
            request_timeout_seconds=request_timeout_seconds,
        )
        self._last_variables = dict(payload.get("variables", {}) or {})
        self._last_variable_previews = dict(payload.get("variable_previews", {}) or {})
        self._last_execution_count = int(payload.get("execution_count", self._last_execution_count) or 0)
        if "terminated_reason" not in payload:
            payload["terminated_reason"] = "none"
        if "policy_violation_code" not in payload:
            payload["policy_violation_code"] = None
        if "execution_time_ms" not in payload:
            payload["execution_time_ms"] = int((time.time() - started) * 1000)
        return payload

    def __del__(self):
        try:
            self.close()
        except Exception:
            logger.debug(
                f"[CodeLabExecutor] 析构 close 失败 notebook_id={getattr(self, 'notebook_id', 'unknown')}\n"
                f"{traceback.format_exc()}"
            )
