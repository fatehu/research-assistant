import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
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


@pytest.mark.asyncio
async def test_build_paper_intake_payload_prefers_local_markdown_even_when_multimodal_ready(monkeypatch, tmp_path: Path):
    service = PaperExperimentService(db=None)
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    paper = SimpleNamespace(
        id=113,
        title="Bag of Tricks for Efficient Text Classification",
        abstract="demo abstract",
        authors=[],
        year=2016,
        venue="arXiv",
        journal=None,
        arxiv_id="1607.01759",
        doi=None,
        url="https://arxiv.org/abs/1607.01759",
        pdf_url="https://arxiv.org/pdf/1607.01759",
        arxiv_url="https://arxiv.org/abs/1607.01759",
        fields_of_study=[],
        raw_data={},
    )

    async def _fake_ingest_pdf(**kwargs):
        return {
            "document_text": "# Demo\n\nTable 1 data",
            "extractor": "local_structured_pdf_fast",
            "report": {"page_count": 8},
            "document_source_spans": [],
        }

    async def _fake_ensure_pdf_available(*args, **kwargs):
        return pdf_path

    monkeypatch.setattr(service.pdf_ingest_service, "ingest_pdf", _fake_ingest_pdf)
    monkeypatch.setattr(service, "_ensure_pdf_available", _fake_ensure_pdf_available)
    monkeypatch.setattr(service, "_paper_intake_multimodal_ready", lambda: True)
    monkeypatch.setattr(service, "_count_pdf_pages", lambda _path: 8)
    monkeypatch.setattr(settings, "default_llm_provider", "aliyun_qwen35_flash", raising=False)

    payload = await service._build_paper_intake_payload(paper, user_id=1)

    assert payload["source_mode"] == "local_pdf_markdown"
    assert payload["extractor"] == "local_structured_pdf_fast"
    assert payload["page_count"] == 8
    assert "Table 1 data" in payload["paper_markdown"]


@pytest.mark.asyncio
async def test_build_paper_intake_payload_falls_back_to_page_images_when_markdown_missing(monkeypatch, tmp_path: Path):
    service = PaperExperimentService(db=None)
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    paper = SimpleNamespace(
        id=113,
        title="Bag of Tricks for Efficient Text Classification",
        abstract="demo abstract",
        authors=[],
        year=2016,
        venue="arXiv",
        journal=None,
        arxiv_id="1607.01759",
        doi=None,
        url="https://arxiv.org/abs/1607.01759",
        pdf_url="https://arxiv.org/pdf/1607.01759",
        arxiv_url="https://arxiv.org/abs/1607.01759",
        fields_of_study=[],
        raw_data={},
    )

    async def _fake_ingest_pdf(**kwargs):
        return {
            "document_text": "",
            "extractor": "local_structured_pdf_fast",
            "report": {"page_count": 8},
            "document_source_spans": [],
        }

    async def _fake_ensure_pdf_available(*args, **kwargs):
        return pdf_path

    monkeypatch.setattr(service.pdf_ingest_service, "ingest_pdf", _fake_ingest_pdf)
    monkeypatch.setattr(service, "_ensure_pdf_available", _fake_ensure_pdf_available)
    monkeypatch.setattr(service, "_paper_intake_multimodal_ready", lambda: True)
    monkeypatch.setattr(service, "_count_pdf_pages", lambda _path: 8)

    payload = await service._build_paper_intake_payload(paper, user_id=1)

    assert payload["source_mode"] == "local_pdf_page_images"
    assert payload["extractor"] == "dashscope_multimodal_pages"


@pytest.mark.asyncio
async def test_extract_paper_intake_json_uses_multimodal_path_for_page_images(monkeypatch):
    service = PaperExperimentService(db=None)
    expected = {"schema_version": "paper_intake_v1", "paper_profile": {"task_type": "classification"}}

    async def _fake_mm(payload):
        assert payload["source_mode"] == "local_pdf_page_images"
        return expected

    monkeypatch.setattr(service, "_extract_paper_intake_json_from_pdf_images", _fake_mm)

    result = await service._extract_paper_intake_json(
        {
            "source_mode": "local_pdf_page_images",
            "pdf_path": "/tmp/demo.pdf",
            "paper_markdown": "# fallback text",
            "metadata": {},
            "raw_data_text": "{}",
        }
    )

    assert result == expected


def test_build_experiment_spec_keeps_stage1_as_paper_scaffold():
    service = PaperExperimentService(db=None)
    paper = SimpleNamespace(id=113, title="Bag of Tricks for Efficient Text Classification", url="u", pdf_url="p", arxiv_url="a")

    summary = {
        "execution_mode": "repo_backed",
        "repo_urls": ["https://github.com/facebookresearch/fastText"],
        "dataset_urls": ["https://example.com/ag"],
        "paper_intake": {
            "paper_profile": {
                "task_type": "text classification",
                "domain": "nlp",
                "research_direction": "efficient text classification",
                "research_method": "bag of tricks over fastText style linear models",
                "research_content": "classification and tag prediction experiments",
                "contribution_summary": "strong simple baseline",
                "experiment_goal": "show simple models remain competitive",
            },
            "dataset_candidates": [{"name": "AG News"}, {"name": "Sogou"}],
            "models": [{"name": "fastText"}],
            "metrics": [{"name": "accuracy"}],
            "entrypoint_hints": [{"kind": "repo", "value": "official repo"}],
            "optimization_candidates": [{"id": "lr", "name": "learning rate", "category": "hyperparameter", "rationale": "paper discusses tuning"}],
            "reference_links": [{"url": "https://github.com/facebookresearch/fastText", "category": "official_repo", "role": "primary_official"}],
        },
    }

    spec = service._build_experiment_spec(paper, summary)

    assert spec["execution_spec_version"] == "v3_paper_intake_scaffold"
    assert spec["paper_focus"]["research_direction"] == "efficient text classification"
    assert spec["execution_contract"]["runtime"] == "repo_assessment_pending"
    assert spec["optimization_brief"]["first_runs"] == []
    assert spec["codelab_run_templates"] == []


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
