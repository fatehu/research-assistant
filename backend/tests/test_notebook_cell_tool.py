import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.notebook_tools_impl.tools import NotebookCellTool


def test_notebook_cell_get_marks_sandbox_risky_cells():
    notebook_id = "nb-risky-list"
    notebooks_store = {
        notebook_id: {
            "id": notebook_id,
            "cells": [
                {
                    "id": "cell-risky",
                    "cell_type": "code",
                    "source": "import os\nprint(os.getcwd())",
                    "outputs": [],
                    "metadata": {"created_by": "ai_agent"},
                }
            ],
        }
    }
    tool = NotebookCellTool(notebooks_store, notebook_id, user_authorized=False)

    result = asyncio.run(tool.execute(action="get"))

    assert result.success is True
    assert "⚠️禁用导入:os" in result.output
    assert "不要直接建议执行该单元格" in result.output
    assert result.data["cells"][0]["blocked_imports"] == ["os"]
    assert result.data["cells"][0]["sandbox_risky"] is True


def test_notebook_cell_get_one_warns_about_forbidden_imports():
    notebook_id = "nb-risky-one"
    notebooks_store = {
        notebook_id: {
            "id": notebook_id,
            "cells": [
                {
                    "id": "cell-risky",
                    "cell_type": "code",
                    "source": "import os\nprint(os.listdir('.'))",
                    "outputs": [],
                    "metadata": {"created_by": "ai_agent"},
                }
            ],
        }
    }
    tool = NotebookCellTool(notebooks_store, notebook_id, user_authorized=False)

    result = asyncio.run(tool.execute(action="get_one", cell_index=1))

    assert result.success is True
    assert "风险提示: 检测到沙箱禁用导入 os" in result.output
    assert "不要直接建议执行该单元格" in result.output
    assert result.data["blocked_imports"] == ["os"]
    assert result.data["sandbox_risky"] is True


def test_notebook_cell_prefers_cell_id_over_cell_index_when_both_are_present():
    notebook_id = "nb-cell-id-first"
    notebooks_store = {
        notebook_id: {
            "id": notebook_id,
            "cells": [
                {
                    "id": "cell-one",
                    "cell_type": "code",
                    "source": "print('one')",
                    "outputs": [],
                    "metadata": {},
                },
                {
                    "id": "cell-two",
                    "cell_type": "code",
                    "source": "print('two')",
                    "outputs": [],
                    "metadata": {},
                },
            ],
        }
    }
    tool = NotebookCellTool(notebooks_store, notebook_id, user_authorized=False)

    result = asyncio.run(tool.execute(action="get_one", cell_id="cell-two", cell_index=1))

    assert result.success is True
    assert result.data["id"] == "cell-two"
    assert "print('two')" in result.output
