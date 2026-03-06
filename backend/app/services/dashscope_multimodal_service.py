from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from app.services.reader_single_agent_controller import parse_json_dict_from_model_text

try:  # pragma: no cover - import guarded for environments without the SDK
    import dashscope
    from dashscope import MultiModalConversation
except Exception:  # pragma: no cover - import guarded for tests/runtime fallback
    dashscope = None
    MultiModalConversation = None


class DashScopeMultimodalService:
    """DashScope multimodal helper that uploads local files via file:// URIs."""

    @staticmethod
    def is_available() -> bool:
        return dashscope is not None and MultiModalConversation is not None

    @staticmethod
    def normalize_api_base(base_url: str) -> str:
        token = str(base_url or "").strip()
        if not token:
            return "https://dashscope.aliyuncs.com/api/v1"
        if token.endswith("/compatible-mode/v1"):
            return token[: -len("/compatible-mode/v1")] + "/api/v1"
        return token

    @staticmethod
    def normalize_file_uri(raw_path: str) -> str:
        token = str(raw_path or "").strip()
        if not token:
            return ""
        if token.startswith("file://"):
            return token
        try:
            resolved = Path(token).expanduser().resolve()
        except Exception:
            return ""
        if not resolved.is_file():
            return ""
        return resolved.as_uri()

    @classmethod
    def collect_local_file_uris(cls, *raw_paths: str, limit: int = 3) -> List[str]:
        uris: List[str] = []
        seen: set[str] = set()
        for raw in raw_paths:
            uri = cls.normalize_file_uri(str(raw or "").strip())
            if not uri or uri in seen:
                continue
            uris.append(uri)
            seen.add(uri)
            if len(uris) >= max(1, int(limit)):
                break
        return uris

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        output = getattr(response, "output", None)
        if output is None:
            return ""
        text = str(getattr(output, "text", "") or "").strip()
        if text:
            return text
        choices = list(getattr(output, "choices", None) or [])
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    value = str(item.get("text") or "").strip()
                else:
                    value = str(getattr(item, "text", "") or "").strip()
                if value:
                    parts.append(value)
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _extract_usage(response: Any) -> Dict[str, int]:
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    @classmethod
    async def chat_json(
        cls,
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[str],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        if not cls.is_available():
            raise RuntimeError("dashscope_sdk_unavailable")
        file_uris = cls.collect_local_file_uris(*[str(item or "").strip() for item in list(image_paths or [])])
        if not file_uris:
            raise RuntimeError("dashscope_local_image_missing")

        system_message = {
            "role": "system",
            "content": [{"text": str(system_prompt or "").strip()}],
        }
        user_content: List[Dict[str, str]] = [{"image": uri} for uri in file_uris]
        user_content.append({"text": str(user_prompt or "").strip()})
        user_message = {"role": "user", "content": user_content}
        api_base = cls.normalize_api_base(base_url)

        def _call() -> Any:
            previous_base = getattr(dashscope, "base_http_api_url", "")
            dashscope.base_http_api_url = api_base
            try:
                return MultiModalConversation.call(
                    model=str(model or "").strip(),
                    api_key=str(api_key or "").strip(),
                    messages=[system_message, user_message],
                    temperature=float(temperature),
                    max_length=max(512, int(max_tokens)),
                )
            finally:
                dashscope.base_http_api_url = previous_base

        response = await asyncio.to_thread(_call)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code and status_code != 200:
            code = str(getattr(response, "code", "") or "").strip()
            message = str(getattr(response, "message", "") or "").strip()
            raise RuntimeError(f"dashscope_multimodal_failed:{status_code}:{code}:{message}")

        raw_text = cls._extract_response_text(response)
        parsed = await parse_json_dict_from_model_text(raw_text)
        usage = cls._extract_usage(response)
        logger.debug(
            "[DashScopeMultimodalService] multimodal call finished model={} prompt_tokens={} completion_tokens={} images={}",
            str(model or "").strip(),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            len(file_uris),
        )
        return {
            "parsed": parsed if isinstance(parsed, dict) else {},
            "raw_text": raw_text,
            "usage": usage,
            "model": str(model or "").strip(),
            "image_file_uris": file_uris,
        }
