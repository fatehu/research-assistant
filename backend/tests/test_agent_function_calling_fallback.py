import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.services.react_agent as react_agent_module
from app.config import settings
from app.services.agent_tools import ToolResult
from app.services.react_agent import AgentContext, AgentRuntimeContext, AgentStep, ExecutedToolCall, ParsedToolCall, ReActAgent


class _FallbackLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.chat_calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        raise RuntimeError("provider function-calling failed")

    async def chat(self, *args, **kwargs):
        self.chat_calls += 1
        if self.chat_calls == 1:
            return {
                "content": '<think>先算一下</think><action>{"tool":"calculator","input":{"expression":"2+2"}}</action>'
            }
        return {"content": "<think>完成</think><answer>结果是 4</answer>"}


class _FallbackTools:
    def get_tools_description(self, **kwargs):
        return "- calculator: 计算器"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "calculator"
        return ToolResult(success=True, output="4", data={"result": 4})


class _DirectAnswerFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        return {
            "content": "这是一个无需调用工具的直接回答。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _StreamingDirectAnswerFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True

    async def chat_with_tools_stream(self, *args, **kwargs):
        yield {"type": "content", "data": "第一段，"}
        yield {"type": "content", "data": "第二段"}
        yield {
            "type": "done",
            "data": {
                "content": "第一段，第二段",
                "reasoning": "",
                "tool_calls": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                "function_calling_streaming": True,
            },
        }

    async def chat_with_tools(self, *args, **kwargs):
        raise AssertionError("streaming path should not call non-stream chat_with_tools")

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _StreamingDraftThenToolFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools_stream(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "content", "data": "让我先查看脚本内容。"}
            yield {
                "type": "done",
                "data": {
                    "content": "让我先查看脚本内容。",
                    "reasoning": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "name": "datetime",
                            "arguments": "{\"query\":\"2014 到现在多少年\"}",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    "function_calling_streaming": True,
                },
            }
            return
        yield {"type": "content", "data": "结果是 12 年。"}
        yield {
            "type": "done",
            "data": {
                "content": "结果是 12 年。",
                "reasoning": "",
                "tool_calls": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                "function_calling_streaming": True,
            },
        }

