import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import codelab
from app.api import chat
from app.api import notebook_agent
from app.services import react_agent


def test_codelab_agent_chat_uses_db_session_factory_for_tool_registry():
    source = inspect.getsource(codelab.notebook_agent_chat)
    assert "db_session_factory=async_session_factory" in source
    assert "db=None" in source
    assert 'route_profile="codelab"' in source


def test_notebook_agent_routes_have_db_dependency():
    chat_sig = inspect.signature(notebook_agent.notebook_agent_chat)
    tools_sig = inspect.signature(notebook_agent.get_available_tools)

    assert "db" in chat_sig.parameters
    assert "db" in tools_sig.parameters


def test_notebook_agent_chat_awaits_get_llm_service():
    source = inspect.getsource(notebook_agent.notebook_agent_chat)
    assert "await get_llm_service()" in source


def test_notebook_agent_chat_uses_db_session_factory_for_tool_registry():
    source = inspect.getsource(notebook_agent.notebook_agent_chat)
    assert "db_session_factory=async_session_factory" in source
    assert "db=None" in source


def test_chat_stream_uses_db_session_factory_for_tool_registry():
    source = inspect.getsource(chat.send_message)
    assert "db_session_factory=async_session_factory" in source
    assert "db=None" in source
    assert 'route_profile="chat"' in source


def test_chat_stream_done_payload_includes_rag_metrics():
    source = inspect.getsource(chat.send_message)
    assert 'done_payload["rag_metrics"] = rag_metrics' in source


def test_codelab_done_payload_includes_rag_metrics():
    source = inspect.getsource(codelab.notebook_agent_chat)
    assert 'done_payload["rag_metrics"] = rag_metrics' in source


def test_notebook_agent_done_payload_includes_rag_metrics():
    source = inspect.getsource(notebook_agent.notebook_agent_chat)
    assert 'done_payload["rag_metrics"] = rag_metrics' in source


def test_chat_stream_persists_rag_metrics_to_message_metadata():
    source = inspect.getsource(chat.send_message)
    assert '"rag_metrics": response.get("rag_metrics")' in source
    assert '"citation_index": response.get("citation_index")' in source


def test_codelab_assistant_message_persists_rag_metrics_metadata():
    source = inspect.getsource(codelab.notebook_agent_chat)
    assert 'metadata={"rag_metrics": rag_metrics} if isinstance(rag_metrics, dict) else {}' in source


def test_notebook_agent_assistant_message_persists_rag_metrics_metadata():
    source = inspect.getsource(notebook_agent.notebook_agent_chat)
    assert '"metadata": {"rag_metrics": rag_metrics} if isinstance(rag_metrics, dict) else {}' in source


def test_react_agent_observation_output_is_not_truncated():
    source = inspect.getsource(react_agent.ReActAgent)
    assert "observation_output[:2000]" not in source


def test_notebook_agent_observation_event_is_not_truncated():
    source = inspect.getsource(notebook_agent.notebook_agent_chat)
    assert "[:500]" not in source
