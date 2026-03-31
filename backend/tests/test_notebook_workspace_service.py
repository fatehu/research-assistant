import io
import os
import sys

import pytest
from fastapi import UploadFile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.codelab_executor import CodeLabExecutor
from app.services.notebook_workspace_service import (
    build_notebook_workspace_context,
    save_notebook_workspace_upload,
)


@pytest.fixture(autouse=True)
def _disable_runner(monkeypatch):
    monkeypatch.setattr("app.config.settings.codelab_runner_enabled", False)


@pytest.mark.asyncio
async def test_notebook_workspace_upload_builds_context(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))

    upload = UploadFile(filename="dataset.csv", file=io.BytesIO(b"a,b\n1,2\n3,4\n"))
    saved = await save_notebook_workspace_upload("nb-files", 12, upload)
    workspace = build_notebook_workspace_context("nb-files", 12)

    assert saved["name"] == "dataset.csv"
    assert workspace["file_count"] == 1
    assert workspace["file_names"] == ["dataset.csv"]
    assert workspace["display_path"].endswith("/12/nb-files")
    assert workspace["files"][0]["runtime_path"].endswith("dataset.csv")


def test_codelab_executor_reads_uploaded_files_from_workspace(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "data.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (workspace_dir / "notes.txt").write_text("alpha beta", encoding="utf-8")

    executor = CodeLabExecutor(notebook_id="workspace-exec", hard_timeout_seconds=10)
    try:
        result = executor.execute(
            "\n".join(
                [
                    "import numpy as np",
                    "print(list_uploaded_files())",
                    "print(np.loadtxt('data.csv', delimiter=',', skiprows=1).shape)",
                    "print(read_uploaded_text('notes.txt'))",
                    "print(uploaded_file_path('data.csv').endswith('data.csv'))",
                ]
            ),
            timeout_seconds=8,
            workspace_context={
                "directory": str(workspace_dir),
                "file_names": ["data.csv", "notes.txt"],
                "file_paths": {
                    "data.csv": str(workspace_dir / "data.csv"),
                    "notes.txt": str(workspace_dir / "notes.txt"),
                },
            },
        )

        assert result["success"] is True
        stream_output = "\n".join(str(item.get("content") or "") for item in result["outputs"])
        assert "['data.csv', 'notes.txt']" in stream_output
        assert "(2, 2)" in stream_output
        assert "alpha beta" in stream_output
        assert "True" in stream_output
        assert result["variables"]["NOTEBOOK_FILES"] == "list"
    finally:
        executor.close()
