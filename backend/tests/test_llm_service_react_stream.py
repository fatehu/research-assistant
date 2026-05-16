import os
import sys
import types
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_react_chat_stream_supports_thinking_alias_tags():
    service = _build_service_with_chunks(
        [
            "<thinking>先分析问题",
            "再给结论</thinking><answer>最终答案</answer>",
        ]
    )

    events = []
    async for event in service.react_chat_stream([{"role": "user", "content": "hello"}]):
        events.append(event)

    thought_events = [item for item in events if item.get("type") == "thought"]
    content_events = [item for item in events if item.get("type") == "content"]

    assert thought_events
    assert thought_events[0]["data"] == "先分析问题再给结论"
    assert "".join(item["data"] for item in content_events) == "最终答案"
    assert events[-1]["data"]["answer"] == "最终答案"


@pytest.mark.asyncio
async def test_chat_with_tools_sanitizes_invalid_function_names_and_maps_back():
    captured = {}
    service = object.__new__(LLMService)
    service.provider = "deepseek"
    service.config = {"model": "test-model"}

    async def _fake_create(**kwargs):
        captured["tools"] = kwargs.get("tools") or []
        alias_name = captured["tools"][0]["function"]["name"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        reasoning_content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                type="function",
                                function=SimpleNamespace(
                                    name=alias_name,
                                    arguments='{"query":"hello"}',
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14),
            model="test-model",
        )

    service.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_create),
        )
    )

    result = await service.chat_with_tools(
        messages=[{"role": "user", "content": "search"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "mcp.firecrawl.firecrawl_scrape",
                    "description": "scrape a page",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
        system_prompt="system",
    )

    assert captured["tools"][0]["function"]["name"] == "mcp_firecrawl_firecrawl_scrape"
    assert result["tool_calls"][0]["name"] == "mcp.firecrawl.firecrawl_scrape"


@pytest.mark.asyncio
async def test_chat_with_tools_stream_emits_native_content_before_done():
    captured = {}
    service = object.__new__(LLMService)
    service.provider = "deepseek"
    service.config = {"model": "test-model"}

    async def _fake_create(**kwargs):
        captured["stream"] = kwargs.get("stream")

        async def _stream():
            yield SimpleNamespace(
                model="test-model",
                choices=[SimpleNamespace(delta=SimpleNamespace(content="第一段，"), finish_reason=None)],
            )
            yield SimpleNamespace(
                model="test-model",
                choices=[SimpleNamespace(delta=SimpleNamespace(content="第二段"), finish_reason="stop")],
            )

        return _stream()

    service.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_create),
        )
    )

    events = []
    async for event in service.chat_with_tools_stream(
        messages=[{"role": "user", "content": "answer"}],
        tools=[],
        system_prompt="system",
    ):
        events.append(event)

    assert captured["stream"] is True
    assert [item["type"] for item in events] == ["content", "content", "done"]
    assert "".join(item["data"] for item in events if item["type"] == "content") == "第一段，第二段"
    assert events[-1]["data"]["content"] == "第一段，第二段"
    assert events[-1]["data"]["function_calling_streaming"] is True


@pytest.mark.asyncio
async def test_chat_with_tools_stream_reconstructs_split_tool_call_and_maps_alias():
    captured = {}
    service = object.__new__(LLMService)
    service.provider = "deepseek"
    service.config = {"model": "test-model"}

    async def _fake_create(**kwargs):
        captured["tools"] = kwargs.get("tools") or []
        alias_name = captured["tools"][0]["function"]["name"]

        async def _stream():
            yield SimpleNamespace(
                model="test-model",
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-1",
                                    type="function",
                                    function=SimpleNamespace(name=alias_name, arguments='{"query"'),
                                )
                            ]
                        ),
                        finish_reason=None,
                    )
                ],
            )
            yield SimpleNamespace(
                model="test-model",
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    type=None,
                                    function=SimpleNamespace(name="", arguments=':"hello"}'),
                                )
                            ]
                        ),
                        finish_reason="tool_calls",
                    )
                ],
            )

        return _stream()

    service.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=_fake_create),
        )
    )

    events = []
    async for event in service.chat_with_tools_stream(
        messages=[{"role": "user", "content": "search"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "mcp.firecrawl.firecrawl_scrape",
                    "description": "scrape a page",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
        system_prompt="system",
    ):
        events.append(event)

    assert captured["tools"][0]["function"]["name"] == "mcp_firecrawl_firecrawl_scrape"
    assert [item["type"] for item in events] == ["tool_call_delta", "tool_call_delta", "done"]
    assert events[-1]["data"]["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "name": "mcp.firecrawl.firecrawl_scrape",
            "arguments": '{"query":"hello"}',
        }
    ]


def test_sanitize_provider_messages_strips_internal_fields_but_keeps_tool_protocol():
    sanitized = LLMService.sanitize_provider_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "thought": "这是一条内部摘要",
                "metadata": {"debug": True},
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{\"query\":\"attention\"}"},
                    }
                ],
                "thought": "准备调用工具",
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "web_search",
                "content": "tool output",
                "metadata": {"debug": True},
            },
        ]
    )

    assert sanitized == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{\"query\":\"attention\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "tool output",
            "tool_call_id": "call_1",
            "name": "web_search",
        },
    ]
