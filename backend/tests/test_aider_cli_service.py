from pathlib import Path

import pytest

from app.services.aider_cli_service import AiderCliService
from app.config import settings


@pytest.mark.asyncio
async def test_run_repo_mode_builds_command_and_persists_artifacts(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    repo_dir = workspace_dir / "paper_repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    (repo_dir / "foo.py").write_text("print('before')\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("# demo\n", encoding="utf-8")

    monkeypatch.setattr(AiderCliService, "_resolve_binary", classmethod(lambda cls: "/usr/local/bin/aider"))
    monkeypatch.setattr(
        settings.__class__,
        "get_llm_config",
        lambda self, provider=None: {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "model": "gpt-4o-mini",
        },
    )

    statuses = iter(
        [
            {"stdout": "", "stderr": ""},
            {"stdout": " M foo.py\n", "stderr": ""},
        ]
    )

    async def _fake_git_status_porcelain(cls, repo_dir: Path):
        return next(statuses)

    captured: dict = {}

    async def _fake_run_subprocess(cls, *, command, cwd, env, timeout_seconds):
        captured["command"] = list(command)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        captured["timeout_seconds"] = timeout_seconds
        (cwd / "foo.py").write_text("print('after')\n", encoding="utf-8")
        return {
            "timeout": False,
            "returncode": 0,
            "stdout": "aider edited foo.py\n",
            "command": list(command),
        }

    async def _fake_git_diff_patch(cls, repo_dir: Path):
        return "--- a/foo.py\n+++ b/foo.py\n@@\n-print('before')\n+print('after')\n"

    monkeypatch.setattr(AiderCliService, "_git_status_porcelain", classmethod(_fake_git_status_porcelain))
    monkeypatch.setattr(AiderCliService, "_run_subprocess", classmethod(_fake_run_subprocess))
    monkeypatch.setattr(AiderCliService, "_git_diff_patch", classmethod(_fake_git_diff_patch))

    payload = await AiderCliService.run(
        workspace_dir=workspace_dir,
        instruction="Update foo.py to print after.",
        target_root="repo",
        editable_files=["foo.py"],
        read_only_files=["README.md"],
        provider="openai",
        mode="architect",
        editor_model="gpt-4o",
        dry_run=False,
        auto_test=True,
        test_cmd="pytest -q",
    )

    assert payload["success"] is True
    assert payload["changed_files"] == ["foo.py"]
    assert Path(payload["prompt_path"]).is_file()
    assert Path(payload["stdout_path"]).is_file()
    assert Path(payload["diff_path"]).is_file()
    assert captured["cwd"] == repo_dir
    assert "--architect" in captured["command"]
    assert "--file" in captured["command"]
    assert "foo.py" in captured["command"]
    assert "--read" in captured["command"]
    assert "README.md" in captured["command"]
    assert "--no-auto-commits" in captured["command"]
    assert "--no-dirty-commits" in captured["command"]
    assert "--no-auto-lint" in captured["command"]
    assert "--test-cmd" in captured["command"]
    assert "pytest -q" in captured["command"]
    assert captured["env"]["AIDER_OPENAI_API_KEY"] == "test-key"
    assert captured["env"]["AIDER_OPENAI_API_BASE"] == "https://example.invalid/v1"
    assert "--openai-api-key" not in captured["command"]


@pytest.mark.asyncio
async def test_run_workspace_mode_requires_explicit_editable_files(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True)
    monkeypatch.setattr(AiderCliService, "_resolve_binary", classmethod(lambda cls: "/usr/local/bin/aider"))

    payload = await AiderCliService.run(
        workspace_dir=workspace_dir,
        instruction="Update readme intake.",
        target_root="workspace",
        editable_files=[],
        provider="openai",
    )

    assert payload["success"] is False
    assert payload["error"] == "workspace_editable_files_required"


@pytest.mark.asyncio
async def test_run_workspace_mode_tracks_local_json_diff(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    reference_dir = workspace_dir / "reference" / "repo"
    reference_dir.mkdir(parents=True)
    target_file = reference_dir / "readme_intake.json"
    target_file.write_text('{"status":"before"}\n', encoding="utf-8")

    monkeypatch.setattr(AiderCliService, "_resolve_binary", classmethod(lambda cls: "/usr/local/bin/aider"))
    monkeypatch.setattr(
        settings.__class__,
        "get_llm_config",
        lambda self, provider=None: {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "model": "gpt-4o-mini",
        },
    )

    async def _fake_run_subprocess(cls, *, command, cwd, env, timeout_seconds):
        assert env["AIDER_OPENAI_API_KEY"] == "test-key"
        (cwd / "reference" / "repo" / "readme_intake.json").write_text('{"status":"after"}\n', encoding="utf-8")
        return {
            "timeout": False,
            "returncode": 0,
            "stdout": "updated readme_intake.json\n",
            "command": list(command),
        }

    monkeypatch.setattr(AiderCliService, "_run_subprocess", classmethod(_fake_run_subprocess))

    payload = await AiderCliService.run(
        workspace_dir=workspace_dir,
        instruction="Change status to after.",
        target_root="workspace",
        editable_files=["reference/repo/readme_intake.json"],
        provider="openai",
    )

    assert payload["success"] is True
    assert payload["changed_files"] == ["reference/repo/readme_intake.json"]
    diff_text = Path(payload["diff_path"]).read_text(encoding="utf-8")
    assert '"status":"after"' in diff_text

    read_back = AiderCliService.read_run(
        workspace_dir=workspace_dir,
        run_id=payload["run_id"],
        include_stdout=True,
        include_diff=True,
        max_chars=20000,
    )
    assert read_back["success"] is True
    assert "updated readme_intake.json" in read_back["stdout"]
    assert '"status":"after"' in read_back["diff"]

    tail = AiderCliService.tail_log(
        workspace_dir=workspace_dir,
        run_id=payload["run_id"],
        max_chars=20000,
    )
    assert tail["success"] is True
    assert "updated readme_intake.json" in tail["tail"]
