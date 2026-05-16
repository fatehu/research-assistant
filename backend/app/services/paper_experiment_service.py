from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.literature import Paper, PaperExperimentRun, PaperExperimentWorkspace
from app.services.codelab_sandbox_policy import SANDBOX_FORBIDDEN_IMPORT_ROOTS
from app.services.dashscope_multimodal_service import DashScopeMultimodalService
from app.services.literature_service import get_literature_service
from app.services.llm_service import LLMService
from app.services.paper_experiment_adapter_service import PaperExperimentAdapterService
from app.services.notebook_service import NotebookService
from app.services.online_mm_ingest_service import OnlineMmIngestService
from app.services.pdf_rag_ingest_service import PdfRagIngestService
from app.services.model_context_windows import resolve_model_context_window


_REPO_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_DATASET_URL_RE = re.compile(
    r"https?://(?:huggingface\.co/datasets/|www\.kaggle\.com/|zenodo\.org/|figshare\.com/|drive\.google\.com/)[^\s\"']+"
)
_HIGHER_IS_BETTER_TOKENS = ("acc", "accuracy", "f1", "auc", "precision", "recall", "score", "bleu", "rouge")
_LOWER_IS_BETTER_TOKENS = ("loss", "error", "wer", "perplexity", "rmse", "mae", "mse")
_PAPER_INTAKE_OUTPUT_TOKENS = 8192
_PAPER_INTAKE_TIMEOUT_SECONDS = 600
_RAW_DATA_CONTEXT_MAX_CHARS = 24000
_PAPER_MARKDOWN_STORE_MAX_CHARS = 1200000
_PAPER_TEMPLATE_FORBIDDEN_PATTERNS = tuple(sorted(SANDBOX_FORBIDDEN_IMPORT_ROOTS)) + (
    "open(",
    "exec(",
    "eval(",
    "__import__",
)


_PAPER_INTAKE_SYSTEM_PROMPT = """You are a paper PDF-to-structured-intake engine for ML/DL research workflows.

Return STRICT JSON only. Do not include Markdown, comments, or explanatory prose.
The first character of the response must be `{` and the last character must be `}`. Do not wrap the JSON in ``` fences.

You will receive the paper metadata, raw import metadata, and either rendered PDF page images or markdown rendered from a local PDF parser.
Use the full paper content to extract the structured facts and discovery hints needed for later repo/data inspection.
This stage does not execute code, inspect external repositories, or generate runnable code.

Current task:
- Read the paper as a research engineer preparing later repo and notebook verification.
- First produce a reliable paper-guidance artifact, not an execution plan.
- Identify and classify links that the paper explicitly provides.
- Prioritize four outputs:
  1. author intent: what problem the authors want to solve, their core idea, and the main innovation
  2. paper pipeline: how data enters, how the model processes it, and the high-level train/eval flow
  3. verification questions: what later repo/notebook inspection must confirm, what the paper leaves unclear, and what is most likely to block reproduction
  4. weak hypotheses: likely important factors, key gain sources, or modules worth verifying later
- Extract datasets, models, metrics, protocols, and discovery hints only as paper-grounded clues for later repo/runtime work.

Rules:
- Do not invent URLs, repository names, dataset links, commands, or dependencies.
- Prioritize the narrative sections of the paper: title, abstract, introduction, method, experiment text, conclusion, and figure captions.
- Treat table cells as supporting evidence, not as the single source of truth.
- When the PDF contains multiple experiment groups, tables, or benchmark suites, keep their boundaries clear.
- Do not merge datasets across different tables/experiments unless the paper explicitly says they belong to the same reproduction target.
- If table structure is ambiguous after PDF parsing, trust the surrounding narrative text first and record the ambiguity in `limitations` or `discovery_tasks`.
- If the paper mentions a dataset name but no URL, return the name with url=null.
- Every extracted item must include short evidence_text copied or tightly paraphrased from the paper.
- Keep evidence_text concise: no more than 120 characters per item.
- Prefer concrete evidence about code availability, dataset availability, task, models, metrics, train/eval settings, hyperparameters, scripts, and artifacts.
- Output paper understanding, planning constraints, and follow-up discovery tasks, not implementation.
- Do not output Python code, shell commands, package installation commands, synthetic scripts, or fake repo file paths.
- If repo/code/data are not in the PDF, mark them as missing and add discovery_tasks.
- Distinguish dataset purpose and source type. For example, prior dumps for pretraining are different from sklearn built-in demo datasets or benchmark evaluation datasets.
- If a data artifact is mentioned as a downloadable dump, external file, repository asset, built-in sklearn dataset, or benchmark split, encode that explicitly in dataset_candidates.
- If the paper mentions a README, notebook, example, or file-like artifact such as an experiment notebook, prior dump filename, config, or sklearn loader, keep it as a hint; do not turn it into code.
- If a URL is the paper's own official repository, set role="primary_official". Strong evidence includes phrases such as "our code", "code and details are open-sourced", "official implementation", or a repository name matching the paper/topic/authors.
- If a URL is used only for a compared baseline model, set role="baseline_implementation".
- If a URL is a general external library/tool/reference, set role="third_party_reference".
- Keep verification_status="paper_claimed" for links asserted by the PDF; do not set "externally_verified" because this stage does not browse the web.
- Keep `entrypoint_hints`, `optimization_candidates`, and `model_swap_candidates` sparse and optional.
- Only include them when the paper explicitly provides strong narrative evidence; otherwise use [].
- Do not imply any hint, optimization candidate, or model swap is directly runnable until repo/data/configs have been inspected.
- Keep symbolic parameter values as JSON strings. Do not output invalid JSON expressions such as 4/3, 2/3, NaN, Infinity, or comments.
- Keep the JSON concise but complete.
- Include all important items needed for downstream paper understanding and repo verification.
- Do not drop main benchmark datasets, primary repositories, core baselines, metrics, protocols, or critical verification questions just to be brief.
- Do not generate runnable execution plans, baseline commands, variant scripts, first-run instructions, or tuning plans from the paper alone.
- Order items by downstream importance:
  1. primary official repository and reproduction-critical links
  2. main benchmark datasets and required splits
  3. proposed models and strong baselines
  4. metrics and protocol details
  5. required discovery tasks, verification questions, and blockers
  6. weak hypotheses and optional low-confidence hints
  7. optional or low-priority references
- Avoid repetitive or low-value items.
- If something is not present, use null or [].
- Do not output self-rated confidence.

Required JSON shape:
{
  "schema_version": "paper_intake_v1",
  "paper_profile": {
    "task_type": string|null,
    "domain": string|null,
    "author_intent": string|null,
    "problem_statement": string|null,
    "research_direction": string|null,
    "research_method": string|null,
    "research_content": string|null,
    "core_innovation": string|null,
    "contribution_summary": string|null,
    "experiment_goal": string|null
  },
  "reference_links": [
    {
      "url": string,
      "category": "official_repo"|"project_page"|"dataset_or_download"|"benchmark_reference"|"third_party_reference"|"unknown",
      "label": string|null,
      "role": "primary_official"|"supporting"|"reference"|"unknown",
      "verification_status": "paper_claimed"|"unverified",
      "evidence_text": string,
      "evidence_section": string|null
    }
  ],
  "code_repositories": [
    {
      "url": string,
      "role": "primary_official"|"baseline_implementation"|"third_party_reference"|"unknown",
      "verification_status": "paper_claimed"|"unverified",
      "supports": [string],
      "priority": "primary"|"secondary"|"reference"|"unknown",
      "evidence_text": string,
      "evidence_section": string|null
    }
  ],
  "project_page_candidates": [
    {"url": string, "evidence_text": string, "evidence_section": string|null}
  ],
  "dataset_candidates": [
    {
      "name": string,
      "url": string|null,
      "split_or_config": string|null,
      "purpose": "pretraining"|"training"|"evaluation"|"demo"|"benchmark"|"unknown",
      "source_type": "paper_provided"|"external_dump"|"sklearn_builtin"|"repo_asset"|"external_repository"|"benchmark_suite"|"unknown",
      "requires_download": boolean,
      "artifact_hint": string|null,
      "evidence_text": string,
      "evidence_section": string|null
    }
  ],
  "models": [
    {"name": string, "role": string|null, "evidence_text": string}
  ],
  "metrics": [
    {"name": string, "direction": "higher_is_better"|"lower_is_better"|"unknown", "evidence_text": string}
  ],
  "training_setup": {
    "default_params": object,
    "resource_hints": object,
    "dependencies_mentioned": [string],
    "evidence_text": string|null
  },
  "evaluation_setup": {
    "metrics": [string],
    "artifacts": [string],
    "evidence_text": string|null
  },
  "paper_pipeline": {
    "data_flow": string|null,
    "model_flow": string|null,
    "train_eval_flow": string|null,
    "evidence_text": string|null
  },
  "verification_questions": [
    {
      "id": string,
      "question": string,
      "why_it_matters": string,
      "target": "repo"|"notebook"|"dataset"|"runtime"|"metric"|"unknown"
    }
  ],
  "entrypoint_hints": [
    {"kind": "repo"|"notebook"|"train_script"|"eval_script"|"config"|"readme"|"example"|"project_page"|"unknown", "value": string|null, "evidence_text": string}
  ],
  "optimization_candidates": [
    {
      "id": string,
      "name": string,
      "category": "hyperparameter"|"architecture"|"preprocessing"|"training_protocol"|"model_swap"|"evaluation"|"data"|"system"|"unknown",
      "applies_to": [string],
      "paper_values": [string],
      "suggested_search_space": {
        "type": "choice"|"range"|"boolean"|"freeform"|"unknown",
        "values": [string],
        "range": {"min": string|null, "max": string|null, "step": string|null}
      },
      "rationale": string,
      "expected_effect": string|null,
      "risk": "low"|"medium"|"high"|null,
      "requires_repo_verification": boolean,
      "requires_dataset_verification": boolean,
      "evidence_text": string|null
    }
  ],
  "model_swap_candidates": [
    {
      "name": string,
      "swap_type": "baseline_comparison"|"stronger_model"|"lighter_model"|"ablation"|"unknown",
      "reason": string|null,
      "expected_effect": string|null,
      "risk": "low"|"medium"|"high"|null,
      "evidence_text": string|null
    }
  ],
  "discovery_tasks": [
    {
      "id": string,
      "target": "repo"|"dataset"|"project_page"|"supplementary"|"entrypoint"|"config"|"metric"|"unknown",
      "query_or_hint": string,
      "reason": string,
      "required_before_execution": boolean
    }
  ],
  "limitations": [string]
}
"""


