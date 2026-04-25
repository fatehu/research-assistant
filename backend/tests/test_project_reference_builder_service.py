import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import project_reference_builder_service as builder_module
from app.services.project_reference_builder_service import ProjectReferenceBuilderService


@pytest.mark.asyncio
async def test_project_reference_builder_service_builds_reference_files(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))

    service = ProjectReferenceBuilderService(db=object())
    paper = SimpleNamespace(
        id=113,
        title="Example Paper",
        authors=["Alice", "Bob"],
        year=2024,
        venue="ICML",
        journal=None,
    )

    project_dir = tmp_path / "uploads" / "projects" / "7"
    repo_source_dir = project_dir / "repo" / "source"
    repo_source_dir.mkdir(parents=True, exist_ok=True)
    (repo_source_dir / "README.md").write_text("# Repo\n\nRun `python train.py`.\n", encoding="utf-8")
    (repo_source_dir / "train.py").write_text("print('train')\n", encoding="utf-8")
    (repo_source_dir / "requirements.txt").write_text("torch\n", encoding="utf-8")

    async def _fake_build_paper_intake(*, paper, user_id):
        assert paper.id == 113
        assert user_id == 1
        return {
            "paper_markdown": "# Example Paper\n\nFull markdown.\n",
            "paper_intake": {
                "paper_profile": {
                    "task_type": "classification",
                    "domain": "nlp",
                    "problem_statement": "Classify text.",
                    "author_intent": "Improve baseline quality.",
                    "research_direction": "efficient adaptation",
                    "core_innovation": "A lightweight adaptation module.",
                    "contribution_summary": "Better results with fewer parameters.",
                    "research_method": "adapter-based fine-tuning",
                    "research_content": "Train and evaluate an adapted model.",
                    "experiment_goal": "Beat the baseline.",
                },
                "paper_pipeline": {
                    "data_flow": "dataset -> tokenizer -> model",
                    "model_flow": "encoder -> adapter -> classifier",
                    "train_eval_flow": "train then evaluate",
                },
                "code_repositories": [
                    {
                        "url": "https://github.com/example/repo",
                        "role": "primary_official",
                        "priority": "primary",
                        "evidence_text": "official code release",
                    }
                ],
                "dataset_candidates": [
                    {
                        "name": "AG News",
                        "purpose": "training",
                        "source_type": "benchmark_suite",
                        "split_or_config": "default",
                        "evidence_text": "AG News benchmark",
                    }
                ],
                "models": [
                    {"name": "BERT", "role": "base_model", "evidence_text": "BERT encoder"}
                ],
                "metrics": [
                    {"name": "accuracy", "direction": "higher_is_better", "evidence_text": "topline metric"}
                ],
                "training_setup": {"default_params": {"epochs": "3"}},
                "evaluation_setup": {"metrics": ["accuracy"]},
                "verification_questions": [
                    {
                        "id": "vq1",
                        "question": "Which script starts training?",
                        "why_it_matters": "Need the real entrypoint.",
                        "target": "repo",
                    }
                ],
                "optimization_candidates": [
                    {
                        "id": "opt1",
                        "name": "learning rate",
                        "category": "hyperparameter",
                        "rationale": "Paper reports sensitivity to LR.",
                        "expected_effect": "Better convergence.",
                        "risk": "medium",
                        "paper_values": ["1e-4"],
                    }
                ],
                "limitations": ["Exact seed handling is unclear."],
            },
            "intake_metadata": {
                "source_mode": "local_pdf_markdown",
                "extractor": "local_structured_pdf_fast",
                "page_count": 12,
                "total_chars": 4000,
                "sent_chars": 4000,
                "truncated": False,
            },
        }

    async def _fake_materialize_repo(*, project_dir, repo_url, refresh):
        assert project_dir == project_dir  # keep signature check simple
        assert repo_url == "https://github.com/example/repo"
        assert refresh is False
        return {
            "status": "reused",
            "repo_url": repo_url,
            "repo_source_dir": str(repo_source_dir),
        }

    async def _fake_generate(self, *, repo_url, readme_relative_path, readme_text):
        assert repo_url == "https://github.com/example/repo"
        assert readme_relative_path == "README.md"
        assert "python train.py" in readme_text
        return {
            "schema_version": "repo_readme_reproduction_intake_v1",
            "repo_url": repo_url,
            "readme_relative_path": readme_relative_path,
            "reproduction_goal": "Run the baseline from README",
            "environment_requirements": {
                "languages": ["python"],
                "package_managers": ["pip"],
                "system_dependencies": [],
                "python_version": None,
                "hardware_hints": [],
                "notes": [],
            },
            "installation_steps": [],
            "run_commands": [{"label": "train", "command": "python train.py", "purpose": "train baseline"}],
            "entrypoints": [],
            "dataset_materials": [],
            "evaluation_steps": [],
            "expected_outputs": [],
            "focus_files": [],
            "focus_directories": [],
            "blocking_questions": [],
            "evidence_snippets": [],
        }

    async def _fake_build_project_index(*, project_dir, workspace_dir, force_reindex):
        assert project_dir == repo_source_dir.parent.parent
        assert workspace_dir == repo_source_dir.parent.parent
        assert force_reindex is False
        return {
            "success": True,
            "status": "created",
            "index_dir": str(project_dir / ".zoekt_project" / "index"),
        }

    monkeypatch.setattr(service, "_build_paper_intake", _fake_build_paper_intake)
    monkeypatch.setattr(service, "_materialize_repo", _fake_materialize_repo)
    monkeypatch.setattr(builder_module.RepoReadmeReproductionIntakeService, "generate", _fake_generate)
    monkeypatch.setattr(builder_module.ZoektCliService, "build_project_index", _fake_build_project_index)

    summary = await service.build(paper=paper, project_id=7, user_id=1, refresh=False)

    assert summary["reference_ready"] is True
    assert summary["zoekt_index"]["status"] == "created"
    assert (project_dir / "reference" / "paper" / "paper_pdf2md.md").is_file()
    assert (project_dir / "reference" / "paper" / "paper_interpretation.md").is_file()
    assert (project_dir / "reference" / "paper" / "paper_interpretation.json").is_file()
    assert (project_dir / "reference" / "repo" / "readme_intake.json").is_file()

    paper_interpretation = json.loads((project_dir / "reference" / "paper" / "paper_interpretation.json").read_text(encoding="utf-8"))
    assert paper_interpretation["core_innovation"] == "A lightweight adaptation module."
    assert paper_interpretation["tuning_directions"][0]["name"] == "learning rate"

    readme_intake = json.loads((project_dir / "reference" / "repo" / "readme_intake.json").read_text(encoding="utf-8"))
    assert readme_intake["status"] == "ready"
    assert "requirements.txt" in readme_intake["dependency_files"]
    assert readme_intake["repo_structure"]["top_level_entries"]
    assert "train.py" in readme_intake["focus_files"]
