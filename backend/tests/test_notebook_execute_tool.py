import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.notebook_tools_impl.tools import NotebookExecuteTool


class _DummyKernel:
    def __init__(self, result, delay_seconds=0.0):
        self._result = result
        self._delay_seconds = float(delay_seconds)
        self.last_code = None
        self.last_timeout = None
        self.last_workspace_context = None

    def execute(self, code, timeout=60, workspace_context=None):
        self.last_code = code
        self.last_timeout = timeout
        self.last_workspace_context = workspace_context
        if self._delay_seconds > 0:
            time.sleep(self._delay_seconds)
        return dict(self._result)


class _DummyKernelManager:
    def __init__(self, kernel):
        self._kernel = kernel
        self.last_notebook_id = None

    def get_or_create_kernel(self, notebook_id):
        self.last_notebook_id = notebook_id
        return self._kernel


class _FakeAsyncSessionFactory:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeNotebookService:
    def __init__(self, db_session):
        self.db_session = db_session

    async def save_cell_execution(self, *args, **kwargs):
        return {}


def test_notebook_execute_updates_recent_error_cell_when_code_looks_like_fix():
    notebook_id = "nb-fix"
    notebook = {
        "id": notebook_id,
        "user_id": 7,
        "cells": [
            {
                "id": "cell-error",
                "cell_type": "code",
                "source": "joblib.dump(model, 'model.joblib')\nprint('saved')",
                "outputs": [
                    {
                        "output_type": "error",
                        "content": {
                            "ename": "PolicyViolationError",
                            "evalue": "不在白名单内的模块: joblib",
                            "traceback": [],
                        },
                    }
                ],
                "execution_count": 3,
                "metadata": {},
            }
        ],
        "execution_count": 3,
    }
    notebooks_store = {notebook_id: notebook}
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "saved", "mime_type": "text/plain"}],
            "execution_count": 4,
            "execution_time_ms": 18,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute("joblib.dump(model, 'model.joblib')\nprint('saved ok')", description="修复保存模型的报错")
    )

    assert result.success is True
    assert result.data["operation"] == "update"
    assert result.data["new_cell"] is None
    assert result.data["updated_cell"]["id"] == "cell-error"
    assert len(notebooks_store[notebook_id]["cells"]) == 1
    assert notebooks_store[notebook_id]["cells"][0]["source"].endswith("print('saved ok')")
    assert notebooks_store[notebook_id]["cells"][0]["outputs"][0]["output_type"] == "stream"
    assert kernel.last_workspace_context["display_path"].endswith(f"/7/{notebook_id}")


def test_notebook_execute_appends_new_cell_for_unrelated_code():
    notebook_id = "nb-append"
    notebook = {
        "id": notebook_id,
        "user_id": 9,
        "cells": [
            {
                "id": "cell-error",
                "cell_type": "code",
                "source": "joblib.dump(model, 'model.joblib')\nprint('saved')",
                "outputs": [
                    {
                        "output_type": "error",
                        "content": {
                            "ename": "PolicyViolationError",
                            "evalue": "不在白名单内的模块: joblib",
                            "traceback": [],
                        },
                    }
                ],
                "execution_count": 3,
                "metadata": {},
            }
        ],
        "execution_count": 3,
    }
    notebooks_store = {notebook_id: notebook}
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "plot ready", "mime_type": "text/plain"}],
            "execution_count": 4,
            "execution_time_ms": 20,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute("plt.figure()\nplt.plot([1, 2, 3], [3, 2, 1])\nprint('plot ready')", description="新增一个绘图单元格")
    )

    assert result.success is True
    assert result.data["operation"] == "append"
    assert result.data["updated_cell"] is None
    assert result.data["new_cell"]["id"]
    assert len(notebooks_store[notebook_id]["cells"]) == 2
    assert notebooks_store[notebook_id]["cells"][0]["id"] == "cell-error"


def test_notebook_execute_reuses_recent_ai_draft_cell_instead_of_appending_duplicate():
    notebook_id = "nb-reuse-draft"
    notebook = {
        "id": notebook_id,
        "user_id": 10,
        "cells": [
            {
                "id": "cell-draft",
                "cell_type": "code",
                "source": "files = list_uploaded_files()\nprint(files)",
                "outputs": [],
                "execution_count": None,
                "metadata": {"created_by": "ai_agent"},
            }
        ],
        "execution_count": 0,
    }
    notebooks_store = {notebook_id: notebook}
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "['data.csv']", "mime_type": "text/plain"}],
            "execution_count": 1,
            "execution_time_ms": 12,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute(
            "files = list_uploaded_files()\nprint(files)",
            description="执行刚刚创建的文件检查 cell",
        )
    )

    assert result.success is True
    assert result.data["operation"] == "update"
    assert result.data["new_cell"] is None
    assert result.data["updated_cell"]["id"] == "cell-draft"
    assert len(notebooks_store[notebook_id]["cells"]) == 1
    assert notebooks_store[notebook_id]["cells"][0]["execution_count"] == 1
    assert notebooks_store[notebook_id]["cells"][0]["outputs"][0]["content"] == "['data.csv']"