    async def chat_with_tools(self, *args, **kwargs):
        raise AssertionError("streaming path should not call non-stream chat_with_tools")

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _ThinkingAliasDirectAnswerFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        return {
            "content": "<thinking>先判断问题无需工具</thinking>这是直接答案。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _CaptureToolChoiceFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.captured_tool_choice = None

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.captured_tool_choice = kwargs.get("tool_choice")
        return {
            "content": "",
            "reasoning": "先检索知识库",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "name": "knowledge_search",
                    "arguments": "{\"query\":\"agentic search\"}",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _AnswerDraftToolCallFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "reasoning": (
                    "让我先给出关键里程碑：\n"
                    "## 核心节点\n"
                    "- 2014年：注意力机制进入机器翻译\n"
                    "- 2017年：Transformer 发布\n"
                    "- 2018年：BERT 推动预训练范式\n"
                    "具体来说，这些节点说明了为什么后续大模型能力会快速增强。"
                ),
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "datetime",
                        "arguments": "{\"query\":\"2014 到现在多少年\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "关键里程碑如下：2014、2017、2018。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _CaptureMultiTurnToolChoiceFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.captured_tool_choices = []
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.calls += 1
        self.captured_tool_choices.append(kwargs.get("tool_choice"))
        if self.calls == 1:
            return {
                "content": "",
                "reasoning": "先检索知识库",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "knowledge_search",
                        "arguments": "{\"query\":\"agentic search\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "Agentic search 会在检索过程中自主规划下一步，并结合工具与反馈迭代决策 [来源1]。",
            "reasoning": "已有足够证据，直接回答",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _RedundantKnowledgeSearchFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "reasoning": "先做一次知识库检索",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "knowledge_search",
                        "arguments": "{\"query\":\"agentic search 智能体搜索\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        if self.calls == 2:
            return {
                "content": "",
                "reasoning": "再换个说法搜一次",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "name": "knowledge_search",
                        "arguments": "{\"query\":\"agentic search 定义 特点 与传统RAG区别\"}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "Agentic search 会在检索过程中自主规划下一步，并结合反馈调整策略 [来源1]。",
            "reasoning": "已有足够证据，直接给出答案",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


@pytest.mark.asyncio
async def test_tool_result_ledger_summary_surfaces_structured_grounding_errors():
    executed = ExecutedToolCall(
        action_event={},
        observation_event={},
        tool_message={},
        tool_name="paper_research_write_grounding_report",
        observation_output="grounding_report JSON 未通过归档校验",
        result_data={
            "relative_path": "specs/grounding_report.json",
            "structured_validation_errors": [
                {
                    "path": "dataset.sources",
                    "code": "missing_probe_or_local_evidence",
                    "message": "sogou_news 写成 grounded，但没有本轮 probe 证据。",
                    "evidence_needed": "successful probe for dataset source url or local_presence.available=true",
                    "suggested_tool_calls": [
                        {
                            "tool": "paper_research_probe_url",
                            "args": {
                                "project_id": 6,
                                "url": "https://example.com/sogou",
                                "resolve_download_gate": True,
                            },
                        }
                    ],
                }
            ],
        },
        tool_call_id="call-1",
        arguments={},
        success=False,
        error="grounding_report_invalid",
        permission_required=False,
        execution_time_ms=12.0,
        output_tokens_estimate=0,
        truncated=False,
        metadata={},
    )

    summary = await ReActAgent._tool_result_ledger_summary_text(executed)

    assert "paper_research_write_grounding_report" in summary
    assert "dataset.sources" in summary
    assert "missing_probe_or_local_evidence" in summary
    assert "paper_research_probe_url" in summary


@pytest.mark.asyncio
async def test_budget_compression_times_out_instead_of_hanging(monkeypatch):
    class _HangingCompressionLLM:
        def __init__(self, provider):
            self.provider = provider
            self.config = {}

        async def chat(self, *args, **kwargs):
            await asyncio.sleep(0.2)
            return {"content": "never"}

    monkeypatch.setattr(react_agent_module, "LLMService", _HangingCompressionLLM)
    monkeypatch.setattr(settings, "agent_budget_compression_timeout_seconds", 0.01)

    compressed = await ReActAgent._compress_text_with_qwen_turbo(
        "需要被压缩的长文本" * 100,
        target_token_budget=120,
        source="test.timeout",
        compression_kind="测试压缩",
    )

    assert compressed
    assert "需要被压缩的长文本" in compressed


class _RepeatedPaperRepoReadFCLLM:
    provider = "test"
    config = {"model": "test-model"}

    def __init__(self):
        self.calls = 0

    def supports_function_calling(self):
        return True

    async def chat_with_tools(self, *args, **kwargs):
        self.calls += 1
        if self.calls in {1, 2}:
            return {
                "content": "",
                "reasoning": "继续确认 classification-results.sh 的循环范围",
                "tool_calls": [
                    {
                        "id": f"call_{self.calls}",
                        "type": "function",
                        "name": "paper_research_read_repo_file",
                        "arguments": "{\"repo_relative_path\":\"scripts/classification-results.sh\",\"line_start\":80,\"line_end\":96}",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        return {
            "content": "已确认脚本会遍历 8 个数据集；当前应直接报告 blocker，而不是继续读取同一脚本。",
            "reasoning": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def chat(self, *args, **kwargs):
        return {"content": "<answer>fallback</answer>"}


class _NoopTools:
    def get_tools_description(self, **kwargs):
        return "- datetime: 时间"

    def list_tools(self, **kwargs):
        return []

    async def execute(self, tool_name: str, **kwargs):
        raise AssertionError("no tool call expected")


class _ForbiddenRunDraftTools:
    async def execute(self, tool_name: str, **kwargs):
        raise AssertionError(f"tool should have been blocked before execution: {tool_name}")


class _GroundingRefreshTools:
    async def execute(self, tool_name: str, **kwargs):
        assert tool_name in {
            "paper_research_read_grounding_report",
            "paper_research_write_grounding_report",
        }
        return ToolResult(success=True, output="ok", data={"tool_name": tool_name, "kwargs": kwargs})


class _PassThroughPaperTools:
    async def execute(self, tool_name: str, **kwargs):
        assert tool_name.startswith("paper_research_")
        return ToolResult(success=True, output="ok", data={"tool_name": tool_name, "kwargs": kwargs})


class _PassThroughWebTools:
    async def execute(self, tool_name: str, **kwargs):
        assert tool_name in {"web_search", "web_scrape"}
        return ToolResult(success=True, output="ok", data={"tool_name": tool_name, "kwargs": kwargs})


class _KnowledgeIntentTools:
    def classify_intent(self, user_text: str) -> str:
        return "knowledge_query"

    def select_tool_names_for_intent(self, intent: str, user_text: str = ""):
        return ["knowledge_search", "datetime", "calculator"]

    def get_tools_description(self, **kwargs):
        return "- knowledge_search: 搜索知识库"

    def list_tools(self, **kwargs):
        names = set(kwargs.get("include_tool_names") or {"knowledge_search"})
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
            for name in sorted(names)
        ]

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "knowledge_search"
        return ToolResult(success=True, output="[来源1] 检索命中", data={"results": [{"content": "agentic search"}]})


class _DateTimeOnlyTools:
    def get_tools_description(self, **kwargs):
        return "- datetime: 时间计算"

    def list_tools(self, **kwargs):
        return [
            {
                "type": "function",
                "function": {
                    "name": "datetime",
                    "description": "datetime",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ]

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "datetime"
        return ToolResult(success=True, output="2014 距今约 12 年。")


class _RepeatedFailureLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": '<think>继续修复</think><action>{"tool":"notebook_execute","input":{"code":"print(1)"}}</action>',
            "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
        }


class _RepeatedFailureTools:
    def get_tools_description(self, **kwargs):
        return "- notebook_execute: 执行 notebook cell"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "notebook_execute"
        return ToolResult(success=False, output="PolicyViolationError: 不要导入 os", error="policy_violation")


class _RepeatedExecutionSpecFailureLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": (
                '<think>继续尝试写 execution_spec</think>'
                '<action>{"tool":"paper_research_write_execution_spec","input":{"project_id":135,"execution_spec":{"execution_id":"agnews-tuning","execution_intent":{"runtime_type":"plain-python","entrypoint_type":"generated_python","generated_program_name":"train_variant.py"}}}}</action>'
            ),
            "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
        }


class _RepeatedExecutionSpecFailureTools:
    def get_tools_description(self, **kwargs):
        return "- paper_research_write_execution_spec: 写入 execution 计划"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "paper_research_write_execution_spec"
        return ToolResult(
            success=False,
            output=(
                "execution_spec 无效，未写入: generated_python entrypoint_path 缺失；"
                "shell wrapper commands are not allowed"
            ),
            error="execution_spec_invalid",
        )


class _RepeatedRunDraftFailureLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": """<think>继续修正 run_drafts</think><action>{"tool":"paper_research_write_run_drafts","input":{"project_id":4,"run_drafts":{"drafts":[{"id":"baseline_ag_news_fixed","kind":"baseline_repro","objective":"baseline","entrypoint":{"type":"repo_script","path_or_hint":"bash -c './classification-results-ag-news-only.sh'"},"evidence_files":["repo/source/classification-results.sh"]}]}}}</action>""",
            "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
        }


class _RepeatedRunDraftFailureTools:
    def get_tools_description(self, **kwargs):
        return "- paper_research_write_run_drafts: 写入 run drafts"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "paper_research_write_run_drafts"
        return ToolResult(
            success=False,
            output=(
                "run_drafts JSON 未通过归档校验，未写入文件。\n"
                "- drafts[0].entrypoint.path_or_hint references missing repo file `classification-results-ag-news-only.sh`. "
                "Use readme_command/dataset_step/manual_step for README-only actions."
            ),
            error="run_drafts_schema_invalid",
        )


class _RepeatedRepoReadFailureLLM:
    provider = "test"
    config = {"model": "test-model"}

    def supports_function_calling(self):
        return False

    async def chat(self, *args, **kwargs):
        return {
            "content": (
                '<think>继续确认 classification-results.sh 的真实位置</think>'
                '<action>{"tool":"paper_research_read_repo_file","input":{"project_id":142,'
                '"repo_relative_path":"scripts/classification-results.sh","line_start":80,"line_end":96}}</action>'
            ),
            "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
        }


class _RepeatedRepoReadFailureTools:
    def get_tools_description(self, **kwargs):
        return "- paper_research_read_repo_file: 读取 repo 文件"

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "paper_research_read_repo_file"
        return ToolResult(
            success=False,
            output=(
                "仓库文件不存在: `repo/source/scripts/classification-results.sh`。"
                "这通常不是文件不可读，而是 repo_relative_path 猜错了。"
                "如果不确定真实路径，请先调用 `paper_research_search_repo` 用文件名或关键字定位，"
                "不要继续假设它在 `scripts/` 等目录下。"
            ),
            error="repo_file_not_found",
        )


class _SimpleExecuteTools:
    def get_tools_description(self, **kwargs):
        return "- datetime: 时间计算"

    def get(self, _name: str):
        return None

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "datetime"
        assert kwargs["query"] == "2014 到现在多少年"
        return ToolResult(
            success=True,
            output="Bahdanau 2014 引入注意力机制。",
            data={"result": "12 年"},
            execution_time_ms=12.5,
            output_tokens_estimate=18,
        )


class _PaperRepoReadOnlyTools:
    def get_tools_description(self, **kwargs):
        return "- paper_research_read_repo_file: 读取 repo 文件"

    def list_tools(self, **kwargs):
        return [
            {
                "type": "function",
                "function": {
                    "name": "paper_research_read_repo_file",
                    "description": "read repo file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo_relative_path": {"type": "string"},
                            "line_start": {"type": "integer"},
                            "line_end": {"type": "integer"},
                        },
                        "required": ["repo_relative_path"],
                    },
                },
            }
        ]

    def get(self, _name: str):
        return None

    async def execute(self, tool_name: str, **kwargs):
        assert tool_name == "paper_research_read_repo_file"
        assert kwargs["repo_relative_path"] == "scripts/classification-results.sh"
        return ToolResult(
            success=True,
            output="第 86 行是 for i in {0..7}，脚本会继续处理 8 个数据集。",
            data={"relative_path": "scripts/classification-results.sh", "line_start": 86, "line_end": 86},
        )


class _ToolLedgerRuntimeService:
    def __init__(self):
        self.entries = []
        self.item_entries = []
        self.context_states = []

    async def append_conversation_tool_ledger_entries(self, conversation_id: int, entries):
        assert conversation_id == 54
        self.entries.extend(list(entries or []))

    async def append_conversation_item_entries(self, conversation_id: int, entries):
        assert conversation_id == 54
        self.item_entries.extend(list(entries or []))

    async def upsert_conversation_context_state(self, conversation_id: int, payload):
        assert conversation_id == 54
        self.context_states.append(dict(payload or {}))



@pytest.mark.asyncio
async def test_function_calling_fallback_to_xml(monkeypatch):
    monkeypatch.setattr(settings, "agent_function_calling_fallback_xml", True)
    agent = ReActAgent(_FallbackLLM(), _FallbackTools(), max_iterations=3)

    events = []
    async for event in agent.run([{"role": "user", "content": "2+2 等于多少"}], stream=False):
        events.append(event)

    action_events = [e for e in events if e.get("type") == "action"]
    observation_events = [e for e in events if e.get("type") == "observation"]
    done_events = [e for e in events if e.get("type") == "done"]

    assert len(action_events) >= 1
    assert len(observation_events) >= 1
    assert len(done_events) == 1
    assert "4" in done_events[0]["data"]["answer"]


@pytest.mark.asyncio
async def test_function_calling_direct_answer_emits_thought_step():
    agent = ReActAgent(_DirectAnswerFCLLM(), _NoopTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "直接回答"}], stream=False):
        events.append(event)

    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert thought_events
    assert "问题分析" in str(thought_events[0].get("data", ""))
    assert done_events and "直接回答" in str(done_events[0]["data"]["answer"])


@pytest.mark.asyncio
async def test_function_calling_streams_direct_answer_before_done():
    agent = ReActAgent(_StreamingDirectAnswerFCLLM(), _NoopTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "直接回答"}], stream=True):
        events.append(event)

    content_events = [event for event in events if event.get("type") == "content"]
    done_index = next(index for index, event in enumerate(events) if event.get("type") == "done")
    assert not content_events
    assert events[done_index]["data"]["answer"] == "第一段，第二段"


@pytest.mark.asyncio
async def test_function_calling_stream_does_not_emit_draft_content_before_tool_calls():
    agent = ReActAgent(_StreamingDraftThenToolFCLLM(), _DateTimeOnlyTools(), max_iterations=3)

    events = []
    async for event in agent.run([{"role": "user", "content": "先查一下再回答"}], stream=True):
        events.append(event)

    action_index = next(index for index, event in enumerate(events) if event.get("type") == "action")
    done_index = next(index for index, event in enumerate(events) if event.get("type") == "done")
    content_chunks = [str(event.get("data") or "") for event in events if event.get("type") == "content"]
    done_event = next(event for event in events if event.get("type") == "done")

    assert action_index < done_index
    assert all("让我先查看脚本内容" not in chunk for chunk in content_chunks)
    assert "结果是 12 年" in str(done_event["data"]["answer"])


@pytest.mark.asyncio
async def test_function_calling_direct_answer_extracts_thinking_alias_into_thought():
    agent = ReActAgent(_ThinkingAliasDirectAnswerFCLLM(), _NoopTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "直接回答"}], stream=False):
        events.append(event)

    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert thought_events and thought_events[0]["data"] == "先判断问题无需工具"
    assert done_events and done_events[0]["data"]["answer"] == "这是直接答案。"


@pytest.mark.asyncio
async def test_function_calling_tool_plan_keeps_original_reasoning_text_in_process_lane():
    agent = ReActAgent(_AnswerDraftToolCallFCLLM(), _DateTimeOnlyTools(), max_iterations=2)

    events = []
    async for event in agent.run([{"role": "user", "content": "帮我梳理注意力机制时间线"}], stream=False):
        events.append(event)

    thought_events = [event for event in events if event.get("type") == "thought"]
    answer_event = next(event for event in events if event.get("type") == "answer")

    assert thought_events
    assert "让我先给出关键里程碑" in str(thought_events[0]["data"])
    assert "2014年：注意力机制进入机器翻译" in str(thought_events[0]["data"])
    assert "2014" in str(answer_event["data"])


@pytest.mark.asyncio
async def test_function_calling_uses_auto_tool_choice_with_available_tools():
    llm = _CaptureToolChoiceFCLLM()
    agent = ReActAgent(llm, _KnowledgeIntentTools(), max_iterations=1)

    events = []
    async for event in agent.run([{"role": "user", "content": "利用知识库解释 agentic search"}], stream=False):
        events.append(event)

    assert llm.captured_tool_choice == "auto"
    assert any(event.get("type") == "action" for event in events)


@pytest.mark.asyncio
async def test_function_calling_keeps_auto_tool_choice_after_first_knowledge_observation():
    llm = _CaptureMultiTurnToolChoiceFCLLM()
    agent = ReActAgent(llm, _KnowledgeIntentTools(), max_iterations=3)

    events = []
    async for event in agent.run([{"role": "user", "content": "利用知识库解释 agentic search"}], stream=False):
        events.append(event)

    done_events = [event for event in events if event.get("type") == "done"]

    assert llm.captured_tool_choices[:2] == ["auto", "auto"]
    assert done_events and "[来源1]" in str(done_events[0]["data"]["answer"])


@pytest.mark.asyncio
async def test_function_calling_blocks_redundant_knowledge_search_after_success():
    llm = _RedundantKnowledgeSearchFCLLM()
    agent = ReActAgent(llm, _KnowledgeIntentTools(), max_iterations=4)

    events = []
    async for event in agent.run([{"role": "user", "content": "利用知识库解释 agentic search"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 1
    assert any("重复知识库搜索" in str(event.get("data", "")) for event in thought_events)
    assert done_events and "[来源1]" in str(done_events[0]["data"]["answer"])


@pytest.mark.asyncio
async def test_function_calling_interrupts_repeated_repo_reads_after_success():
    agent = ReActAgent(_RepeatedPaperRepoReadFCLLM(), _PaperRepoReadOnlyTools(), max_iterations=4)

    events = []
    async for event in agent.run([{"role": "user", "content": "确认 classification-results.sh 的问题"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    thought_events = [event for event in events if event.get("type") == "thought"]
    done_event = next(event for event in events if event.get("type") == "done")

    assert len(action_events) == 2
    assert any("重复读取同一 repo 目标" in str(event.get("data", "")) for event in thought_events)
    assert "直接报告 blocker" in str(done_event["data"]["answer"])


@pytest.mark.asyncio
async def test_paper_grounding_requires_grounding_report_before_direct_answer():
    agent = ReActAgent(_DirectAnswerFCLLM(), _NoopTools(), max_iterations=1)
    agent._last_tool_selection = {"active_skill_names": ["paper-reproduction"]}

    context = AgentContext(
        messages=[{"role": "user", "content": "继续 grounding"}],
        conversation_state={"workflow_binding": {"skill": "paper-reproduction", "current_stage": "grounding"}},
        steps=[
            AgentStep(
                step_type="action",
                content="probe url",
                tool_name="paper_research_probe_url",
                tool_input={"url": "https://example.com/ag_news.csv"},
            ),
            AgentStep(
                step_type="observation",
                content="probe ok",
                tool_name="paper_research_probe_url",
                success=True,
            ),
        ],
    )

    missing = agent._missing_required_paper_skill_tool_calls(context)
    assert "paper_research_write_grounding_report" in missing

    events, done = await agent._finalize_function_calling_iteration(
        context,
        content="grounding 已完成，可以进入下一阶段。",
        reasoning="",
        parsed_calls=[],
    )

    assert done is False
    assert any("真实 paper_research 工具调用" in str(event.get("data", "")) for event in events if event.get("type") == "thought")
    assert context.messages[-1]["role"] == "user"
    assert "paper_research_write_grounding_report" in str(context.messages[-1]["content"])


@pytest.mark.asyncio
async def test_paper_read_only_grounding_summary_can_answer_without_rewriting_report():
    agent = ReActAgent(_DirectAnswerFCLLM(), _NoopTools(), max_iterations=1)
    agent._last_tool_selection = {"active_skill_names": ["paper-reproduction"]}

    context = AgentContext(
        messages=[{"role": "user", "content": "读取并总结当前 grounding_report"}],
        conversation_state={"workflow_binding": {"skill": "paper-reproduction", "current_stage": "grounding"}},
        steps=[
            AgentStep(
                step_type="action",
                content="read grounding report",
                tool_name="paper_research_read_grounding_report",
            ),
            AgentStep(
                step_type="observation",
                content="overall_status=blocked",
                tool_name="paper_research_read_grounding_report",
                success=True,
            ),
        ],
    )

    missing = agent._missing_required_paper_skill_tool_calls(context)
    assert "paper_research_write_grounding_report" not in missing


@pytest.mark.asyncio
async def test_paper_workflow_binding_still_requires_real_tool_call_for_followup_request():
    agent = ReActAgent(_DirectAnswerFCLLM(), _NoopTools(), max_iterations=1)
    agent._last_tool_selection = {"active_skill_names": ["paper-reproduction"]}

    context = AgentContext(
        messages=[{"role": "user", "content": "重新跑一次"}],
        conversation_state={
            "workflow_binding": {
                "skill": "paper-reproduction",
                "current_stage": "grounding",
                "paper_id": 113,
                "project_id": 6,
                "workspace_id": 6,
            }
        },
        steps=[],
    )

    missing = agent._missing_required_paper_skill_tool_calls(context)
    assert "any paper_research_* tool call" in missing

    events, done = await agent._finalize_function_calling_iteration(
        context,
        content="Grounding 已重新完成，可以继续。",
        reasoning="",
        parsed_calls=[],
    )

    assert done is False
    assert any(
        "真实 paper_research 工具调用" in str(event.get("data", ""))
        for event in events
        if event.get("type") == "thought"
    )


def test_grounding_blocked_without_alternative_candidates_prefers_alternative_source_search():
    row = ExecutedToolCall(
        action_event={},
        observation_event={},
        tool_message={},
        tool_name="paper_research_write_grounding_report",
        observation_output="ok",
        result_data={
            "grounding_ready": False,
            "content": {
                "dataset": {"status": "blocked", "alternative_source_candidates": []},
                "external_dependencies": {"status": "blocked", "alternative_source_candidates": []},
                "summary": {
                    "repo_grounded": True,
                    "entrypoint_grounded": True,
                    "dataset_grounded": False,
                    "runtime_grounded": False,
                    "external_dependencies_grounded": False,
                    "overall_status": "blocked",
                },
            },
        },
        tool_call_id="call-1",
        arguments={},
        success=True,
        error=None,
        permission_required=False,
        execution_time_ms=1.0,
        output_tokens_estimate=10,
        truncated=False,
    )

    message, decision = ReActAgent._tool_workflow_next_action(
        [row],
        paper_skill_active=True,
        repo_edit_allowed=True,
        workflow_binding={"current_stage": "grounding"},
    )

    assert "替代源搜索" in message
    assert decision["next_action"] == "search_alternative_sources"
    assert "web_search" in decision["allowed_actions"]


@pytest.mark.asyncio
async def test_paper_tools_are_not_hard_blocked_by_decision_state_allowed_actions():
    agent = ReActAgent(_DirectAnswerFCLLM(), _PassThroughPaperTools(), max_iterations=1)
    context = AgentContext(
        messages=[{"role": "user", "content": "继续复现"}],
        conversation_state={
            "workflow_binding": {"skill": "paper-reproduction", "current_stage": "grounding"},
            "decision_state": {
                "status": "blocked",
                "next_action": "report_blocker",
                "allowed_actions": ["report_blocker", "read_grounding_report"],
            },
        },
    )
    call = ParsedToolCall(
        name="paper_research_write_run_drafts",
        arguments={"project_id": 6, "run_drafts": {"drafts": []}},
        call_id="call_1",
        arguments_raw='{"project_id":6,"run_drafts":{"drafts":[]}}',
    )

    executed = await agent._execute_single_tool_call(context, call, parallel_group="iter-1-test")

    assert executed.success is True
    assert executed.error is None
    assert executed.result_data["tool_name"] == "paper_research_write_run_drafts"


@pytest.mark.asyncio
async def test_paper_web_search_is_not_blocked_by_decision_state_allowed_actions():
    agent = ReActAgent(_DirectAnswerFCLLM(), _PassThroughWebTools(), max_iterations=1)
    context = AgentContext(
        messages=[{"role": "user", "content": "搜索 AG News 官方数据集来源"}],
        conversation_state={
            "workflow_binding": {"skill": "paper-reproduction", "current_stage": "grounding"},
            "decision_state": {
                "status": "blocked",
                "next_action": "report_blocker",
                "allowed_actions": ["report_blocker"],
            },
        },
    )
    call = ParsedToolCall(
        name="web_search",
        arguments={"query": "AG News dataset official source", "max_results": 5},
        call_id="call_web_1",
        arguments_raw='{"query":"AG News dataset official source","max_results":5}',
    )

    executed = await agent._execute_single_tool_call(context, call, parallel_group="iter-1-test")

    assert executed.success is True
    assert executed.error is None
    assert executed.result_data["tool_name"] == "web_search"


@pytest.mark.asyncio
async def test_grounding_rerun_request_can_bypass_blocked_decision_state_for_read_grounding_report():
    agent = ReActAgent(_DirectAnswerFCLLM(), _GroundingRefreshTools(), max_iterations=1)
    context = AgentContext(
        messages=[{"role": "user", "content": "再做一次grounding"}],
        conversation_state={
            "workflow_binding": {
                "skill": "paper-reproduction",
                "current_stage": "grounding",
                "paper_id": 113,
                "project_id": 6,
                "workspace_id": 6,
            },
            "decision_state": {
                "status": "blocked",
                "next_action": "report_blocker",
                "allowed_actions": ["report_blocker"],
            },
        },
    )
    call = ParsedToolCall(
        name="paper_research_read_grounding_report",
        arguments={"project_id": 6, "mode": "full"},
        call_id="call_refresh_read",
        arguments_raw='{"project_id":6,"mode":"full"}',
    )

    executed = await agent._execute_single_tool_call(context, call, parallel_group="iter-1-test")

    assert executed.success is True
    assert executed.error is None
    assert executed.result_data["tool_name"] == "paper_research_read_grounding_report"


@pytest.mark.asyncio
async def test_grounding_rerun_request_can_bypass_blocked_decision_state_for_write_grounding_report():
    agent = ReActAgent(_DirectAnswerFCLLM(), _GroundingRefreshTools(), max_iterations=1)
    context = AgentContext(
        messages=[{"role": "user", "content": "重新跑一次 grounding"}],
        conversation_state={
            "workflow_binding": {
                "skill": "paper-reproduction",
                "current_stage": "grounding",
                "paper_id": 113,
                "project_id": 6,
                "workspace_id": 6,
            },
            "decision_state": {
                "status": "blocked",
                "next_action": "report_blocker",
                "allowed_actions": ["report_blocker"],
            },
        },
    )
    call = ParsedToolCall(
        name="paper_research_write_grounding_report",
        arguments={"project_id": 6, "grounding_report": {"summary": {}}},
        call_id="call_refresh_write",
        arguments_raw='{"project_id":6,"grounding_report":{"summary":{}}}',
    )

    executed = await agent._execute_single_tool_call(context, call, parallel_group="iter-1-test")

    assert executed.success is True
    assert executed.error is None
    assert executed.result_data["tool_name"] == "paper_research_write_grounding_report"


def test_tool_workflow_next_action_prefers_execution_after_repo_evidence():
    message, decision = ReActAgent._tool_workflow_next_action(
        [
            ExecutedToolCall(
                action_event={},
                observation_event={},
                tool_message={},
                tool_name="paper_research_read_repo_file",
                observation_output="README and script read",
                result_data={},
                tool_call_id="call_repo_1",
                arguments={"project_id": 6, "repo_relative_path": "classification-results.sh"},
                success=True,
                error=None,
                permission_required=False,
                execution_time_ms=12.0,
                output_tokens_estimate=64,
                truncated=False,
            )
        ],
        paper_skill_active=True,
        repo_edit_allowed=False,
        workflow_binding={"current_stage": "grounding"},
    )

    assert "repo-first execution" in message
    assert decision["next_action"] == "write_execution_spec"
    assert "write_execution_spec" in decision["allowed_actions"]
    assert "start_execution" in decision["allowed_actions"]
    assert "write_grounding_report" in decision["allowed_actions"]


def test_tool_workflow_next_action_does_not_force_implementation_after_grounding_report():
    message, decision = ReActAgent._tool_workflow_next_action(
        [
            ExecutedToolCall(
                action_event={},
                observation_event={},
                tool_message={},
                tool_name="paper_research_write_grounding_report",
                observation_output="saved grounding report",
                result_data={
                    "grounding_ready": True,
                    "content": {
                        "summary": {
                            "repo_grounded": True,
                            "entrypoint_grounded": True,
                            "dataset_grounded": True,
                            "runtime_grounded": True,
                            "external_dependencies_grounded": True,
                            "overall_status": "grounded",
                        }
                    },
                },
                tool_call_id="call_grounding_1",
                arguments={"project_id": 6},
                success=True,
                error=None,
                permission_required=False,
                execution_time_ms=18.0,
                output_tokens_estimate=96,
                truncated=False,
            )
        ],
        paper_skill_active=True,
        repo_edit_allowed=True,
        workflow_binding={"current_stage": "grounding"},
    )

    assert "repo-first execution" in message
    assert decision["next_action"] == "write_execution_spec"
    assert "start_execution" in decision["allowed_actions"]
    assert "write_implementation_spec" in decision["allowed_actions"]


@pytest.mark.asyncio
async def test_agent_stops_after_repeated_same_tool_failures(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_failure_streak_limit", 3, raising=False)
    agent = ReActAgent(_RepeatedFailureLLM(), _RepeatedFailureTools(), max_iterations=8)

    events = []
    async for event in agent.run([{"role": "user", "content": "继续修复这个 notebook"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 3
    assert done_events
    assert done_events[0]["data"]["iterations"] == 3
    assert "已停止自动重试" in str(done_events[0]["data"]["answer"])


def test_grounding_probe_failures_do_not_trigger_generic_failure_streak():
    agent = ReActAgent(_DirectAnswerFCLLM(), _NoopTools(), max_iterations=1)
    context = AgentContext(
        messages=[{"role": "user", "content": "继续 grounding"}],
        conversation_state={"workflow_binding": {"skill": "paper-reproduction", "current_stage": "grounding"}},
    )
    events = [
        {
            "type": "observation",
            "data": {
                "tool": "paper_research_probe_url",
                "success": False,
                "error": "url_probe_failed",
                "output": "HTTP 403",
                "data": {"status_code": 403, "diagnosis": "http_403"},
            },
        },
        {
            "type": "observation",
            "data": {
                "tool": "paper_research_probe_url",
                "success": False,
                "error": "url_probe_failed",
                "output": "HTTP 404",
                "data": {"status_code": 404, "diagnosis": "http_404"},
            },
        },
        {
            "type": "observation",
            "data": {
                "tool": "paper_research_probe_url",
                "success": False,
                "error": "url_probe_failed",
                "output": "license gate",
                "data": {"diagnosis": "license_gate"},
            },
        },
        {
            "type": "observation",
            "data": {
                "tool": "paper_research_probe_url",
                "success": False,
                "error": "url_probe_failed",
                "output": "drive confirm gate",
                "data": {"status_code": 200, "diagnosis": "gdrive_confirm_required", "page_kind": "download_gate"},
            },
        },
    ]

    message = agent._maybe_stop_after_repeated_tool_failures(context, events)

    assert message is None
    assert context.state.name == "IDLE"
    assert context.tool_failure_streaks.get("paper_research_probe_url", 0) == 0


@pytest.mark.asyncio
async def test_agent_stops_repeated_execution_spec_failures_with_script_guidance(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_failure_streak_limit", 3, raising=False)
    agent = ReActAgent(_RepeatedExecutionSpecFailureLLM(), _RepeatedExecutionSpecFailureTools(), max_iterations=8)

    events = []
    async for event in agent.run([{"role": "user", "content": "继续为 AG News 写 execution_spec"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 3
    assert any("合法路径" in str(event.get("data", "")) or "Python repo 文件" in str(event.get("data", "")) for event in thought_events)
    assert done_events
    assert "execution_intent.entrypoint_type=\"repo_script\"" in str(done_events[0]["data"]["answer"])
    assert "command=[\"./classification-results.sh\"]" in str(done_events[0]["data"]["answer"])
    assert done_events[0]["data"]["iterations"] == 3


@pytest.mark.asyncio
async def test_agent_stops_repeated_run_drafts_failures_with_schema_guidance(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_failure_streak_limit", 3, raising=False)
    agent = ReActAgent(_RepeatedRunDraftFailureLLM(), _RepeatedRunDraftFailureTools(), max_iterations=8)

    events = []
    async for event in agent.run([{"role": "user", "content": "继续修正 AG News run_drafts"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 3
    assert any("entrypoint schema" in str(event.get("data", "")) for event in thought_events)
    assert done_events
    assert "不能写 shell wrapper" in str(done_events[0]["data"]["answer"])
    assert "paper_research_write_execution_script" in str(done_events[0]["data"]["answer"])
    assert done_events[0]["data"]["iterations"] == 3


@pytest.mark.asyncio
async def test_agent_stops_repeated_repo_read_failures_with_search_guidance(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_failure_streak_limit", 3, raising=False)
    agent = ReActAgent(_RepeatedRepoReadFailureLLM(), _RepeatedRepoReadFailureTools(), max_iterations=8)

    events = []
    async for event in agent.run([{"role": "user", "content": "继续定位 classification-results.sh"}], stream=False):
        events.append(event)

    action_events = [event for event in events if event.get("type") == "action"]
    thought_events = [event for event in events if event.get("type") == "thought"]
    done_events = [event for event in events if event.get("type") == "done"]

    assert len(action_events) == 3
    assert any("先 search_repo" in str(event.get("data", "")) or "真实路径" in str(event.get("data", "")) for event in thought_events)
    assert done_events
    assert "paper_research_search_repo" in str(done_events[0]["data"]["answer"])
    assert "repo_relative_path 猜错了" in str(done_events[0]["data"]["answer"])
    assert done_events[0]["data"]["iterations"] == 3


@pytest.mark.asyncio
async def test_execute_tool_calls_persists_tool_ledger_entries():
    runtime_service = _ToolLedgerRuntimeService()
    agent = ReActAgent(
        llm_service=_DirectAnswerFCLLM(),
        tool_registry=_SimpleExecuteTools(),
        runtime_context=AgentRuntimeContext(user_id=7, channel="chat", conversation_id=54),
        runtime_service=runtime_service,
    )
    context = AgentContext(
        messages=[{"role": "user", "content": "解释注意力机制"}],
        iteration=1,
        run_id="run-1",
    )

    executed = await agent._execute_tool_calls(
        context,
        [
            ParsedToolCall(
                call_id="call_1",
                name="datetime",
                arguments={"query": "2014 到现在多少年"},
                arguments_raw='{"query":"2014 到现在多少年"}',
            )
        ],
    )

    assert len(executed) == 1
    assert len(runtime_service.entries) == 2
    assert runtime_service.entries[0]["kind"] == "tool_call"
    assert runtime_service.entries[0]["tool_name"] == "datetime"
    assert runtime_service.entries[1]["kind"] == "tool_result"
    assert runtime_service.entries[1]["status"] == "succeeded"
    assert runtime_service.entries[1]["success"] is True
    assert str(runtime_service.entries[1]["summary"]).strip()
    assert runtime_service.item_entries
    assert runtime_service.item_entries[0]["kind"] == "tool_use_summary"
    assert runtime_service.item_entries[0]["turn_id"] is None
    assert "datetime" in str(runtime_service.item_entries[0]["summary"])
    assert runtime_service.item_entries[0]["metadata"]["workflow_summary"]["decision_state"]["next_action"] == "synthesize"
    assert runtime_service.context_states[0]["decision_state"]["next_action"] == "synthesize"


@pytest.mark.asyncio
async def test_execute_tool_calls_marks_script_followup_after_execution_spec_failure():
    runtime_service = _ToolLedgerRuntimeService()

    class _FailureTools:
        def get_tools_description(self, **kwargs):
            return "- paper_research_write_execution_spec: 写入 execution 计划"

        def get(self, _name: str):
            return None

        async def execute(self, tool_name: str, **kwargs):
            assert tool_name == "paper_research_write_execution_spec"
            return ToolResult(
                success=False,
                output="execution_spec 无效，未写入: generated_python entrypoint_path 缺失",
                error="execution_spec_invalid",
            )

    agent = ReActAgent(
        llm_service=_DirectAnswerFCLLM(),
        tool_registry=_FailureTools(),
        runtime_context=AgentRuntimeContext(user_id=7, channel="chat", conversation_id=54),
        runtime_service=runtime_service,
    )
    context = AgentContext(
        messages=[{"role": "user", "content": "继续写 execution 计划"}],
        iteration=1,
        run_id="run-1",
    )

    executed = await agent._execute_tool_calls(
        context,
        [
            ParsedToolCall(
                call_id="call_exec_spec",
                name="paper_research_write_execution_spec",
                arguments={"project_id": 135, "execution_spec": {"execution_id": "agnews-tuning"}},
                arguments_raw='{"project_id":135,"execution_spec":{"execution_id":"agnews-tuning"}}',
            )
        ],
    )

    assert len(executed) == 1
    assert runtime_service.item_entries
    assert runtime_service.item_entries[0]["metadata"]["workflow_summary"]["decision_state"]["next_action"] == "inspect_execution_spec"
    assert runtime_service.context_states[0]["decision_state"]["next_action"] == "inspect_execution_spec"


def test_plain_chat_normalization_strips_tool_protocol_messages():
    messages = [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "paper_read",
            "content": "paper observation",
        },
    ]

    normalized = ReActAgent._normalize_messages_for_plain_chat(messages)

    assert normalized == [
        {"role": "user", "content": "用户问题"},
        {"role": "user", "content": "<observation>\npaper observation\n</observation>"},
    ]


def test_function_calling_normalization_preserves_complete_tool_call_groups():
    messages = [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "paper_read",
            "content": "paper observation",
        },
    ]

    normalized = ReActAgent._normalize_messages_for_function_calling(messages)

    assert normalized == [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "paper_read",
            "content": "paper observation",
        },
    ]


def test_function_calling_normalization_downgrades_broken_tool_call_groups():
    messages = [
        {"role": "user", "content": "用户问题"},
        {
            "role": "assistant",
            "content": "先查一下",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "paper_read", "arguments": "{\"query\":\"Fig 3\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_other",
            "name": "paper_read",
            "content": "orphan observation",
        },
    ]

    normalized = ReActAgent._normalize_messages_for_function_calling(messages)

    assert normalized == [
        {"role": "user", "content": "用户问题"},
        {"role": "assistant", "content": "先查一下"},
        {"role": "user", "content": "<observation>\norphan observation\n</observation>"},
    ]
