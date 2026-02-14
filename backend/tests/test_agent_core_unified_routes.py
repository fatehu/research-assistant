import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import chat, codelab


def test_chat_and_codelab_both_use_create_react_agent():
    chat_source = inspect.getsource(chat.send_message)
    codelab_source = inspect.getsource(codelab.notebook_agent_chat)

    assert "create_react_agent(" in chat_source
    assert "create_react_agent(" in codelab_source


def test_chat_and_codelab_pass_runtime_context():
    chat_source = inspect.getsource(chat.send_message)
    codelab_source = inspect.getsource(codelab.notebook_agent_chat)

    assert "AgentRuntimeContext(" in chat_source
    assert "AgentRuntimeContext(" in codelab_source
