import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.paper_experiment_adapter_service import PaperExperimentAdapterService
from app.services.paper_experiment_service import PaperExperimentService


def test_paper_experiment_template_policy_blocks_forbidden_ops():
    service = PaperExperimentService(db=None)

    assert service._template_has_forbidden_ops("import os\nprint('x')") is True
    assert service._template_has_forbidden_ops("print(open('x.txt').read())") is True
    assert service._template_has_forbidden_ops("import json\nprint('safe')") is False


def test_paper_experiment_templates_reject_unsafe_without_fallback():
    service = PaperExperimentService(db=None)

    templates = service._resolve_codelab_run_templates(
        intake={
            "codelab_run_templates": [
                {
                    "title": "Unsafe Baseline",
                    "target": "baseline",
                    "description": "Should be replaced",
                    "python_code": "import os\nprint(os.getcwd())",
                }
            ]
        },
    )

    assert templates == []


def test_paper_experiment_missing_template_uses_guard_not_fake_baseline():
    service = PaperExperimentService(db=None)

    code = service._resolve_run_template_code(spec={"codelab_run_templates": []}, run=SimpleNamespace(run_kind="baseline", model_name=None))

    assert "No safe paper-backed executable template is available" in code
    assert "requires_manual_implementation" in code
    assert "MLPClassifier" not in code
    assert "make_classification" not in code


def test_paper_experiment_repo_index_builds_workspace_assets(tmp_path: Path):
    workspace_dir = tmp_path / "workspace"
    repo_dir = workspace_dir / "paper_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("# Demo Repo\n\nTrain with train.py\n", encoding="utf-8")
    (repo_dir / "requirements.txt").write_text("scikit-learn\npandas\n", encoding="utf-8")
    (repo_dir / "train.py").write_text("print('train')\n", encoding="utf-8")
    (repo_dir / "src").mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "evaluate.py").write_text("print('eval')\n", encoding="utf-8")

    adapter = PaperExperimentAdapterService()
    payload = adapter._build_repo_index(  # pylint: disable=protected-access
        workspace_dir=workspace_dir,
        repo_manifest={
            "status": "cloned",
            "repo_dir": str(repo_dir),
        },
        experiment_spec={
            "entrypoint_hints": [
                {"kind": "train_script", "value": "train.py"},
                {"kind": "eval_script", "value": "evaluate.py"},
            ]
        },
    )

    assert payload["status"] == "indexed"
    assert payload["indexed_file_count"] >= 4
    assert any(item["path"] == "train.py" for item in payload["entrypoint_candidates"])
    assert "requirements.txt" in payload["dependency_files"]
    assert payload["readme_excerpt_file"] == "repo_readme_excerpt.md"
    assert (workspace_dir / "repo_file_index.json").exists()
    assert (workspace_dir / "repo_readme_excerpt.md").exists()
