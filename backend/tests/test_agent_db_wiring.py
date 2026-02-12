import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import codelab
from app.api import chat
from app.api import notebook_agent


def test_codelab_agent_chat_uses_db_session_factory_for_tool_registry():
    source = inspect.getsource(codelab.notebook_agent_chat)
    assert "db_session_factory=async_session_factory" in source
    assert "db=None" in source


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
