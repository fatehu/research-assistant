import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services import llm_service as llm_service_module
from app.services.llm_service import LLMService


def test_deepseek_test_provider_reuses_deepseek_transport_config(monkeypatch):
    captured = {}

    class _FakeAsyncOpenAI:
        def __init__(self, api_key=None, base_url=None, **kwargs):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(settings, "deepseek_api_key", "test-deepseek-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.example")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-chat")
    monkeypatch.setattr(settings, "deepseek_test_model_alias", "deepseek-chat-test")

    service = LLMService("deepseek_test")

    assert captured == {
        "api_key": "test-deepseek-key",
        "base_url": "https://api.deepseek.example",
    }
    assert service.provider == "deepseek_test"
    assert service.provider_family == "deepseek"
    assert service.config["model"] == "deepseek-chat"
    assert service.config["display_model"] == "deepseek-chat-test"
    assert service.config["context_window_model"] == "deepseek-chat-test"
    assert service.supports_function_calling() is True
