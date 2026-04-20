import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.paper_experiment_adapter_service import PaperExperimentAdapterService


def test_ensure_workspace_archive_from_existing_state_backfills_core_files(tmp_path):
    workspace_dir = tmp_path / "workspace"
    repo_dir = workspace_dir / "paper_repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "README.md").write_text("# Demo Repo\n\nRun the notebook.\n", encoding="utf-8")
    (repo_dir / "train.py").write_text("print('ok')\n", encoding="utf-8")

    paper = SimpleNamespace(
        id=111,
        title="nanoTabPFN",
        abstract="demo abstract",
        authors=[],
        year=2025,
        venue="cs.LG",
        journal=None,
        arxiv_id="2511.03634",
        doi=None,
        url="https://arxiv.org/abs/2511.03634",
        pdf_url="https://arxiv.org/pdf/2511.03634",
        arxiv_url="https://arxiv.org/abs/2511.03634",
        fields_of_study=[],
    )
    summary = {
        "execution_mode": "repo_backed",
        "paper_llm_input": {"source_mode": "local_pdf_markdown", "sent_chars": 1024},
        "paper_intake": {"schema_version": "paper_intake_v1", "paper_profile": {"task_type": "classification"}},
        "workspace_adapter": {
            "repo": {
                "status": "reused",
                "repo_url": "https://github.com/example/demo",
                "repo_dir": str(repo_dir),
            }
        },
    }
    experiment_spec = {
        "task": {"task_type": "classification"},
        "sources": {"repo_urls": ["https://github.com/example/demo"], "dataset_urls": []},
        "entrypoint_hints": [{"kind": "train_script", "value": "train.py", "evidence_text": "train.py"}],
    }

    manifest = PaperExperimentAdapterService().ensure_workspace_archive_from_existing_state(
        paper=paper,
        workspace_dir=workspace_dir,
        summary=summary,
        experiment_spec=experiment_spec,
    )

    assert (workspace_dir / "paper_intake_result.json").is_file()
    assert (workspace_dir / "experiment_spec.json").is_file()
    assert (workspace_dir / "workspace_adapter_manifest.json").is_file()
    assert (workspace_dir / "repo_file_index.json").is_file()
    assert manifest["experiment_spec_file"] == "experiment_spec.json"
    assert manifest["repo"]["repo_url"] == "https://github.com/example/demo"


def test_build_repo_index_extracts_repo_history_url_candidates(tmp_path):
    workspace_dir = tmp_path / "workspace"
    repo_dir = workspace_dir / "paper_repo"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test User"], check=True, capture_output=True, text=True)

    old_readme = (
        "# Demo Repo\n\n"
        "curl http://ml.informatik.uni-freiburg.de/research-artifacts/nanoTabPFN/300k_150x5_2.h5 --output 300k_150x5_2.h5\n"
    )
    new_readme = (
        "# Demo Repo\n\n"
        'curl -L -o 300k_150x5_2.h5 "https://figshare.com/ndownloader/files/58932628?private_link=63fc1ada93e42e388e63"\n'
    )
    (repo_dir / "README.md").write_text(old_readme, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "old source"], check=True, capture_output=True, text=True)
    (repo_dir / "README.md").write_text(new_readme, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "new source"], check=True, capture_output=True, text=True)

    payload = PaperExperimentAdapterService()._build_repo_index(  # pylint: disable=protected-access
        workspace_dir=workspace_dir,
        repo_manifest={
            "status": "cloned",
            "repo_dir": str(repo_dir),
        },
        experiment_spec={},
    )

    assert payload["repo_history_candidates_file"] == "repo_history_url_candidates.json"
    assert payload["history_candidate_count"] >= 2
    history_path = workspace_dir / "repo_history_url_candidates.json"
    assert history_path.is_file()
    content = history_path.read_text(encoding="utf-8")
    assert "ml.informatik.uni-freiburg.de" in content
    assert "figshare.com" in content


@pytest.mark.asyncio
async def test_clone_repo_via_git_uses_non_shallow_clone(monkeypatch, tmp_path):
    captured = {}

    def _fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = list(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = await PaperExperimentAdapterService()._clone_repo_via_git(  # pylint: disable=protected-access
        git_path="/usr/bin/git",
        repo_url="https://github.com/example/demo",
        repo_dir=tmp_path / "paper_repo",
    )

    assert result["status"] == "cloned"
    assert "--depth" not in captured["args"]
