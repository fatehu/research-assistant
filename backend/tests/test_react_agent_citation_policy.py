import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.react_agent import ReActAgent


class _DummyLLM:
    provider = "test"
    config = {"model": "test-model"}


class _DummyTools:
    def get_tools_description(self) -> str:
        return "- knowledge_search: 搜索知识库"


def test_system_prompt_contains_citation_policy():
    agent = ReActAgent(_DummyLLM(), _DummyTools(), max_iterations=1)
    prompt = agent._build_system_prompt()

    assert "知识检索引用规范" in prompt
    assert "[来源X]" in prompt
    assert "禁止编造不存在的来源编号" in prompt


def test_observation_message_for_knowledge_search_requires_citation():
    message = ReActAgent._build_observation_message(
        "knowledge_search",
        "[来源1] 这是检索结果",
    )

    assert "<observation>" in message
    assert "必须在关键结论后保留对应的 [来源X] 标注" in message
    assert "只能使用 observation 中出现过的来源编号" in message


def test_observation_message_for_other_tools_is_generic():
    message = ReActAgent._build_observation_message("calculator", "4")

    assert "<observation>" in message
    assert "请根据工具返回的信息继续。如果已有足够信息，请用<answer>标签给出最终回答。" in message