def test_notebook_execute_does_not_reuse_user_draft_cell():
    notebook_id = "nb-keep-user-draft"
    notebook = {
        "id": notebook_id,
        "user_id": 10,
        "cells": [
            {
                "id": "cell-user-draft",
                "cell_type": "code",
                "source": "files = list_uploaded_files()\nprint(files)",
                "outputs": [],
                "execution_count": None,
                "metadata": {"created_by": "user"},
            }
        ],
        "execution_count": 0,
    }
    notebooks_store = {notebook_id: notebook}
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "['data.csv']", "mime_type": "text/plain"}],
            "execution_count": 1,
            "execution_time_ms": 12,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute(
            "files = list_uploaded_files()\nprint(files)",
            description="执行一段新代码",
        )
    )

    assert result.success is True
    assert result.data["operation"] == "append"
    assert result.data["updated_cell"] is None
    assert result.data["new_cell"]["id"] != "cell-user-draft"
    assert len(notebooks_store[notebook_id]["cells"]) == 2
    assert notebooks_store[notebook_id]["cells"][0]["execution_count"] is None


def test_notebook_execute_prefers_cell_id_over_cell_index_when_both_are_present():
    notebook_id = "nb-execute-id-first"
    notebook = {
        "id": notebook_id,
        "user_id": 11,
        "cells": [
            {
                "id": "cell-one",
                "cell_type": "code",
                "source": "print('one')",
                "outputs": [],
                "execution_count": 1,
                "metadata": {},
            },
            {
                "id": "cell-two",
                "cell_type": "code",
                "source": "print('two')",
                "outputs": [],
                "execution_count": 2,
                "metadata": {},
            },
        ],
        "execution_count": 2,
    }
    notebooks_store = {notebook_id: notebook}
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "patched", "mime_type": "text/plain"}],
            "execution_count": 3,
            "execution_time_ms": 15,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute(
            "print('patched')",
            description="覆盖指定 cell",
            cell_id="cell-two",
            cell_index=1,
            write_mode="replace",
        )
    )

    assert result.success is True
    assert result.data["operation"] == "update"
    assert result.data["updated_cell"]["id"] == "cell-two"
    assert notebooks_store[notebook_id]["cells"][1]["source"] == "print('patched')"
    assert notebooks_store[notebook_id]["cells"][0]["source"] == "print('one')"


def test_notebook_execute_rejects_forbidden_import_without_mutating_notebook():
    notebook_id = "nb-reject-import"
    notebooks_store = {
        notebook_id: {
            "id": notebook_id,
            "user_id": 13,
            "cells": [],
            "execution_count": 0,
        }
    }
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "should not run", "mime_type": "text/plain"}],
            "execution_count": 1,
            "execution_time_ms": 1,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute("import os\nprint(os.listdir('.'))", description="bad file discovery")
    )

    assert result.success is False
    assert result.error == "sandbox_forbidden_import"
    assert result.data["notebook_updated"] is False
    assert result.data["operation"] == "rejected"
    assert notebooks_store[notebook_id]["cells"] == []
    assert kernel.last_code is None


def test_notebook_execute_does_not_persist_failed_runtime_result():
    notebook_id = "nb-runtime-fail"
    notebooks_store = {
        notebook_id: {
            "id": notebook_id,
            "user_id": 15,
            "cells": [],
            "execution_count": 0,
        }
    }
    kernel = _DummyKernel(
        {
            "success": False,
            "outputs": [
                {
                    "output_type": "error",
                    "content": {
                        "ename": "ValueError",
                        "evalue": "bad data",
                        "traceback": [],
                    },
                }
            ],
            "execution_count": 1,
            "execution_time_ms": 5,
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute("raise ValueError('bad data')", description="runtime failure")
    )

    assert result.success is False
    assert result.data["notebook_updated"] is False
    assert result.data["operation"] == "rejected"
    assert result.data["new_cell"] is None
    assert result.data["updated_cell"] is None
    assert notebooks_store[notebook_id]["cells"] == []


def test_notebook_execute_fast_background_completion_flows_back_to_observation(monkeypatch):
    monkeypatch.setattr(
        "app.services.notebook_tools_impl.tools.async_session_factory",
        lambda: _FakeAsyncSessionFactory(),
    )
    monkeypatch.setattr(
        "app.services.notebook_tools_impl.tools.NotebookService",
        _FakeNotebookService,
    )
    monkeypatch.setattr("app.config.settings.codelab_background_inline_wait_seconds", 1.0)

    notebook_id = "nb-fast-bg"
    notebooks_store = {
        notebook_id: {
            "id": notebook_id,
            "user_id": 17,
            "cells": [],
            "execution_count": 0,
        }
    }
    kernel = _DummyKernel(
        {
            "success": True,
            "outputs": [{"output_type": "stream", "content": "training done", "mime_type": "text/plain"}],
            "execution_count": 1,
            "execution_time_ms": 25,
            "terminated_reason": "none",
        }
    )
    tool = NotebookExecuteTool(
        kernel_manager=_DummyKernelManager(kernel),
        notebook_id=notebook_id,
        notebooks_store=notebooks_store,
        user_authorized=True,
    )

    result = asyncio.run(
        tool.execute(
            "model_a.fit(X, y)\nmodel_b.fit(X, y)\nmodel_c.fit(X, y)\nprint('training done')",
            description="训练多个模型",
            run_mode="background",
        )
    )

    assert result.success is True
    assert result.data["background_execution_completed"] is True
    assert result.data.get("background_execution_started") is not True
    assert result.data["outputs"][0]["content"] == "training done"
    assert "training done" in result.output
    assert kernel.last_timeout == 0
    assert notebooks_store[notebook_id]["cells"][0]["outputs"][0]["content"] == "training done"
    assert notebooks_store[notebook_id]["cells"][0]["metadata"]["background_execution"]["status"] == "completed"
