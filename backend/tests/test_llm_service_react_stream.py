import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.llm_service import LLMService


def _build_service_with_chunks(chunks):
    service = object.__new__(LLMService)
    service.provider = "test"
    service.config = {"model": "test-model"}

    async def _fake_chat_stream(self, messages, system_prompt=None, temperature=None, max_tokens=None):
        for chunk in chunks:
            yield chunk

    service.chat_stream = types.MethodType(_fake_chat_stream, service)
    return service


@pytest.mark.asyncio
async def test_react_chat_stream_keeps_incremental_answer_streaming():
    service = _build_service_with_chunks(
        [
            "<think>先分析问题",
            "再给结论</think><answer>第一段，",
            "第二段</answer>",
        ]
    )

    events = []
    async for event in service.react_chat_stream([{"role": "user", "content": "hello"}]):
        events.append(event)

    content_events = [item for item in events if item.get("type") == "content"]
    done_index = next(i for i, event in enumerate(events) if event.get("type") == "done")
    first_content_index = next(i for i, event in enumerate(events) if event.get("type") == "content")

    assert first_content_index < done_index
    assert "".join(item["data"] for item in content_events) == "第一段，第二段"
    assert any(item.get("type") == "thought" for item in events)
    assert events[-1]["data"]["answer"] == "第一段，第二段"


@pytest.mark.asyncio
async def test_react_chat_stream_plain_text_without_tags_is_incremental():
    service = _build_service_with_chunks(["plain ", "stream ", "output"])

    events = []
    async for event in service.react_chat_stream([{"role": "user", "content": "hello"}]):
        events.append(event)

    content_events = [item for item in events if item.get("type") == "content"]

    assert "".join(item["data"] for item in content_events) == "plain stream output"
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["answer"] == "plain stream output"
