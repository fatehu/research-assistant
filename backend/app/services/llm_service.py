"""
LLM service with provider abstraction.
Supports plain chat, streaming chat, and native function-calling.
"""

import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

from loguru import logger
from openai import AsyncOpenAI

from app.config import settings


class LLMService:
    """LLM service for OpenAI-compatible providers."""

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
        self.client = AsyncOpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["base_url"],
        )

    def supports_function_calling(self) -> bool:
        """Whether to use native function calling."""
        if not bool(getattr(settings, "agent_function_calling_enabled", True)):
            return False
        if self.provider == "ollama":
            return False
        return self.provider in {"openai", "deepseek", "aliyun"}

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
        full_messages.extend(messages)
        return full_messages

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
            logger.error(f"LLM chat failed [{self.provider}]: {exc}")
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
            logger.error(f"LLM stream failed [{self.provider}]: {exc}")
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

        try:
            response = await self.client.chat.completions.create(
                model=self.config["model"],
                messages=full_messages,
                tools=tools or None,
                tool_choice=tool_choice if tools else None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            message = response.choices[0].message
            raw_tool_calls = getattr(message, "tool_calls", None) or []

            tool_calls: List[Dict[str, Any]] = []
            for call in raw_tool_calls:
                fn = getattr(call, "function", None)
                tool_calls.append(
                    {
                        "id": getattr(call, "id", ""),
                        "type": getattr(call, "type", "function"),
                        "name": getattr(fn, "name", ""),
                        "arguments": getattr(fn, "arguments", "") or "{}",
                    }
                )

            return {
                "content": message.content or "",
                "tool_calls": tool_calls,
                "usage": self._normalize_usage(response.usage),
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
            }
        except Exception as exc:
            logger.error(f"LLM function calling failed [{self.provider}]: {exc}")
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
        """Legacy ReAct stream parser for <think>/<answer>."""
        system_prompt = self.REACT_SYSTEM_PROMPT
        if available_tools:
            tools_desc = "\n".join([f"- {tool}" for tool in available_tools])
            system_prompt += f"\n\n可用工具:\n{tools_desc}"

        yield {"type": "start", "data": {"provider": self.provider, "model": self.config["model"]}}
        open_think = "<think>"
        close_think = "</think>"
        open_answer = "<answer>"
        close_answer = "</answer>"
        tag_prefixes = [open_think, close_think, open_answer, close_answer]

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
                        (idx, "think")
                        for idx in [buffer.find(open_think)]
                        if idx >= 0
                    ] + [
                        (idx, "answer")
                        for idx in [buffer.find(open_answer)]
                        if idx >= 0
                    ]
                    if not candidates:
                        keep = max(len(open_think), len(open_answer)) - 1
                        if len(buffer) > keep:
                            plain = buffer[:-keep]
                            if plain:
                                answer_parts.append(plain)
                                yield {"type": "content", "data": plain}
                            buffer = buffer[-keep:]
                        break

                    idx, tag_type = min(candidates, key=lambda item: item[0])
                    if idx > 0:
                        plain = buffer[:idx]
                        answer_parts.append(plain)
                        yield {"type": "content", "data": plain}

                    if tag_type == "think":
                        if not thinking_started:
                            thinking_started = True
                            yield {"type": "thinking_start", "data": ""}
                        buffer = buffer[idx + len(open_think):]
                        mode = "think"
                    else:
                        buffer = buffer[idx + len(open_answer):]
                        mode = "answer"
                    continue

                if mode == "think":
                    close_idx = buffer.find(close_think)
                    if close_idx >= 0:
                        thought_parts.append(buffer[:close_idx])
                        buffer = buffer[close_idx + len(close_think):]
                        mode = "outside"
                        continue

                    keep = len(close_think) - 1
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
