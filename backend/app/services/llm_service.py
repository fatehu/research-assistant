"""
LLM service with provider abstraction.
Supports plain chat, streaming chat, and native function-calling.
"""

import hashlib
import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from loguru import logger
from openai import AsyncOpenAI

from app.config import settings


class LLMService:
    """LLM service for OpenAI-compatible providers."""

    _FUNCTION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

    REACT_SYSTEM_PROMPT = (
        "你是一个专业的AI科研助手。请先在<think>中简要思考，再在<answer>中给出中文结论。"
    )

    REACT_TOOLS_SYSTEM_PROMPT = """你是一个专业的AI科研助手，可使用以下工具：

{tools_description}

请按如下格式回复：
- 需要工具时：<think>...</think><action>{{"tool":"工具名","input":{{...}}}}</action>
- 直接回答时：<think>...</think><answer>...</answer>
"""

    def __init__(self, provider: Optional[str] = None):
        self.provider = provider or settings.default_llm_provider
        self.config = settings.get_llm_config(self.provider)
        self.provider_family = str(self.config.get("provider_family") or self.provider).strip().lower()
        self.client = AsyncOpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["base_url"],
        )

    def supports_function_calling(self) -> bool:
        """Whether to use native function calling."""
        if not bool(getattr(settings, "agent_function_calling_enabled", True)):
            return False
        if self.provider_family == "ollama":
            return False
        return self.provider_family in {"openai", "deepseek", "aliyun"}

    @staticmethod
    def _normalize_usage(usage: Any) -> Dict[str, int]:
        if not usage:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }

    def _build_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
    ) -> List[Dict[str, Any]]:
        full_messages: List[Dict[str, Any]] = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(self.sanitize_provider_messages(messages))
        return full_messages

    @classmethod
    def sanitize_provider_messages(
        cls,
        messages: List[Dict[str, Any]] | Tuple[Dict[str, Any], ...] | None,
    ) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for raw in list(messages or []):
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "").strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                continue

            content = raw.get("content")
            if isinstance(content, list):
                normalized_content: Any = list(content)
            elif isinstance(content, dict):
                normalized_content = dict(content)
            else:
                normalized_content = str(content or "")

            entry: Dict[str, Any] = {
                "role": role,
                "content": normalized_content,
            }

            if role == "assistant":
                tool_calls = [dict(item) for item in list(raw.get("tool_calls") or []) if isinstance(item, dict)]
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                elif not str(normalized_content or "").strip():
                    continue
            elif role in {"system", "user"} and not str(normalized_content or "").strip():
                continue
            elif role == "tool":
                tool_call_id = str(raw.get("tool_call_id") or "").strip()
                if tool_call_id:
                    entry["tool_call_id"] = tool_call_id
                tool_name = str(raw.get("name") or "").strip()
                if tool_name:
                    entry["name"] = tool_name

            sanitized.append(entry)
        return sanitized

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        parts = [f"{type(exc).__name__}: {exc!r}"]
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            parts.append(f"cause={type(cause).__name__}: {cause!r}")
        context = getattr(exc, "__context__", None)
        if context is not None and context is not cause:
            parts.append(f"context={type(context).__name__}: {context!r}")
        return " | ".join(parts)

    @classmethod
    def _sanitize_tool_name(cls, name: str) -> str:
        raw = str(name or "").strip()
        if raw and cls._FUNCTION_NAME_PATTERN.fullmatch(raw):
            return raw
        sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_")
        return sanitized or "tool"

    @classmethod
    def _build_provider_safe_tools(
        cls,
        tools: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        alias_to_actual: Dict[str, str] = {}
        used_aliases: set[str] = set()
        safe_tools: List[Dict[str, Any]] = []

        for item in list(tools or []):
            if not isinstance(item, dict):
                safe_tools.append(item)
                continue

            function_payload = item.get("function")
            if not isinstance(function_payload, dict):
                safe_tools.append(dict(item))
                continue

            actual_name = str(function_payload.get("name") or "").strip()
            alias_name = cls._sanitize_tool_name(actual_name)

            if alias_name in used_aliases and alias_to_actual.get(alias_name) != actual_name:
                digest = hashlib.sha1(actual_name.encode("utf-8")).hexdigest()[:8]
                alias_name = f"{alias_name[:48]}_{digest}"

            used_aliases.add(alias_name)
            alias_to_actual[alias_name] = actual_name or alias_name

            copied_tool = dict(item)
            copied_function = dict(function_payload)
            copied_function["name"] = alias_name
            copied_tool["function"] = copied_function
            safe_tools.append(copied_tool)

        return safe_tools, alias_to_actual

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        if temperature is None:
            temperature = settings.llm_temperature
        if max_tokens is None:
            max_tokens = settings.llm_max_tokens

        full_messages = self._build_messages(messages, system_prompt)

        try:
            response = await self.client.chat.completions.create(
                model=self.config["model"],
                messages=full_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return {
                "content": response.choices[0].message.content or "",
                "usage": self._normalize_usage(response.usage),
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
            }
        except Exception as exc:
            logger.error(f"LLM chat failed [{self.provider}]: {self._format_exception(exc)}")
            raise

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        if temperature is None:
            temperature = settings.llm_temperature
        if max_tokens is None:
            max_tokens = settings.llm_max_tokens

        full_messages = self._build_messages(messages, system_prompt)

        try:
            stream = await self.client.chat.completions.create(
                model=self.config["model"],
                messages=full_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except Exception as exc:
            logger.error(f"LLM stream failed [{self.provider}]: {self._format_exception(exc)}")
            raise

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        if temperature is None:
            temperature = settings.llm_temperature
        if max_tokens is None:
            max_tokens = settings.llm_max_tokens

        full_messages = self._build_messages(messages, system_prompt)
        safe_tools, alias_to_actual = self._build_provider_safe_tools(tools)

        try:
            response = await self.client.chat.completions.create(
                model=self.config["model"],
                messages=full_messages,
                tools=safe_tools or None,
                tool_choice=tool_choice if safe_tools else None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            message = response.choices[0].message
            raw_tool_calls = getattr(message, "tool_calls", None) or []
            reasoning_payload = getattr(message, "reasoning_content", None)
            if not isinstance(reasoning_payload, str) or not reasoning_payload.strip():
                reasoning_payload = getattr(message, "reasoning", None)

            reasoning_text = ""
            if isinstance(reasoning_payload, str):
                reasoning_text = reasoning_payload.strip()
            elif isinstance(reasoning_payload, list):
                parts: List[str] = []
                for item in reasoning_payload:
                    if isinstance(item, str):
                        parts.append(item)
                        continue
                    if isinstance(item, dict):
                        parts.append(str(item.get("text") or item.get("content") or ""))
                        continue
                    parts.append(str(getattr(item, "text", "") or getattr(item, "content", "") or ""))
                reasoning_text = "\n".join(part.strip() for part in parts if str(part).strip())

            tool_calls: List[Dict[str, Any]] = []
            for call in raw_tool_calls:
                fn = getattr(call, "function", None)
                raw_name = getattr(fn, "name", "")
                tool_calls.append(
                    {
                        "id": getattr(call, "id", ""),
                        "type": getattr(call, "type", "function"),
                        "name": alias_to_actual.get(str(raw_name), str(raw_name)),
                        "arguments": getattr(fn, "arguments", "") or "{}",
                    }
                )

            return {
                "content": message.content or "",
                "reasoning": reasoning_text,
                "tool_calls": tool_calls,
                "usage": self._normalize_usage(response.usage),
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
            }
        except Exception as exc:
            logger.error(f"LLM function calling failed [{self.provider}]: {self._format_exception(exc)}")
            raise

    async def chat_with_tools_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tool_choice: str = "auto",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Compatibility stream wrapper for function-calling."""
        result = await self.chat_with_tools(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        if result.get("content"):
            yield {"type": "content", "data": result["content"]}
        for call in result.get("tool_calls", []):
            yield {"type": "tool_call", "data": call}
        yield {"type": "done", "data": result}

    async def react_chat_stream(
        self,
        messages: List[Dict[str, Any]],
        available_tools: Optional[List[str]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Legacy ReAct stream parser for <think>/<thinking>/<answer>."""
        system_prompt = self.REACT_SYSTEM_PROMPT
        if available_tools:
            tools_desc = "\n".join([f"- {tool}" for tool in available_tools])
            system_prompt += f"\n\n可用工具:\n{tools_desc}"

        yield {"type": "start", "data": {"provider": self.provider, "model": self.config["model"]}}
        open_think_tags = ["<think>", "<thinking>"]
        close_think_tags = ["</think>", "</thinking>"]
        open_answer = "<answer>"
        close_answer = "</answer>"
        tag_prefixes = [*open_think_tags, *close_think_tags, open_answer, close_answer]

        mode = "outside"
        buffer = ""
        thought_parts: List[str] = []
        answer_parts: List[str] = []
        thinking_started = False

        async for chunk in self.chat_stream(messages, system_prompt):
            buffer += chunk
            while True:
                if mode == "outside":
                    candidates = [
                        (idx, "think", tag)
                        for tag in open_think_tags
                        for idx in [buffer.find(tag)]
                        if idx >= 0
                    ] + [
                        (idx, "answer", open_answer)
                        for idx in [buffer.find(open_answer)]
                        if idx >= 0
                    ]
                    if not candidates:
                        keep = max(max(len(tag) for tag in open_think_tags), len(open_answer)) - 1
                        if len(buffer) > keep:
                            plain = buffer[:-keep]
                            if plain:
                                answer_parts.append(plain)
                                yield {"type": "content", "data": plain}
                            buffer = buffer[-keep:]
                        break

                    idx, tag_type, matched_tag = min(candidates, key=lambda item: item[0])
                    if idx > 0:
                        plain = buffer[:idx]
                        answer_parts.append(plain)
                        yield {"type": "content", "data": plain}

                    if tag_type == "think":
                        if not thinking_started:
                            thinking_started = True
                            yield {"type": "thinking_start", "data": ""}
                        buffer = buffer[idx + len(matched_tag):]
                        mode = "think"
                    else:
                        buffer = buffer[idx + len(matched_tag):]
                        mode = "answer"
                    continue

                if mode == "think":
                    close_candidates = [
                        (idx, tag)
                        for tag in close_think_tags
                        for idx in [buffer.find(tag)]
                        if idx >= 0
                    ]
                    if close_candidates:
                        close_idx, matched_close_tag = min(close_candidates, key=lambda item: item[0])
                        thought_parts.append(buffer[:close_idx])
                        buffer = buffer[close_idx + len(matched_close_tag):]
                        mode = "outside"
                        continue

                    keep = max(len(tag) for tag in close_think_tags) - 1
                    if len(buffer) > keep:
                        thought_parts.append(buffer[:-keep])
                        buffer = buffer[-keep:]
                    break

                close_idx = buffer.find(close_answer)
                if close_idx >= 0:
                    answer_chunk = buffer[:close_idx]
                    if answer_chunk:
                        answer_parts.append(answer_chunk)
                        yield {"type": "content", "data": answer_chunk}
                    buffer = buffer[close_idx + len(close_answer):]
                    mode = "outside"
                    continue

                keep = len(close_answer) - 1
                if len(buffer) > keep:
                    answer_chunk = buffer[:-keep]
                    if answer_chunk:
                        answer_parts.append(answer_chunk)
                        yield {"type": "content", "data": answer_chunk}
                    buffer = buffer[-keep:]
                break

        if mode == "think":
            thought_parts.append(buffer)
            buffer = ""
        elif mode == "answer":
            if buffer:
                answer_parts.append(buffer)
                yield {"type": "content", "data": buffer}
            buffer = ""

        if mode == "outside" and buffer:
            if not any(tag.startswith(buffer) for tag in tag_prefixes):
                answer_parts.append(buffer)
                yield {"type": "content", "data": buffer}

        thought = "".join(thought_parts).strip()
        answer = "".join(answer_parts).strip()

        if thought:
            if not thinking_started:
                yield {"type": "thinking_start", "data": ""}
            yield {"type": "thought", "data": thought}

        yield {"type": "done", "data": {"thought": thought, "answer": answer}}

    async def react_chat_with_tools_stream(
        self,
        messages: List[Dict[str, Any]],
        tools_description: str,
        tool_executor,
        max_iterations: int = 3,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Legacy helper kept for compatibility."""
        system_prompt = self.REACT_TOOLS_SYSTEM_PROMPT.format(tools_description=tools_description)
        yield {"type": "start", "data": {"provider": self.provider, "model": self.config["model"]}}

        response = await self.chat(messages, system_prompt=system_prompt)
        content = response.get("content", "")

        action_match = re.search(r"<action>(.*?)</action>", content, re.DOTALL)
        answer_match = re.search(r"<answer>(.*?)</answer>", content, re.DOTALL)

        if action_match:
            try:
                action_data = json.loads(action_match.group(1).strip())
                tool_name = action_data.get("tool")
                tool_input = action_data.get("input", {})
                yield {"type": "action", "data": {"tool": tool_name, "input": tool_input}}
                tool_result = await tool_executor(tool_name, **tool_input)
                yield {
                    "type": "observation",
                    "data": {
                        "tool": tool_name,
                        "success": bool(getattr(tool_result, "success", False)),
                        "output": str(getattr(tool_result, "output", "")),
                    },
                }
            except Exception as exc:
                yield {"type": "error", "data": f"工具调用失败: {exc}"}

        if answer_match:
            final_answer = answer_match.group(1).strip()
        else:
            final_answer = re.sub(r"</?(?:think|action|answer|observation)>", "", content).strip()

        if final_answer:
            yield {"type": "content", "data": final_answer}

        yield {"type": "done", "data": {"thought": "", "answer": final_answer}}


async def get_llm_service(provider: Optional[str] = None) -> LLMService:
    """Get LLM service instance."""
    return LLMService(provider)