class PaperExperimentService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.notebook_service = NotebookService(db)
        self.pdf_ingest_service = PdfRagIngestService()
        self.adapter_service = PaperExperimentAdapterService()

    async def get_workspace(self, *, paper_id: int, user_id: int) -> Optional[PaperExperimentWorkspace]:
        result = await self.db.execute(
            select(PaperExperimentWorkspace)
            .where(
                PaperExperimentWorkspace.paper_id == int(paper_id),
                PaperExperimentWorkspace.user_id == int(user_id),
            )
            .options(selectinload(PaperExperimentWorkspace.runs))
        )
        return result.scalar_one_or_none()

    async def bootstrap_workspace(self, *, paper: Paper, user_id: int) -> PaperExperimentWorkspace:
        workspace = await self.get_workspace(paper_id=int(paper.id), user_id=int(user_id))
        if workspace is not None:
            return workspace

        # 工作区首次创建时先产出论文理解和实验规格，再创建笔记本，
        # 避免笔记本中出现与后续持久化状态不一致的占位内容。
        bundle = await self._build_workspace_bundle(paper, user_id=user_id)
        summary = dict(bundle.get("summary") or {})
        experiment_spec = self._build_experiment_spec(paper, summary)
        summary["paper_summary"] = self.adapter_service.build_paper_summary(
            paper=paper,
            summary=summary,
            experiment_spec=experiment_spec,
        )
        notebook = await self.notebook_service.create_notebook(
            user_id=int(user_id),
            title=f"{paper.title[:72]} - Experiment Workspace",
            description="Paper-backed ML/DL experiment workspace",
            initial_cells=[
                {
                    "cell_type": "markdown",
                    "source": f"# {paper.title}\n\nPreparing experiment workspace...",
                    "metadata": {
                        "created_by": "paper_experiment",
                        "slot": "workspace_pending",
                    },
                }
            ],
        )

        workspace = PaperExperimentWorkspace(
            user_id=int(user_id),
            paper_id=int(paper.id),
            notebook_id=str(notebook["id"]),
            status="ready",
            title=f"{paper.title[:120]} - Experiment Workspace",
            summary_json=summary,
            experiment_spec_json=experiment_spec,
            compare_report_json=self._build_compare_report([]),
        )
        self.db.add(workspace)
        await self.db.flush()

        # 适配器会把论文摄取结果、仓库索引和模板文件落到笔记本工作区，
        # 后续代码单元只读取这些受控资产，不直接假设仓库结构。
        adapter_manifest = await self._materialize_workspace_assets(
            paper=paper,
            notebook_id=str(notebook["id"]),
            user_id=int(user_id),
            summary=summary,
            experiment_spec=experiment_spec,
            materials=dict(bundle.get("materials") or {}),
        )
        summary["workspace_adapter"] = adapter_manifest
        experiment_spec["workspace_adapter"] = adapter_manifest
        workspace.summary_json = summary
        workspace.experiment_spec_json = experiment_spec

        await self._sync_workspace_intro_cells(
            notebook_id=str(notebook["id"]),
            user_id=int(user_id),
            paper=paper,
            experiment_spec=experiment_spec,
        )

        baseline_run = await self._create_run_record(
            workspace=workspace,
            label="Baseline",
            run_kind="baseline",
            model_name=str(experiment_spec.get("baseline", {}).get("model_family") or "").strip() or None,
            hypothesis="Establish a reproducible starting point for subsequent variants.",
            params=experiment_spec.get("baseline", {}).get("default_params") or {},
            variant_spec={"type": "baseline"},
            seed_notebook=True,
        )
        workspace.compare_report_json = self._build_compare_report([baseline_run])
        await self.db.commit()
        return await self.get_workspace(paper_id=int(paper.id), user_id=int(user_id)) or workspace

    async def refresh_workspace_intake(
        self,
        *,
        paper: Paper,
        workspace: PaperExperimentWorkspace,
    ) -> PaperExperimentWorkspace:
        bundle = await self._build_workspace_bundle(paper, user_id=int(workspace.user_id))
        summary = dict(bundle.get("summary") or {})
        experiment_spec = self._build_experiment_spec(paper, summary)
        summary["paper_summary"] = self.adapter_service.build_paper_summary(
            paper=paper,
            summary=summary,
            experiment_spec=experiment_spec,
        )

        adapter_manifest = await self._materialize_workspace_assets(
            paper=paper,
            notebook_id=str(workspace.notebook_id or ""),
            user_id=int(workspace.user_id),
            summary=summary,
            experiment_spec=experiment_spec,
            materials=dict(bundle.get("materials") or {}),
        )
        summary["workspace_adapter"] = adapter_manifest
        experiment_spec["workspace_adapter"] = adapter_manifest
        workspace.summary_json = summary
        workspace.experiment_spec_json = experiment_spec
        workspace.updated_at = datetime.utcnow()

        if workspace.notebook_id:
            await self._sync_workspace_intro_cells(
                notebook_id=str(workspace.notebook_id),
                user_id=int(workspace.user_id),
                paper=paper,
                experiment_spec=experiment_spec,
            )
            await self._refresh_run_cells(workspace=workspace)

        await self.db.commit()
        return await self.get_workspace(paper_id=int(paper.id), user_id=int(workspace.user_id)) or workspace

    async def create_run(
        self,
        *,
        workspace: PaperExperimentWorkspace,
        label: str,
        run_kind: str,
        model_name: Optional[str],
        hypothesis: Optional[str],
        params: Dict[str, Any],
        variant_spec: Dict[str, Any],
        base_run_id: Optional[int] = None,
    ) -> PaperExperimentRun:
        run = await self._create_run_record(
            workspace=workspace,
            label=label,
            run_kind=run_kind,
            model_name=model_name,
            hypothesis=hypothesis,
            params=params,
            variant_spec={**dict(variant_spec or {}), "base_run_id": base_run_id},
            base_run_id=base_run_id,
            seed_notebook=True,
        )
        workspace.compare_report_json = self._build_compare_report(self._unique_runs(list(workspace.runs) + [run]))
        workspace.updated_at = datetime.utcnow()
        await self.db.commit()
        return run

    async def update_run(
        self,
        *,
        workspace: PaperExperimentWorkspace,
        run: PaperExperimentRun,
        status: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        artifacts: Optional[Dict[str, Any]] = None,
        summary: Optional[Dict[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> PaperExperimentRun:
        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            run.status = normalized_status
            if normalized_status == "running" and run.started_at is None:
                run.started_at = datetime.utcnow()
            if normalized_status in {"completed", "failed", "cancelled"}:
                run.completed_at = datetime.utcnow()

        if metrics is not None:
            run.metrics_json = dict(metrics)
        if artifacts is not None:
            run.artifacts_json = dict(artifacts)
        if summary is not None:
            run.summary_json = dict(summary)
        if notes is not None:
            run.notes = notes.strip() or None

        run.updated_at = datetime.utcnow()
        workspace.compare_report_json = self._build_compare_report(list(workspace.runs))
        workspace.updated_at = datetime.utcnow()
        await self.db.commit()
        return run

    async def get_run(
        self,
        *,
        workspace_id: int,
        run_id: int,
        user_id: int,
    ) -> Optional[PaperExperimentRun]:
        result = await self.db.execute(
            select(PaperExperimentRun)
            .where(
                PaperExperimentRun.id == int(run_id),
                PaperExperimentRun.workspace_id == int(workspace_id),
                PaperExperimentRun.user_id == int(user_id),
            )
        )
        return result.scalar_one_or_none()

    async def _create_run_record(
        self,
        *,
        workspace: PaperExperimentWorkspace,
        label: str,
        run_kind: str,
        model_name: Optional[str],
        hypothesis: Optional[str],
        params: Dict[str, Any],
        variant_spec: Dict[str, Any],
        base_run_id: Optional[int] = None,
        seed_notebook: bool,
    ) -> PaperExperimentRun:
        run = PaperExperimentRun(
            workspace_id=int(workspace.id),
            user_id=int(workspace.user_id),
            notebook_id=workspace.notebook_id,
            base_run_id=base_run_id,
            run_kind=str(run_kind or "variant").strip() or "variant",
            status="draft",
            label=str(label or "").strip() or "Unnamed Run",
            model_name=str(model_name or "").strip() or None,
            hypothesis=str(hypothesis or "").strip() or None,
            variant_spec_json=dict(variant_spec or {}),
            params_json=dict(params or {}),
            metrics_json={},
            artifacts_json={},
            summary_json={},
        )
        self.db.add(run)
        await self.db.flush()

        if seed_notebook and workspace.notebook_id:
            notebook_cell_id = await self._seed_run_cells(workspace=workspace, run=run)
            run.notebook_cell_id = notebook_cell_id

        workspace.status = "active"
        workspace.updated_at = datetime.utcnow()
        return run

    async def _seed_run_cells(self, *, workspace: PaperExperimentWorkspace, run: PaperExperimentRun) -> Optional[str]:
        notebook_id = str(workspace.notebook_id or "").strip()
        if not notebook_id:
            return None

        markdown_source = self._build_run_markdown(workspace, run)
        notebook = await self.notebook_service.add_cell(
            notebook_id,
            int(workspace.user_id),
            cell_type="markdown",
            source=markdown_source,
            metadata={
                "created_by": "paper_experiment",
                "slot": "run_markdown",
                "paper_experiment_run_id": int(run.id),
            },
        )
        _ = notebook  # 明确保留：先持久化说明单元，再追加代码单元。

        code_source = self._build_run_code(workspace, run)
        notebook_after_code = await self.notebook_service.add_cell(
            notebook_id,
            int(workspace.user_id),
            cell_type="code",
            source=code_source,
            metadata={
                "created_by": "paper_experiment",
                "slot": "run_code",
                "paper_experiment_run_id": int(run.id),
            },
        )
        if not notebook_after_code:
            return None
        cells = list(notebook_after_code.get("cells") or [])
        if not cells:
            return None
        return str((cells[-1] or {}).get("id") or "") or None

    async def _refresh_run_cells(self, *, workspace: PaperExperimentWorkspace) -> None:
        notebook_id = str(workspace.notebook_id or "").strip()
        if not notebook_id:
            return
        for run in list(workspace.runs or []):
            cell_id = str(run.notebook_cell_id or "").strip()
            if not cell_id:
                continue
            await self.notebook_service.update_cell(
                notebook_id,
                int(workspace.user_id),
                cell_id,
                source=self._build_run_code(workspace, run),
                metadata={
                    "created_by": "paper_experiment",
                    "slot": "run_code",
                    "paper_experiment_run_id": int(run.id),
                },
            )

    async def _sync_workspace_intro_cells(
        self,
        *,
        notebook_id: str,
        user_id: int,
        paper: Paper,
        experiment_spec: Dict[str, Any],
    ) -> None:
        notebook = await self.notebook_service.get_notebook(notebook_id, user_id)
        if not notebook:
            return

        intro_cells = self._build_initial_notebook_cells(paper, experiment_spec)
        intro_slots = {
            str(dict(cell.get("metadata") or {}).get("slot") or "").strip()
            for cell in intro_cells
            if str(dict(cell.get("metadata") or {}).get("slot") or "").strip()
        }
        # 介绍单元按 slot 覆盖系统生成区，用户后来新增的实验单元会保留原位。
        existing_cells = list(notebook.get("cells") or [])
        slot_to_id: Dict[str, str] = {}
        filtered_cells: List[Dict[str, Any]] = []
        for cell in existing_cells:
            metadata = dict(cell.get("metadata") or {})
            if str(metadata.get("created_by") or "") == "paper_experiment":
                slot = str(metadata.get("slot") or "").strip()
                if slot in intro_slots or slot == "workspace_pending":
                    if slot and str(cell.get("id") or "").strip():
                        slot_to_id[slot] = str(cell.get("id") or "").strip()
                    continue
            filtered_cells.append(cell)

        for cell in intro_cells:
            slot = str(dict(cell.get("metadata") or {}).get("slot") or "").strip()
            if slot and slot in slot_to_id:
                cell["id"] = slot_to_id[slot]

        await self.notebook_service.sync_cells(
            notebook_id,
            user_id,
            [*intro_cells, *filtered_cells],
        )

    async def _materialize_workspace_assets(
        self,
        *,
        paper: Paper,
        notebook_id: str,
        user_id: int,
        summary: Dict[str, Any],
        experiment_spec: Dict[str, Any],
        materials: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized_notebook_id = str(notebook_id or "").strip()
        if not normalized_notebook_id:
            return {
                "status": "skipped",
                "message": "Notebook is missing; workspace assets were not materialized.",
            }
        try:
            return await self.adapter_service.prepare_workspace(
                paper=paper,
                notebook_id=normalized_notebook_id,
                user_id=int(user_id),
                summary=summary,
                experiment_spec=experiment_spec,
                materials=materials,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PaperExperiment] workspace materialization failed paper_id={paper.id}: {exc}")
            return {
                "status": "failed",
                "message": f"{type(exc).__name__}: {exc}",
            }

    async def _build_workspace_bundle(self, paper: Paper, *, user_id: int) -> Dict[str, Any]:
        repo_urls = self._collect_matching_urls(paper.raw_data, _REPO_URL_RE)
        dataset_urls = self._collect_matching_urls(paper.raw_data, _DATASET_URL_RE)
        source_links = [item for item in [paper.url, paper.pdf_url, paper.arxiv_url] if isinstance(item, str) and item.strip()]
        summary = {
            "paper_title": paper.title,
            "paper_year": paper.year,
            "paper_venue": paper.venue,
            "arxiv_id": paper.arxiv_id,
            "source_links": source_links,
            "repo_urls": repo_urls,
            "dataset_urls": dataset_urls,
            "abstract_excerpt": (paper.abstract or "")[:800],
            "execution_mode": "repo_backed" if repo_urls else "paper_backed",
            "readiness": {
                "has_repo": bool(repo_urls),
                "has_dataset_link": bool(dataset_urls),
                "has_pdf": bool(paper.pdf_url or paper.pdf_path or paper.pdf_downloaded),
            },
        }

        intake_payload = await self._build_paper_intake_payload(paper, user_id=user_id)
        # 论文 Markdown 可能很长；summary 只保存元数据，正文放入材料包，
        # 供适配器按需写入工作区文件。
        summary["paper_llm_input"] = {
            key: value
            for key, value in intake_payload.items()
            if key not in {"paper_markdown", "paper_markdown_spans", "raw_data_text"}
        }
        summary["paper_markdown_meta"] = {
            "source_mode": intake_payload.get("source_mode"),
            "extractor": intake_payload.get("extractor"),
            "total_chars": intake_payload.get("total_chars"),
            "stored_chars": intake_payload.get("stored_chars"),
            "sent_chars": intake_payload.get("sent_chars"),
            "truncated": intake_payload.get("truncated"),
            "store_truncated": intake_payload.get("store_truncated"),
            "llm_truncated": intake_payload.get("llm_truncated"),
            "sha256": intake_payload.get("sha256"),
            "span_count": len(list(intake_payload.get("paper_markdown_spans") or [])),
            "report": intake_payload.get("report") or {},
        }

        paper_intake: Dict[str, Any] = {}
        has_intake_input = bool(
            str(intake_payload.get("paper_markdown") or "").strip()
            or (
                str(intake_payload.get("source_mode") or "").strip() == "local_pdf_page_images"
                and str(intake_payload.get("pdf_path") or "").strip()
            )
        )
        if has_intake_input:
            try:
                paper_intake = await self._extract_paper_intake_json(intake_payload)
            except Exception as exc:  # noqa: BLE001 - intake must fail open; workspace scaffold should still be created
                # 大模型摄取失败不能阻断工作区创建；后续仍可基于论文元数据和
                # 适配器脚手架继续人工补全。
                logger.warning(f"[PaperExperiment] paper intake LLM failed paper_id={paper.id}: {exc}")
                summary["paper_intake_error"] = f"{type(exc).__name__}: {exc}"

        summary["paper_intake"] = paper_intake
        llm_repo_urls = self._collect_urls_from_intake(
            paper_intake,
            keys=("code_repositories", "repo_candidates", "project_page_candidates"),
            require_github=True,
        )
        llm_dataset_urls = self._collect_urls_from_intake(
            paper_intake,
            keys=("dataset_candidates",),
            require_github=False,
        )
        summary["repo_urls"] = self._merge_unique_strings(repo_urls, llm_repo_urls)
        summary["dataset_urls"] = self._merge_unique_strings(dataset_urls, llm_dataset_urls)
        summary["execution_mode"] = "repo_backed" if summary["repo_urls"] else "paper_backed"
        summary["readiness"] = {
            **dict(summary.get("readiness") or {}),
            "has_repo": bool(summary["repo_urls"]),
            "has_dataset_link": bool(summary["dataset_urls"]),
            "has_llm_intake": bool(paper_intake),
            "has_markdown": bool(str(intake_payload.get("paper_markdown") or "").strip()),
        }
        return {
            "summary": summary,
            "materials": {
                "paper_markdown": str(
                    intake_payload.get("stored_paper_markdown")
                    or intake_payload.get("paper_markdown")
                    or ""
                ),
                "paper_markdown_spans": list(intake_payload.get("paper_markdown_spans") or []),
                "intake_payload": intake_payload,
                "paper_intake": paper_intake,
            },
        }

    def _build_experiment_spec(self, paper: Paper, summary: Dict[str, Any]) -> Dict[str, Any]:
        title = str(paper.title or "")
        lower_title = title.lower()
        intake = self._as_dict(summary.get("paper_intake"))
        intake_profile = self._as_dict(intake.get("paper_profile"))
        training_setup = self._as_dict(intake.get("training_setup"))
        entrypoint_hints = self._as_list(intake.get("entrypoint_hints"))
        models = self._as_list(intake.get("models"))
        metrics = self._as_list(intake.get("metrics"))
        default_model_family = self._first_string(
            [
                self._as_dict(models[0]).get("name") if models else None,
                "transformer" if any(token in lower_title for token in ("bert", "transformer", "gpt", "llm")) else None,
                "baseline",
            ]
        )
        # 第一版 spec 只表达“论文给出的假设和线索”，不把它们直接升级成可执行命令。
        optimization_candidates = self._as_list(intake.get("optimization_candidates"))
        safe_knobs = self._normalize_safe_knobs(intake.get("safe_knobs"))
        risky_knobs = self._as_list(intake.get("risky_knobs"))
        default_params = self._as_dict(training_setup.get("default_params"))
        first_entrypoint = self._first_entrypoint_hint(entrypoint_hints)
        execution_assets = {
            "code_repositories": self._as_list(intake.get("code_repositories")),
            "repo_candidates": self._as_list(intake.get("repo_candidates")),
            "project_page_candidates": self._as_list(intake.get("project_page_candidates")),
            "dataset_candidates": self._as_list(intake.get("dataset_candidates")),
            "entrypoint_hints": entrypoint_hints,
        }
        reference_links = self._as_list(intake.get("reference_links"))
        weak_hypotheses = [
            {
                "name": str(item.get("name") or item.get("id") or "").strip() or None,
                "category": str(item.get("category") or "").strip() or None,
                "rationale": str(item.get("rationale") or "").strip() or None,
                "expected_effect": str(item.get("expected_effect") or "").strip() or None,
            }
            for item in optimization_candidates[:8]
            if isinstance(item, dict) and str(item.get("name") or item.get("id") or "").strip()
        ]
        paper_focus = {
            "research_direction": self._first_string(
                [
                    intake_profile.get("research_direction"),
                    intake_profile.get("problem_statement"),
                ]
            ),
            "research_method": self._first_string(
                [
                    intake_profile.get("research_method"),
                    intake_profile.get("contribution_summary"),
                ]
            ),
            "research_content": self._first_string(
                [
                    intake_profile.get("research_content"),
                    intake_profile.get("experiment_goal"),
                ]
            ),
            "weak_hypotheses": weak_hypotheses,
        }
        return {
            "execution_spec_version": "v3_paper_intake_scaffold",
            "spec_role": "paper_derived_hypothesis",
            "grounding_status": "paper_only",
            "paper_id": int(paper.id),
            "title": paper.title,
            "execution_mode": summary.get("execution_mode") or "paper_backed",
            "task": intake_profile,
            "datasets": self._as_list(intake.get("dataset_candidates")),
            "models": models,
            "metrics": metrics,
            "paper_focus": paper_focus,
            "execution_assets": execution_assets,
            "training_setup": training_setup,
            "evaluation_setup": self._as_dict(intake.get("evaluation_setup")),
            "entrypoint_hints": entrypoint_hints,
            "baseline": {
                "entrypoint_type": str(first_entrypoint.get("kind") or "paper_hint"),
                "entrypoint_hint": str(
                    first_entrypoint.get("value")
                    or first_entrypoint.get("evidence_text")
                    or "Paper-only hint. Derive the real repo main path later from README/scripts/notebooks."
                ),
                "model_family": default_model_family,
                "default_params": default_params,
            },
            "execution_contract": {
                "runtime": "repo_assessment_pending",
                "file_access": "workspace_helpers_only",
                "sync_keys": ["run_metrics", "run_artifacts"],
            },
            "safe_knobs": safe_knobs,
            "risky_knobs": risky_knobs,
            "optimization_candidates": optimization_candidates,
            "allowed_model_swaps": self._as_list(intake.get("model_swap_candidates")),
            "optimization_brief": {
                "human_summary": self._first_string(
                    [
                        paper_focus.get("research_method"),
                        paper_focus.get("research_content"),
                    ]
                ),
                "recommended_strategy": "Treat these as paper-grounded hints only. Use repo evidence before turning them into executable changes.",
                "first_runs": [],
                "do_not_change_first": [
                    "Do not treat stage-1 paper hints as executable repo commands.",
                    "Do not lock the execution scope until repo mainpath assessment is complete.",
                ],
            },
            "variant_ideas": [],
            "discovery_tasks": self._as_list(intake.get("discovery_tasks")),
            "run_plan_templates": [],
            "notebook_scaffold": [],
            "codelab_run_templates": [],
            "sources": {
                "paper_url": paper.url,
                "pdf_url": paper.pdf_url,
                "arxiv_url": paper.arxiv_url,
                "repo_urls": summary.get("repo_urls") or [],
                "dataset_urls": summary.get("dataset_urls") or [],
                "reference_links": reference_links[:24],
            },
            "intake_status": {
                "has_llm_intake": bool(intake),
                "input": summary.get("paper_llm_input") or {},
                "markdown": summary.get("paper_markdown_meta") or {},
                "error": summary.get("paper_intake_error"),
            },
        }

    def _build_initial_notebook_cells(self, paper: Paper, experiment_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        links = experiment_spec.get("sources") or {}
        overview_lines = [
            f"# {paper.title}",
            "",
            "## Experiment Workspace",
            "",
            f"- Year: {paper.year or 'Unknown'}",
            f"- Venue: {paper.venue or 'Unknown'}",
            f"- arXiv: {paper.arxiv_id or 'N/A'}",
            f"- Paper URL: {links.get('paper_url') or 'N/A'}",
            f"- PDF URL: {links.get('pdf_url') or 'N/A'}",
        ]
        repo_urls = links.get("repo_urls") or []
        dataset_urls = links.get("dataset_urls") or []
        if repo_urls:
            overview_lines.append(f"- Repo: {repo_urls[0]}")
        if dataset_urls:
            overview_lines.append(f"- Dataset: {dataset_urls[0]}")
        task = experiment_spec.get("task") or {}
        if isinstance(task, dict) and task.get("task_type"):
            overview_lines.append(f"- Task: {task.get('task_type')}")
        model_names = [str((item or {}).get("name") or "").strip() for item in list(experiment_spec.get("models") or []) if isinstance(item, dict)]
        metric_names = [str((item or {}).get("name") or "").strip() for item in list(experiment_spec.get("metrics") or []) if isinstance(item, dict)]
        if model_names:
            overview_lines.append(f"- Models: {', '.join(model_names[:6])}")
        if metric_names:
            overview_lines.append(f"- Metrics: {', '.join(metric_names[:6])}")
        overview_lines.extend(["", "## Abstract", "", paper.abstract or "No abstract available."])

        code_source = (
            "import json\n"
            "EXPERIMENT_SPEC = json.loads(read_uploaded_text('experiment_spec.json'))\n"
            "PAPER_INTAKE = json.loads(read_uploaded_text('paper_intake_result.json'))\n"
            "WORKSPACE_ADAPTER = json.loads(read_uploaded_text('workspace_adapter_manifest.json'))\n"
            "REPO_REFERENCE = json.loads(read_uploaded_text('repo_reference.json'))\n"
            "REPO_FILE_INDEX = json.loads(read_uploaded_text('repo_file_index.json'))\n"
            "BASELINE_CONFIG = dict((EXPERIMENT_SPEC.get('baseline') or {}).get('default_params') or {})\n"
            "SAFE_KNOBS = list(EXPERIMENT_SPEC.get('safe_knobs') or [])\n"
            "WORKSPACE_FILES = list_uploaded_files()\n"
            "print('Workspace ready for execution')\n"
            "print('Template files:', [item.get('file_name') for item in WORKSPACE_ADAPTER.get('template_files', [])])\n"
            "print('Repo status:', dict(WORKSPACE_ADAPTER.get('repo') or {}).get('status'))\n"
            "print('Repo indexed files:', REPO_FILE_INDEX.get('indexed_file_count'))\n"
            "print('Repo entrypoints:', [item.get('path') for item in REPO_FILE_INDEX.get('entrypoint_candidates', [])[:8]])\n"
            "print('Dependency files:', REPO_FILE_INDEX.get('dependency_files', [])[:8])\n"
            "print('Workspace files:', WORKSPACE_FILES)\n"
            "print('Baseline config:', BASELINE_CONFIG)\n"
        )
        tuning_cell = self._build_tuning_choices_code(experiment_spec)
        optimization_markdown = self._build_optimization_plan_markdown(experiment_spec)
        return [
            {
                "cell_type": "markdown",
                "source": "\n".join(overview_lines),
                "metadata": {"created_by": "paper_experiment", "slot": "workspace_overview"},
            },
            {
                "cell_type": "code",
                "source": code_source,
                "metadata": {"created_by": "paper_experiment", "slot": "workspace_loader"},
            },
            {
                "cell_type": "markdown",
                "source": optimization_markdown,
                "metadata": {"created_by": "paper_experiment", "slot": "workspace_plan"},
            },
            {
                "cell_type": "code",
                "source": tuning_cell,
                "metadata": {"created_by": "paper_experiment", "slot": "workspace_tuning"},
            },
        ]

    def _build_run_markdown(self, workspace: PaperExperimentWorkspace, run: PaperExperimentRun) -> str:
        lines = [
            f"## {run.label}",
            "",
            f"- Kind: {run.run_kind}",
            f"- Status: {run.status}",
        ]
        if run.model_name:
            lines.append(f"- Model: {run.model_name}")
        if run.hypothesis:
            lines.extend(["", "### Hypothesis", "", run.hypothesis])
        return "\n".join(lines)

    def _build_run_code(self, workspace: PaperExperimentWorkspace, run: PaperExperimentRun) -> str:
        params_json = json.dumps(run.params_json or {}, ensure_ascii=False, indent=2)
        variant_json = json.dumps(run.variant_spec_json or {}, ensure_ascii=False, indent=2)
        spec = self._as_dict(workspace.experiment_spec_json)
        template_code = self._resolve_run_template_code(spec=spec, run=run)
        return (
            f"# Executable run config: {run.label}\n"
            "import json\n"
            f"RUN_ID = {run.id}\n"
            f"RUN_LABEL = {run.label!r}\n"
            f"RUN_KIND = {run.run_kind!r}\n"
            f"RUN_MODEL = {run.model_name!r}\n"
            f"RUN_PARAMS = {params_json}\n"
            f"RUN_VARIANT_SPEC = {variant_json}\n"
            "EXPERIMENT_SPEC = json.loads(read_uploaded_text('experiment_spec.json'))\n"
            "PAPER_INTAKE = json.loads(read_uploaded_text('paper_intake_result.json'))\n"
            "WORKSPACE_ADAPTER = json.loads(read_uploaded_text('workspace_adapter_manifest.json'))\n"
            "REPO_REFERENCE = json.loads(read_uploaded_text('repo_reference.json'))\n"
            "REPO_FILE_INDEX = json.loads(read_uploaded_text('repo_file_index.json'))\n"
            "TUNABLE_PARAMETERS = list(EXPERIMENT_SPEC.get('safe_knobs') or [])\n"
            "OPTIMIZATION_BRIEF = dict(EXPERIMENT_SPEC.get('optimization_brief') or {})\n"
            "MODEL_SWAP_CANDIDATES = list(EXPERIMENT_SPEC.get('allowed_model_swaps') or [])\n"
            "SOURCES = dict(EXPERIMENT_SPEC.get('sources') or {})\n"
            "\n"
            "BASELINE_CONFIG = dict((EXPERIMENT_SPEC.get('baseline') or {}).get('default_params') or {})\n"
            "RUN_CONFIG = dict(BASELINE_CONFIG)\n"
            "for key, value in RUN_PARAMS.items():\n"
            "    if value is not None:\n"
            "        RUN_CONFIG[key] = value\n"
            "if RUN_MODEL:\n"
            "    RUN_CONFIG['model'] = RUN_MODEL\n"
            "\n"
            "RUN_COMMAND_HINT = (EXPERIMENT_SPEC.get('baseline') or {}).get('entrypoint_hint')\n"
            "DATASET_URLS = SOURCES.get('dataset_urls') or []\n"
            "REPO_URLS = SOURCES.get('repo_urls') or []\n"
            "WORKSPACE_FILES = list_uploaded_files()\n"
            "\n"
            "run_metrics = {}\n"
            "run_artifacts = {\n"
            "    'run_config': RUN_CONFIG,\n"
            "    'repo_urls': REPO_URLS,\n"
            "    'dataset_urls': DATASET_URLS,\n"
            "    'repo_status': dict(REPO_REFERENCE or {}).get('status'),\n"
            "    'repo_entrypoints': [item.get('path') for item in REPO_FILE_INDEX.get('entrypoint_candidates', [])[:8]],\n"
            "    'repo_dependency_files': REPO_FILE_INDEX.get('dependency_files', [])[:8],\n"
            "    'command_hint': RUN_COMMAND_HINT,\n"
            "    'optimization_brief': OPTIMIZATION_BRIEF,\n"
            "    'workspace_files': WORKSPACE_FILES,\n"
            "}\n"
            "print('Prepared executable run for', RUN_LABEL)\n"
            "print('Run config:', RUN_CONFIG)\n"
            "print('Repo URLs:', REPO_URLS)\n"
            "print('Dataset URLs:', DATASET_URLS)\n"
            "print('Command hint:', RUN_COMMAND_HINT)\n\n"
            f"{template_code}\n\n"
            "if not isinstance(run_metrics, dict):\n"
            "    run_metrics = {}\n"
            "if not isinstance(run_artifacts, dict):\n"
            "    run_artifacts = {}\n"
            "run_artifacts.setdefault('workspace_adapter_status', dict(WORKSPACE_ADAPTER or {}).get('status'))\n"
            "run_artifacts.setdefault('run_kind', RUN_KIND)\n"
        )

    def _build_optimization_plan_markdown(self, experiment_spec: Dict[str, Any]) -> str:
        brief = self._as_dict(experiment_spec.get("optimization_brief"))
        lines = [
            "## Optimization Plan",
            "",
            str(brief.get("human_summary") or "Use the extracted paper settings to run a baseline, then compare controlled variants."),
            "",
            "### Recommended Strategy",
            "",
            str(brief.get("recommended_strategy") or "Start with low-risk hyperparameters, then test compatible model swaps after baseline metrics are stable."),
        ]
        first_runs = self._as_list(brief.get("first_runs"))
        if first_runs:
            lines.extend(["", "### First Runs"])
            for item in first_runs[:6]:
                payload = self._as_dict(item)
                label = str(payload.get("label") or "Variant").strip()
                goal = str(payload.get("goal") or payload.get("expected_effect") or "").strip()
                changes = self._as_dict(payload.get("changes"))
                lines.append(f"- {label}: {goal or json.dumps(changes, ensure_ascii=False)}")
        do_not_change = [str(item).strip() for item in self._as_list(brief.get("do_not_change_first")) if str(item).strip()]
        if do_not_change:
            lines.extend(["", "### Do Not Change First", "", ", ".join(do_not_change[:8])])
        return "\n".join(lines)

    def _build_tuning_choices_code(self, experiment_spec: Dict[str, Any]) -> str:
        return (
            "# Runnable tuning choices generated from paper intake\n"
            "import json\n"
            "EXPERIMENT_SPEC = json.loads(read_uploaded_text('experiment_spec.json'))\n"
            "TUNABLE_PARAMETERS = list(EXPERIMENT_SPEC.get('safe_knobs') or [])\n"
            "VARIANT_IDEAS = list(EXPERIMENT_SPEC.get('variant_ideas') or [])\n"
            "OPTIMIZATION_BRIEF = dict(EXPERIMENT_SPEC.get('optimization_brief') or {})\n"
            "\n"
            "BASELINE_CONFIG = dict((EXPERIMENT_SPEC.get('baseline') or {}).get('default_params') or {})\n"
            "\n"
            "def build_variant_config(overrides=None):\n"
            "    config = dict(BASELINE_CONFIG)\n"
            "    for key, value in dict(overrides or {}).items():\n"
            "        if value is not None:\n"
            "            config[key] = value\n"
            "    return config\n"
            "\n"
            "suggested_run_configs = []\n"
            "for item in TUNABLE_PARAMETERS:\n"
            "    key = item.get('key')\n"
            "    values = item.get('suggested_values') or []\n"
            "    if key and values:\n"
            "        for value in values[:3]:\n"
            "            suggested_run_configs.append({\n"
            "                'label': f'{key}={value}',\n"
            "                'config': build_variant_config({key: value}),\n"
            "                'reason': item.get('reason'),\n"
            "            })\n"
            "\n"
            "run_metrics = {}\n"
            "run_artifacts = {\n"
            "    'baseline_config': BASELINE_CONFIG,\n"
            "    'tunable_parameters': TUNABLE_PARAMETERS,\n"
            "    'suggested_run_configs': suggested_run_configs,\n"
            "    'variant_ideas': VARIANT_IDEAS,\n"
            "}\n"
            "print('Baseline config:', BASELINE_CONFIG)\n"
            "print('Tunable parameters:', TUNABLE_PARAMETERS)\n"
            "print('Suggested run configs:', suggested_run_configs[:6])\n"
        )

    def _build_intake_refresh_markdown(self, summary: Dict[str, Any], experiment_spec: Dict[str, Any]) -> str:
        task = self._as_dict(experiment_spec.get("task"))
        models = [
            str(self._as_dict(item).get("name") or "").strip()
            for item in self._as_list(experiment_spec.get("models"))
            if str(self._as_dict(item).get("name") or "").strip()
        ]
        metrics = [
            str(self._as_dict(item).get("name") or "").strip()
            for item in self._as_list(experiment_spec.get("metrics"))
            if str(self._as_dict(item).get("name") or "").strip()
        ]
        datasets = [
            str(self._as_dict(item).get("name") or self._as_dict(item).get("url") or "").strip()
            for item in self._as_list(experiment_spec.get("datasets"))
            if str(self._as_dict(item).get("name") or self._as_dict(item).get("url") or "").strip()
        ]
        lines = [
            "## Paper Intake Refresh",
            "",
            f"- LLM intake: {'available' if summary.get('paper_intake') else 'not available'}",
            f"- Input chars: {self._as_dict(summary.get('paper_llm_input')).get('sent_chars') or 0}",
            f"- Input truncated: {self._as_dict(summary.get('paper_llm_input')).get('truncated') or False}",
        ]
        if task.get("task_type"):
            lines.append(f"- Task: {task.get('task_type')}")
        if datasets:
            lines.append(f"- Datasets: {', '.join(datasets[:8])}")
        if models:
            lines.append(f"- Models: {', '.join(models[:8])}")
        if metrics:
            lines.append(f"- Metrics: {', '.join(metrics[:8])}")
        return "\n".join(lines)

    def _build_intake_refresh_code(self, summary: Dict[str, Any], experiment_spec: Dict[str, Any]) -> str:
        intake_json = json.dumps(summary.get("paper_intake") or {}, ensure_ascii=False, indent=2, default=str)
        spec_json = json.dumps(experiment_spec, ensure_ascii=False, indent=2, default=str)
        return (
            "# Paper intake payload generated from the full paper context\n"
            f"PAPER_INTAKE = {intake_json}\n"
            f"EXPERIMENT_SPEC = {spec_json}\n"
            "print('Paper intake refreshed')\n"
            "print('Repo candidates:', EXPERIMENT_SPEC.get('sources', {}).get('repo_urls', []))\n"
            "print('Dataset candidates:', EXPERIMENT_SPEC.get('sources', {}).get('dataset_urls', []))\n"
        )

    async def _build_paper_intake_payload(self, paper: Paper, *, user_id: int) -> Dict[str, Any]:
        pdf_path = await self._ensure_pdf_available(paper, user_id=user_id)
        paper_markdown = ""
        source_mode = "metadata_abstract_fallback"
        extractor_name = None
        report: Dict[str, Any] = {}
        markdown_spans: List[Dict[str, Any]] = []
        page_count = 0
        if pdf_path:
            try:
                page_count = self._count_pdf_pages(pdf_path)
            except Exception as exc:  # noqa: BLE001 - metadata only; intake can still continue
                logger.warning(f"[PaperExperiment] count PDF pages failed paper_id={paper.id}: {exc}")
            try:
                ingest = await self.pdf_ingest_service.ingest_pdf(
                    file_path=str(pdf_path),
                    document_name=pdf_path.name,
                    mode="fast",
                )
                paper_markdown = str(ingest.get("document_text") or "")
                extractor_name = str(ingest.get("extractor") or "local_structured_pdf_fast").strip() or "local_structured_pdf_fast"
                report = dict(ingest.get("report") or {})
                markdown_spans = list(ingest.get("document_source_spans") or [])
                if paper_markdown.strip():
                    source_mode = "local_pdf_markdown"
            except Exception as exc:  # noqa: BLE001 - intake can still use abstract/raw metadata
                logger.warning(f"[PaperExperiment] local PDF markdown extraction failed paper_id={paper.id}: {exc}")
            if not paper_markdown.strip() and self._paper_intake_multimodal_ready():
                source_mode = "local_pdf_page_images"
                extractor_name = "dashscope_multimodal_pages"

        if not paper_markdown.strip():
            paper_markdown = str(paper.abstract or "")
            if source_mode != "local_pdf_page_images":
                extractor_name = "abstract_fallback"
                source_mode = "metadata_abstract_fallback"

        provider = str(
            getattr(settings, "paper_intake_provider", "")
            or getattr(settings, "default_llm_provider", "deepseek")
            or "deepseek"
        )
        model = str((settings.get_llm_config(provider) or {}).get("model") or "")
        original_paper_markdown = paper_markdown
        original_total_chars = len(original_paper_markdown)
        paper_markdown = original_paper_markdown
        store_truncated = False
        paper_markdown_for_llm = paper_markdown
        llm_truncated = False
        raw_data_text = json.dumps(paper.raw_data or {}, ensure_ascii=False, indent=2, default=str)
        raw_data_text = raw_data_text[:_RAW_DATA_CONTEXT_MAX_CHARS]
        metadata = {
            "id": paper.id,
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors or [],
            "year": paper.year,
            "venue": paper.venue,
            "journal": paper.journal,
            "arxiv_id": paper.arxiv_id,
            "doi": paper.doi,
            "url": paper.url,
            "pdf_url": paper.pdf_url,
            "arxiv_url": paper.arxiv_url,
            "fields_of_study": paper.fields_of_study or [],
        }
        text_hash = hashlib.sha256(paper_markdown.encode("utf-8", errors="ignore")).hexdigest() if paper_markdown else ""
        return {
            "metadata": metadata,
            "raw_data_text": raw_data_text,
            "paper_markdown": paper_markdown_for_llm,
            "stored_paper_markdown": paper_markdown,
            "paper_markdown_spans": markdown_spans,
            "source_mode": source_mode,
            "pdf_path": str(pdf_path or ""),
            "extractor": extractor_name,
            "report": report,
            "page_count": int(page_count),
            "provider": provider,
            "model": model,
            "total_chars": original_total_chars,
            "stored_chars": len(paper_markdown),
            "sent_chars": len(paper_markdown_for_llm),
            "truncated": bool(store_truncated or llm_truncated),
            "store_truncated": bool(store_truncated),
            "llm_truncated": bool(llm_truncated),
            "sha256": text_hash,
        }

    async def _extract_paper_intake_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        source_mode = str(payload.get("source_mode") or "").strip()
        if source_mode == "local_pdf_page_images" and str(payload.get("pdf_path") or "").strip():
            try:
                return await self._extract_paper_intake_json_from_pdf_images(payload)
            except Exception as exc:  # noqa: BLE001 - fail open to markdown fallback when available
                logger.warning(f"[PaperExperiment] multimodal intake failed, fallback to text path: {exc}")
                if not str(payload.get("paper_markdown") or "").strip():
                    raise

        user_payload = {
            "metadata": payload.get("metadata") or {},
            "raw_import_metadata_json": payload.get("raw_data_text") or "{}",
            "input_info": {
                "source_mode": payload.get("source_mode"),
                "extractor": payload.get("extractor"),
                "page_count": payload.get("page_count"),
                "total_chars": payload.get("total_chars"),
                "sent_chars": payload.get("sent_chars"),
                "truncated": payload.get("truncated"),
                "report": payload.get("report") or {},
            },
            "full_paper_markdown": payload.get("paper_markdown") or "",
        }
        messages = [
            {
                "role": "user",
                "content": (
                    "Extract the paper-to-experiment workspace JSON from this payload.\n"
                    "Use the full_paper_markdown heavily. Return JSON only.\n\n"
                    f"{json.dumps(user_payload, ensure_ascii=False, default=str)}"
                ),
            }
        ]
        llm = LLMService()
        timeout_seconds = max(
            60,
            int(getattr(settings, "paper_intake_timeout_seconds", _PAPER_INTAKE_TIMEOUT_SECONDS) or _PAPER_INTAKE_TIMEOUT_SECONDS),
        )
        response = await asyncio.wait_for(
            llm.chat(
                messages=messages,
                system_prompt=_PAPER_INTAKE_SYSTEM_PROMPT,
                temperature=0.0,
                max_tokens=max(int(getattr(settings, "llm_max_tokens", 4096) or 4096), _PAPER_INTAKE_OUTPUT_TOKENS),
                source="paper_intake.execution_plan",
            ),
            timeout=timeout_seconds,
        )
        finish_reason = str(response.get("finish_reason") or "").strip().lower()
        if finish_reason == "length":
            raise ValueError("paper intake response was truncated by max_tokens before complete JSON")
        parsed = self._parse_json_object(str(response.get("content") or ""))
        if not parsed:
            raise ValueError("paper intake response is empty")
        return parsed if isinstance(parsed, dict) else {}

    async def _extract_paper_intake_json_from_pdf_images(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        pdf_path = Path(str(payload.get("pdf_path") or "").strip()).expanduser()
        if not pdf_path.is_file():
            raise ValueError("paper intake pdf missing for multimodal path")

        page_limit = max(1, int(getattr(settings, "paper_intake_multimodal_max_pages", 24) or 24))
        page_count = int(payload.get("page_count") or 0)
        if page_count > page_limit:
            raise ValueError(f"paper intake page_count_exceeds_limit:{page_count}>{page_limit}")

        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_dashscope_api_base", "") or getattr(settings, "aliyun_base_url", "") or "").strip()
        model = self._resolve_paper_intake_multimodal_model()
        if not api_key or not base_url:
            raise ValueError("paper intake multimodal credentials unavailable")

        multimodal_payload = {
            "metadata": payload.get("metadata") or {},
            "raw_import_metadata_json": payload.get("raw_data_text") or "{}",
            "input_info": {
                "source_mode": payload.get("source_mode"),
                "extractor": payload.get("extractor"),
                "page_count": page_count,
                "report": payload.get("report") or {},
            },
        }
        user_prompt = (
            "Extract the paper-to-experiment workspace JSON from the attached full-paper page images.\n"
            "Use the page images as primary evidence, especially for tables, benchmark group boundaries, dataset lists, and experiment sections.\n"
            "Do not flatten multiple experiment groups into one dataset list unless the paper explicitly says they are the same reproduction target.\n"
            "Return JSON only.\n\n"
            f"{json.dumps(multimodal_payload, ensure_ascii=False, default=str)}"
        )

        with tempfile.TemporaryDirectory(prefix="paper_intake_mm_") as temp_dir:
            image_paths = OnlineMmIngestService._render_pdf_pages(pdf_path=pdf_path, out_dir=Path(temp_dir))
            if not image_paths:
                raise ValueError("paper intake multimodal render returned no pages")
            response = await DashScopeMultimodalService.chat_json(
                api_key=api_key,
                base_url=base_url,
                model=model,
                system_prompt=_PAPER_INTAKE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                image_paths=[str(path) for path in image_paths],
                max_tokens=max(int(getattr(settings, "llm_max_tokens", 4096) or 4096), _PAPER_INTAKE_OUTPUT_TOKENS),
                temperature=0.0,
            )
        parsed = dict(response.get("parsed") or {})
        if not parsed:
            parsed = self._parse_json_object(str(response.get("raw_text") or "")) or {}
        if not parsed:
            raise ValueError("paper intake multimodal response is empty")
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _count_pdf_pages(pdf_path: Path) -> int:
        return int(OnlineMmIngestService._count_pages(pdf_path))

    def _paper_intake_multimodal_ready(self) -> bool:
        if not bool(getattr(settings, "paper_intake_multimodal_enabled", True)):
            return False
        if not DashScopeMultimodalService.is_available():
            return False
        api_key = str(getattr(settings, "aliyun_api_key", "") or "").strip()
        base_url = str(getattr(settings, "aliyun_dashscope_api_base", "") or getattr(settings, "aliyun_base_url", "") or "").strip()
        return bool(api_key and base_url)

    @staticmethod
    def _resolve_paper_intake_multimodal_model() -> str:
        return str(
            getattr(settings, "paper_intake_multimodal_model", "")
            or getattr(settings, "kb_online_mm_primary_model", "")
            or "qwen3-vl-flash"
        ).strip()

    async def _ensure_pdf_available(self, paper: Paper, *, user_id: int) -> Optional[Path]:
        existing = self._resolve_local_pdf_path(paper=paper, user_id=user_id)
        if existing:
            return existing

        candidates = self._build_pdf_download_candidates(paper)
        if not candidates:
            return None

        target_path = self._build_paper_pdf_file_path(paper=paper, user_id=user_id, ensure_dir=True)
        literature_service = get_literature_service()
        for candidate in candidates:
            success, error = await literature_service.download_pdf(candidate, str(target_path))
            if success:
                paper.pdf_path = str(target_path)
                paper.pdf_downloaded = True
                paper.pdf_url = candidate
                return target_path
            logger.info(f"[PaperExperiment] PDF candidate failed paper_id={paper.id}: {candidate} error={error}")
        return None

    def _resolve_local_pdf_path(self, *, paper: Paper, user_id: int) -> Optional[Path]:
        candidates: List[Path] = []
        if isinstance(paper.pdf_path, str) and paper.pdf_path.strip():
            candidates.append(Path(paper.pdf_path.strip()))
        default_path = self._build_paper_pdf_file_path(paper=paper, user_id=user_id, ensure_dir=False)
        if default_path not in candidates:
            candidates.append(default_path)
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _build_paper_pdf_file_path(*, paper: Paper, user_id: int, ensure_dir: bool) -> Path:
        upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
        pdf_dir = Path(upload_dir) / str(user_id) / "papers"
        if ensure_dir:
            pdf_dir.mkdir(parents=True, exist_ok=True)
        safe_title = "".join(c for c in str(paper.title or "")[:50] if c.isalnum() or c in " -_").strip()
        filename = f"{safe_title or f'paper_{paper.id}'}_{paper.id}.pdf"
        return pdf_dir / filename

    def _build_pdf_download_candidates(self, paper: Paper) -> List[str]:
        raw_data = self._as_dict(paper.raw_data)
        candidates: List[str] = []
        arxiv_id = self._extract_arxiv_id(
            paper.arxiv_id,
            paper.arxiv_url,
            paper.url,
            paper.doi,
            raw_data.get("imported_link"),
            raw_data.get("source_url"),
            raw_data.get("id"),
        )
        if arxiv_id:
            candidates.append(f"https://arxiv.org/pdf/{arxiv_id}")
        for item in (
            paper.pdf_url,
            raw_data.get("pdf_url"),
            raw_data.get("oa_url"),
            paper.url,
            paper.arxiv_url,
        ):
            value = str(item or "").strip()
            if value.lower().endswith(".pdf"):
                candidates.append(value)
        return self._merge_unique_strings([], candidates)

    @staticmethod
    def _extract_arxiv_id(*values: Any) -> Optional[str]:
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _resolve_paper_context_char_budget(*, provider: str, model: str) -> int:
        window = resolve_model_context_window(
            provider=provider,
            model_name=model,
            deepseek_test_alias=str(getattr(settings, "deepseek_test_model_alias", "deepseek-chat-test") or "deepseek-chat-test"),
            deepseek_test_window=max(int(getattr(settings, "deepseek_test_model_window", 4096) or 4096), 1024),
        ) or 64000
        reserved_tokens = max(_PAPER_INTAKE_OUTPUT_TOKENS + 2500, 9000)
        input_tokens = max(window - reserved_tokens, 12000)
        return max(60000, min(int(input_tokens * 3.4), _PAPER_MARKDOWN_STORE_MAX_CHARS))

    @staticmethod
    def _parse_json_object(raw_text: str) -> Dict[str, Any]:
        text = str(raw_text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("paper intake response is not a JSON object")
        return parsed

    def _resolve_codelab_run_templates(
        self,
        *,
        intake: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        templates: List[Dict[str, Any]] = []
        for item in self._as_list(intake.get("codelab_run_templates")):
            payload = self._as_dict(item)
            code = self._normalize_python_template(str(payload.get("python_code") or ""))
            target = str(payload.get("target") or "variant").strip().lower() or "variant"
            if target not in {"baseline", "variant", "sweep"}:
                target = "variant"
            if not code or self._template_has_forbidden_ops(code):
                continue
            templates.append(
                {
                    "title": str(payload.get("title") or f"{target.title()} Template").strip() or f"{target.title()} Template",
                    "target": target,
                    "description": str(payload.get("description") or "").strip(),
                    "python_code": code,
                    "template_origin": str(payload.get("template_origin") or "llm").strip() or "llm",
                }
            )
        return templates[:8]

    def _resolve_run_template_code(self, *, spec: Dict[str, Any], run: PaperExperimentRun) -> str:
        templates = [self._as_dict(item) for item in self._as_list(spec.get("codelab_run_templates"))]
        desired_target = "baseline" if str(run.run_kind or "").strip().lower() == "baseline" else "variant"
        model_name = str(run.model_name or "").strip().lower()

        ranked_templates: List[Dict[str, Any]] = []
        for payload in templates:
            target = str(payload.get("target") or "").strip().lower()
            score = 0
            if target == desired_target:
                score += 3
            if model_name:
                haystack = " ".join(
                    [
                        str(payload.get("title") or ""),
                        str(payload.get("description") or ""),
                        str(payload.get("python_code") or "")[:400],
                    ]
                ).lower()
                if model_name and model_name in haystack:
                    score += 2
            ranked_templates.append({"score": score, **payload})
        ranked_templates.sort(key=lambda item: int(item.get("score") or 0), reverse=True)

        for payload in ranked_templates:
            code = self._normalize_python_template(str(payload.get("python_code") or ""))
            if code and not self._template_has_forbidden_ops(code):
                return code

        return self._build_missing_run_template_guard()

    @staticmethod
    def _normalize_python_template(raw_code: str) -> str:
        text = str(raw_code or "").strip()
        fenced = re.search(r"```(?:python)?\s*([\s\S]+?)\s*```", text, flags=re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        return text

    def _template_has_forbidden_ops(self, code: str) -> bool:
        text = str(code or "").strip().lower()
        if not text:
            return True
        for token in _PAPER_TEMPLATE_FORBIDDEN_PATTERNS:
            if str(token).lower() in text:
                return True
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                module_name = stripped[len("import ") :].split(",")[0].strip().split()[0]
            elif stripped.startswith("from "):
                module_name = stripped[len("from ") :].split()[0].strip()
            else:
                continue
            root = module_name.split(".")[0]
            if root in SANDBOX_FORBIDDEN_IMPORT_ROOTS or root == "sys":
                return True
        return False

    @staticmethod
    def _build_missing_run_template_guard() -> str:
        return (
            "# No paper-backed executable template was extracted.\n"
            "run_artifacts['requires_manual_implementation'] = True\n"
            "run_artifacts['missing_template_reason'] = 'No safe paper-backed CodeLab template is available.'\n"
            "print('No safe paper-backed CodeLab template is available for this run draft.')\n"
            "print('Use the paper evidence, repo entrypoints, and dataset links above to write the implementation cell before executing.')\n"
            "raise RuntimeError('No safe paper-backed executable template is available. Prepare the implementation cell first.')\n"
        )

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        return list(value) if isinstance(value, list) else []

    @staticmethod
    def _first_string(values: Iterable[Any]) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _normalize_safe_knobs(self, value: Any) -> List[Dict[str, Any]]:
        knobs: List[Dict[str, Any]] = []
        for item in self._as_list(value):
            payload = self._as_dict(item)
            key = str(payload.get("key") or "").strip()
            if not key:
                continue
            kind = str(payload.get("kind") or "string").strip()
            if kind not in {"number", "integer", "string", "choice"}:
                kind = "string"
            knobs.append(
                {
                    "key": key,
                    "label": str(payload.get("label") or key).strip() or key,
                    "kind": kind,
                    "default": payload.get("default"),
                    "suggested_values": list(payload.get("suggested_values") or [])[:6],
                    "min": payload.get("min"),
                    "max": payload.get("max"),
                    "reason": payload.get("reason"),
                    "risk": payload.get("risk"),
                    **({"evidence_text": payload.get("evidence_text")} if payload.get("evidence_text") else {}),
                }
            )
        return knobs

    def _first_entrypoint_hint(self, hints: List[Any]) -> Dict[str, Any]:
        for item in hints:
            payload = self._as_dict(item)
            if payload.get("value") or payload.get("evidence_text"):
                return payload
        return {}

    def _collect_urls_from_intake(
        self,
        intake: Dict[str, Any],
        *,
        keys: Iterable[str],
        require_github: bool,
    ) -> List[str]:
        urls: List[str] = []
        for key in keys:
            for item in self._as_list(self._as_dict(intake).get(key)):
                payload = self._as_dict(item)
                url = str(payload.get("url") or "").strip()
                if not url:
                    continue
                if require_github and "github.com/" not in url.lower():
                    continue
                urls.append(url)
        return self._merge_unique_strings([], urls)

    @staticmethod
    def _merge_unique_strings(*groups: Iterable[Any]) -> List[str]:
        merged: List[str] = []
        seen: set[str] = set()
        for group in groups:
            for item in group or []:
                text = str(item or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                merged.append(text)
        return merged

    def _build_compare_report(self, runs: Iterable[PaperExperimentRun]) -> Dict[str, Any]:
        run_list = list(runs)
        completed_runs = [run for run in run_list if str(run.status or "").lower() == "completed"]
        baseline = next((run for run in run_list if str(run.run_kind or "") == "baseline"), None)
        ranking = self._rank_runs(completed_runs)
        best_run = ranking[0] if ranking else None
        ranking_metric = ranking[0][1] if ranking else None
        ranking_direction = ranking[0][2] if ranking else None

        baseline_delta = None
        if best_run and baseline and baseline.id != best_run[0].id:
            metric_name = ranking_metric
            baseline_value = self._coerce_number((baseline.metrics_json or {}).get(metric_name)) if metric_name else None
            best_value = self._coerce_number((best_run[0].metrics_json or {}).get(metric_name)) if metric_name else None
            if baseline_value is not None and best_value is not None:
                baseline_delta = {
                    "metric": metric_name,
                    "baseline": baseline_value,
                    "best": best_value,
                    "delta": round(best_value - baseline_value, 6),
                }

        return {
            "total_runs": len(run_list),
            "completed_runs": len(completed_runs),
            "best_run_id": best_run[0].id if best_run else None,
            "ranking_metric": ranking_metric,
            "ranking_direction": ranking_direction,
            "baseline_delta": baseline_delta,
            "insight": self._build_compare_insight(best_run[0] if best_run else None, ranking_metric, baseline_delta),
        }

    def _rank_runs(self, runs: List[PaperExperimentRun]) -> List[tuple[PaperExperimentRun, str, str]]:
        ranked: List[tuple[PaperExperimentRun, str, str]] = []
        for run in runs:
            metric_name, direction, metric_value = self._pick_primary_metric(run.metrics_json or {})
            if metric_name and metric_value is not None:
                ranked.append((run, metric_name, direction))

        def sort_key(item: tuple[PaperExperimentRun, str, str]) -> float:
            run, metric_name, direction = item
            value = self._coerce_number((run.metrics_json or {}).get(metric_name))
            if value is None:
                return float("-inf")
            return value if direction == "max" else -value

        return sorted(ranked, key=sort_key, reverse=True)

    def _build_compare_insight(
        self,
        best_run: Optional[PaperExperimentRun],
        metric_name: Optional[str],
        baseline_delta: Optional[Dict[str, Any]],
    ) -> str:
        if not best_run or not metric_name:
            return "No completed run has recorded metrics yet."
        if baseline_delta is None:
            return f"Current best run is {best_run.label} on {metric_name}."
        delta = baseline_delta.get("delta")
        return f"Best run is {best_run.label}; {metric_name} changed by {delta} versus baseline."

    def _pick_primary_metric(self, metrics: Dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[float]]:
        for key, value in metrics.items():
            number = self._coerce_number(value)
            if number is None:
                continue
            normalized = str(key or "").strip().lower()
            if any(token in normalized for token in _HIGHER_IS_BETTER_TOKENS):
                return key, "max", number
            if any(token in normalized for token in _LOWER_IS_BETTER_TOKENS):
                return key, "min", number
        for key, value in metrics.items():
            number = self._coerce_number(value)
            if number is not None:
                return key, "max", number
        return None, None, None

    @staticmethod
    def _coerce_number(value: Any) -> Optional[float]:
        try:
            if isinstance(value, bool):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _collect_matching_urls(self, payload: Any, pattern: re.Pattern[str]) -> List[str]:
        matches: List[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, str):
                for item in pattern.findall(value):
                    token = str(item).strip()
                    if token and token not in matches:
                        matches.append(token)
                return
            if isinstance(value, dict):
                for nested in value.values():
                    visit(nested)
                return
            if isinstance(value, (list, tuple, set)):
                for nested in value:
                    visit(nested)

        visit(payload or {})
        return matches[:8]

    @staticmethod
    def _unique_runs(runs: List[PaperExperimentRun]) -> List[PaperExperimentRun]:
        ordered: List[PaperExperimentRun] = []
        seen: set[int] = set()
        for run in runs:
            run_id = int(getattr(run, "id", 0) or 0)
            if run_id and run_id in seen:
                continue
            if run_id:
                seen.add(run_id)
            ordered.append(run)
        return ordered
