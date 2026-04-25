from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config import settings
from app.models.literature import Paper
from app.services.dashscope_multimodal_service import DashScopeMultimodalService
from app.services.literature_service import get_literature_service
from app.services.llm_service import LLMService
from app.services.online_mm_ingest_service import OnlineMmIngestService
from app.services.pdf_rag_ingest_service import PdfRagIngestService


_PAPER_INTAKE_OUTPUT_TOKENS = 8192
_PAPER_INTAKE_TIMEOUT_SECONDS = 600
_RAW_DATA_CONTEXT_MAX_CHARS = 24000

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


class PaperIntakeService:
    def __init__(self) -> None:
        self.pdf_ingest_service = PdfRagIngestService()

    async def build_intake(self, *, paper: Paper, user_id: int) -> Dict[str, Any]:
        payload = await self._build_paper_intake_payload(paper, user_id=user_id)
        paper_markdown = str(payload.get("stored_paper_markdown") or payload.get("paper_markdown") or "")
        paper_intake = await self._extract_paper_intake_json(payload)
        return {
            "paper_markdown": paper_markdown,
            "paper_intake": dict(paper_intake or {}),
            "intake_metadata": {
                "source_mode": payload.get("source_mode"),
                "extractor": payload.get("extractor"),
                "page_count": int(payload.get("page_count") or 0),
                "total_chars": int(payload.get("total_chars") or 0),
                "sent_chars": int(payload.get("sent_chars") or 0),
                "truncated": bool(payload.get("truncated")),
            },
        }

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
            except Exception as exc:
                logger.warning(f"[PaperIntake] count PDF pages failed paper_id={paper.id}: {exc}")
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
            except Exception as exc:
                logger.warning(f"[PaperIntake] local PDF markdown extraction failed paper_id={paper.id}: {exc}")
            if not paper_markdown.strip() and self._paper_intake_multimodal_ready():
                source_mode = "local_pdf_page_images"
                extractor_name = "dashscope_multimodal_pages"

        if not paper_markdown.strip():
            paper_markdown = str(getattr(paper, "abstract", "") or "")
            if source_mode != "local_pdf_page_images":
                extractor_name = "abstract_fallback"
                source_mode = "metadata_abstract_fallback"

        provider = str(
            getattr(settings, "paper_intake_provider", "")
            or getattr(settings, "default_llm_provider", "deepseek")
            or "deepseek"
        )
        model = str((settings.get_llm_config(provider) or {}).get("model") or "")
        original_total_chars = len(paper_markdown)
        raw_data_text = json.dumps(getattr(paper, "raw_data", {}) or {}, ensure_ascii=False, indent=2, default=str)
        raw_data_text = raw_data_text[:_RAW_DATA_CONTEXT_MAX_CHARS]
        metadata = {
            "id": paper.id,
            "title": getattr(paper, "title", None),
            "abstract": getattr(paper, "abstract", None),
            "authors": getattr(paper, "authors", None) or [],
            "year": getattr(paper, "year", None),
            "venue": getattr(paper, "venue", None),
            "journal": getattr(paper, "journal", None),
            "arxiv_id": getattr(paper, "arxiv_id", None),
            "doi": getattr(paper, "doi", None),
            "url": getattr(paper, "url", None),
            "pdf_url": getattr(paper, "pdf_url", None),
            "arxiv_url": getattr(paper, "arxiv_url", None),
            "fields_of_study": getattr(paper, "fields_of_study", None) or [],
        }
        text_hash = hashlib.sha256(paper_markdown.encode("utf-8", errors="ignore")).hexdigest() if paper_markdown else ""
        return {
            "metadata": metadata,
            "raw_data_text": raw_data_text,
            "paper_markdown": paper_markdown,
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
            "sent_chars": len(paper_markdown),
            "truncated": False,
            "store_truncated": False,
            "llm_truncated": False,
            "sha256": text_hash,
        }

    async def _extract_paper_intake_json(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        source_mode = str(payload.get("source_mode") or "").strip()
        if source_mode == "local_pdf_page_images" and str(payload.get("pdf_path") or "").strip():
            try:
                return await self._extract_paper_intake_json_from_pdf_images(payload)
            except Exception as exc:
                logger.warning(f"[PaperIntake] multimodal intake failed, fallback to text path: {exc}")
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
            logger.info(f"[PaperIntake] PDF candidate failed paper_id={paper.id}: {candidate} error={error}")
        return None

    def _resolve_local_pdf_path(self, *, paper: Paper, user_id: int) -> Optional[Path]:
        candidates: List[Path] = []
        if isinstance(getattr(paper, "pdf_path", None), str) and str(paper.pdf_path).strip():
            candidates.append(Path(str(paper.pdf_path).strip()))
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
        safe_title = "".join(c for c in str(getattr(paper, "title", "") or "")[:50] if c.isalnum() or c in " -_").strip()
        filename = f"{safe_title or f'paper_{paper.id}'}_{paper.id}.pdf"
        return pdf_dir / filename

    def _build_pdf_download_candidates(self, paper: Paper) -> List[str]:
        raw_data = getattr(paper, "raw_data", {}) or {}
        if not isinstance(raw_data, dict):
            raw_data = {}
        candidates: List[str] = []
        arxiv_id = self._extract_arxiv_id(
            getattr(paper, "arxiv_id", None),
            getattr(paper, "arxiv_url", None),
            getattr(paper, "url", None),
            getattr(paper, "doi", None),
            raw_data.get("imported_link"),
            raw_data.get("source_url"),
            raw_data.get("id"),
        )
        if arxiv_id:
            candidates.append(f"https://arxiv.org/pdf/{arxiv_id}")
        for item in (
            getattr(paper, "pdf_url", None),
            raw_data.get("pdf_url"),
            raw_data.get("oa_url"),
            getattr(paper, "url", None),
            getattr(paper, "arxiv_url", None),
        ):
            value = str(item or "").strip()
            if value.lower().endswith(".pdf"):
                candidates.append(value)
        return self._merge_unique_strings(candidates)

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

    @staticmethod
    def _merge_unique_strings(values: List[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for item in values:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
        return merged
