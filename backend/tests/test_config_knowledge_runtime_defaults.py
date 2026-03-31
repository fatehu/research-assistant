import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import Settings


def test_development_defaults_should_single_thread_ingest_and_disable_resume(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DOCUMENT_TASK_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("KNOWLEDGE_RESUME_RUNNING_DOCUMENTS_ON_STARTUP", raising=False)

    settings = Settings(_env_file=None, app_env="development")

    assert settings.knowledge_document_task_max_concurrency == 1
    assert settings.knowledge_resume_running_documents_on_startup is False


def test_development_should_preserve_explicit_knowledge_runtime_overrides(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DOCUMENT_TASK_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("KNOWLEDGE_RESUME_RUNNING_DOCUMENTS_ON_STARTUP", raising=False)

    settings = Settings(
        _env_file=None,
        app_env="development",
        knowledge_document_task_max_concurrency=3,
        knowledge_resume_running_documents_on_startup=True,
    )

    assert settings.knowledge_document_task_max_concurrency == 3
    assert settings.knowledge_resume_running_documents_on_startup is True


def test_production_should_keep_existing_knowledge_runtime_defaults(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DOCUMENT_TASK_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("KNOWLEDGE_RESUME_RUNNING_DOCUMENTS_ON_STARTUP", raising=False)

    settings = Settings(
        _env_file=None,
        app_env="production",
        secret_key="x" * 32,
        database_url="postgresql://user:strongpass@localhost/research_assistant",
        codelab_runner_enabled=False,
    )

    assert settings.knowledge_document_task_max_concurrency == 2
    assert settings.knowledge_resume_running_documents_on_startup is True
