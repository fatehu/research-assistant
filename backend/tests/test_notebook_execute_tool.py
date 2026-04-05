import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.notebook_tools_impl.tools import NotebookExecuteTool


class _DummyKernel:
    def __init__(self, result):
        self._result = result
        self.last_code = None
        self.last_timeout = None
        self.last_workspace_context = None

    def execute(self, code, timeout=60, workspace_context=None):
        self.last_code = code
        self.last_timeout = timeout
        self.last_workspace_context = workspace_context
        return dict(self._result)


class _DummyKernelManager:
    def __init__(self, kernel):
        self._kernel = kernel
        self.last_notebook_id = None

    def get_or_create_kernel(self, notebook_id):
        self.last_notebook_id = notebook_id
        return self._kernel


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
