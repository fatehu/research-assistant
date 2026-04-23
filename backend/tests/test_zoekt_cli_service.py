from pathlib import Path

import pytest

from app.services.zoekt_cli_service import ZoektBinarySet, ZoektCliService


@pytest.mark.asyncio
async def test_build_index_treats_zero_returncode_as_success(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    repo_dir = workspace_dir / "paper_repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    monkeypatch.setattr(
        ZoektCliService,
        "_resolve_binary_set",
        classmethod(
            lambda cls: ZoektBinarySet(
                search="/usr/local/bin/zoekt",
                git_index="/usr/local/bin/zoekt-git-index",
                plain_index="/usr/local/bin/zoekt-index",
            )
        ),
    )
    async def _fake_git_head(cls, repo_dir):
        return "abc123"

    async def _fake_git_dirty(cls, repo_dir):
        return False

    monkeypatch.setattr(ZoektCliService, "_git_head", classmethod(_fake_git_head))
    monkeypatch.setattr(ZoektCliService, "_git_dirty", classmethod(_fake_git_dirty))

    async def _fake_run_command(cls, *, command, cwd, timeout_seconds):
        index_dir = Path(command[3])
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "repo.00000.zoekt").write_text("stub", encoding="utf-8")
        return {
            "timeout": False,
            "returncode": 0,
            "stdout": "",
            "stderr": "indexed",
            "command": command,
        }

    monkeypatch.setattr(ZoektCliService, "_run_command", classmethod(_fake_run_command))

    payload = await ZoektCliService.build_index(
        repo_dir=repo_dir,
        workspace_dir=workspace_dir,
        force_reindex=True,
    )

    assert payload["success"] is True
    assert payload["status"] == "created"
    assert payload["repo_head"] == "abc123"
    assert payload["index_file_count"] == 1
    assert (workspace_dir / ".zoekt" / "manifest.json").is_file()
