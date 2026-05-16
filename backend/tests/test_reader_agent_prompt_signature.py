from app.api.literature import LiteratureAskAgentCore
from app.services.generative_reader_agent_core import GenerativeReaderAgentCore
from app.services.reader_compose_agent_core import ReaderComposeAgentCore


class _DummyToolRegistry:
    def get(self, name):
        return True

    def get_tools_description(self, include_tool_names=None, user_text=None):
        return "paper_read(query), knowledge_search(query)"

    def list_tools(self, include_tool_names=None, user_text=None):
        return []


def test_reader_agent_system_prompt_accepts_function_calling_kwarg():
    messages = [{"role": "user", "content": "什么是 Transformer"}]
    tool_registry = _DummyToolRegistry()

    for agent_cls in (
        LiteratureAskAgentCore,
        GenerativeReaderAgentCore,
        ReaderComposeAgentCore,
    ):
        agent = agent_cls(
            llm_service=None,
            tool_registry=tool_registry,
            allowed_tool_names={"paper_read", "knowledge_search"},
        )

        prompt = agent._build_system_prompt(messages, function_calling=True)

        assert prompt
