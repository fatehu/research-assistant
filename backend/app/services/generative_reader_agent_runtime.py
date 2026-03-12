"""
Generative reader agent runtime.

Builds a structured generative plan on top of an existing reader compose payload.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger
from pydantic import ValidationError

from app.config import settings
from app.schemas.literature import ReaderExperiencePlan, ReaderGenerativePlan
from app.services.generative_reader_agent_core import GenerativeReaderAgentCore
from app.services.generative_reader_agent_tools import (
    build_generative_reader_tool_registry,
    resolve_generative_reader_agent_tool_whitelist,
)
from app.services.llm_service import get_llm_service
from app.services.react_agent import AgentRuntimeContext


class GenerativeReaderAgentRuntime:
    READER_NATIVE_TOOLS = ("paper_read", "knowledge_search")
    HIGH_VALUE_RESOURCE_DOMAINS = (
        "usmle.org",
        "pubmed.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
        "nih.gov",
        "nature.com",
        "plos.org",
        "jamanetwork.com",
        "nejm.org",
        "bmj.com",
        "who.int",
        "fda.gov",
        "mededu.jmir.org",
        "jmir.org",
    )
    LOW_VALUE_RESOURCE_DOMAINS = (
        "usmlestrike.com",
        "academically.com",
        "medium.com",
        "blogspot.com",
        "wordpress.com",
        "wikipedia.org",
    )
    READING_PATH_SECTION_MAP = {
        "hero_summary": "hero",
        "focus_evidence": "focus_stage",
        "focus_figure": "focus_stage",
        "figure_focus": "focus_stage",
        "key_finding": "reading_flow",
        "reading_flow": "reading_flow",
        "context_explainer": "explainer_cluster",
        "glossary": "explainer_cluster",
        "supporting_resources": "supporting_resources",
        "open_supporting_resources": "supporting_resources",
        "resource_context": "supporting_resources",
        "explore_questions": "question_lab",
        "question_lab": "question_lab",
        "story_map": "story_map",
    }
    EXPERIENCE_LAYOUT_VARIANTS = (
        "focus_figure_split",
        "guided_story_stack",
        "explainer_first",
        "resource_augmented_reader",
    )
    EXPERIENCE_SECTION_TYPES = (
        "hero",
        "focus_stage",
        "reading_flow",
        "explainer_cluster",
        "supporting_resources",
        "question_lab",
        "story_map",
    )
    GENERIC_COPY_REWRITES = {
        "what fig 3 reveals": "Fig 3 说明了什么",
        "decode the figure": "拆解这张图",
        "read the supporting passage": "阅读支撑正文",
        "push the idea further": "继续往下追问",
        "understand the metrics": "读懂关键指标",
        "behind the page": "页面幕后",
        "figure overview": "整图概览",
        "primary figure": "主图",
        "section context": "章节上下文",
        "related resources": "延伸资源",
        "glossary and background": "术语与背景",
        "glossary": "术语解释",
        "suggested follow-up questions": "接下来值得追问的问题",
        "figure exploration": "图示探索",
        "figure explainer": "图示解读",
        "public resource": "公开资源",
        "scraped source": "抓取来源",
        "reading context": "阅读背景",
        "usmle structure": "USMLE 结构",
        "evaluation metrics": "评估指标",
        "reader may lack context": "读者可能缺少必要背景。",
    }

    @staticmethod
    def _clean_excerpt(text: str, limit: int = 220) -> str:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if len(clean) <= limit:
            return clean
        trimmed = clean[:limit]
        if " " in trimmed:
            trimmed = trimmed.rsplit(" ", 1)[0]
        return trimmed.strip()

    @classmethod
    def _prefer_display_copy(cls, raw: str, fallback: str = "", *, limit: int = 220) -> str:
        primary = cls._clean_excerpt(raw, limit=limit)
        backup = cls._clean_excerpt(fallback, limit=limit)
        if primary and not cls._needs_display_localization(primary, short_form=limit <= 140):
            return primary
        return backup or primary

    @staticmethod
    def _needs_display_localization(text: str, *, short_form: bool = False) -> bool:
        token = str(text or "").strip()
        if not token:
            return False
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", token))
        latin_count = len(re.findall(r"[A-Za-z]", token))
        if cjk_count > 0:
            return False
        if short_form:
            return latin_count >= 6
        return latin_count >= 24 and latin_count > cjk_count * 4

    @staticmethod
    def _resolve_target_display_label(target: Mapping[str, Any]) -> str:
        return str(
            target.get("figure_label")
            or target.get("title")
            or target.get("section_label")
            or target.get("target_id")
            or ""
        ).strip()

    @classmethod
    def _derive_claim_display_text(
        cls,
        claim: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> str:
        target_ids = [str(item or "").strip() for item in list(claim.get("source_target_ids") or []) if str(item or "").strip()]
        primary_target = dict(target_map.get(target_ids[0]) or {}) if target_ids else {}
        target_kind = str(primary_target.get("target_kind") or "").strip()
        target_label = cls._resolve_target_display_label(primary_target)
        section_label = str(primary_target.get("section_label") or "").strip()
        if target_kind in {"figure", "table"}:
            return f"{target_label or '主图'}承载了这一页最值得先看的关键结果。"
        if section_label and not cls._needs_display_localization(section_label, short_form=True):
            return f"{section_label} 段落给出了这一页的重要结论，建议结合原文逐句核对。"
        return "这一段正文包含本页的重要结论，建议结合原文逐句核对。"

    @classmethod
    def _materialize_story_display_copy(
        cls,
        story_substrate: Mapping[str, Any],
        *,
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        materialized = dict(story_substrate or {})
        claims: List[Dict[str, Any]] = []
        for row in list(materialized.get("main_claims") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            item["display_text"] = cls._prefer_display_copy(
                str(item.get("display_text") or item.get("text") or ""),
                cls._derive_claim_display_text(item, target_map),
            )
            claims.append(item)
        materialized["main_claims"] = claims
        return materialized

    @classmethod
    def _derive_resource_display_title(
        cls,
        module: Mapping[str, Any],
        *,
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> str:
        target_ids = [str(item or "").strip() for item in list(module.get("target_ids") or []) if str(item or "").strip()]
        primary_target = dict(target_map.get(target_ids[0]) or {}) if target_ids else {}
        focus_label = cls._resolve_target_display_label(primary_target)
        section_label = str(primary_target.get("section_label") or "").strip()
        module_type = str(module.get("module_type") or "").strip()
        if module_type == "FigureExplainPanel":
            return f"如何阅读 {focus_label}" if focus_label else "如何阅读关键图示"
        if module_type == "RelatedResourceCard":
            if section_label and not cls._needs_display_localization(section_label, short_form=True):
                return f"{section_label} 的背景补充"
            return "这一段的背景补充"
        return "增强资源"

    @classmethod
    def _derive_resource_display_summary(
        cls,
        module: Mapping[str, Any],
        *,
        page_brief: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> str:
        target_ids = [str(item or "").strip() for item in list(module.get("target_ids") or []) if str(item or "").strip()]
        primary_target = dict(target_map.get(target_ids[0]) or {}) if target_ids else {}
        focus_label = cls._resolve_target_display_label(primary_target)
        module_type = str(module.get("module_type") or "").strip()
        resource_strategy = str(page_brief.get("resource_strategy") or "").strip()
        if module_type == "FigureExplainPanel":
            return f"把 {focus_label or '这张图'} 当成主要视觉锚点，再回到正文核对证据。"
        if resource_strategy and not cls._needs_display_localization(resource_strategy):
            return resource_strategy
        return "补充少量高相关的外部资源，帮助理解正文，而不是替代正文。"

    @classmethod
    def _materialize_resource_display_copy(
        cls,
        modules: Sequence[Mapping[str, Any]],
        *,
        page_brief: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        materialized: List[Dict[str, Any]] = []
        for row in list(modules or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            item["display_title"] = cls._prefer_display_copy(
                str(item.get("display_title") or item.get("title") or ""),
                cls._derive_resource_display_title(item, target_map=target_map),
                limit=120,
            )
            item["display_summary"] = cls._prefer_display_copy(
                str(item.get("display_summary") or item.get("summary") or ""),
                cls._derive_resource_display_summary(item, page_brief=page_brief, target_map=target_map),
                limit=240,
            )
            materialized.append(item)
        return materialized

    @classmethod
    def _derive_interaction_display_title(
        cls,
        module: Mapping[str, Any],
        *,
        page_brief: Mapping[str, Any],
    ) -> str:
        module_type = str(module.get("module_type") or "").strip()
        archetype = str(page_brief.get("page_archetype") or "").strip()
        if module_type == "GlossaryPanel":
            return "把方法术语讲明白" if archetype == "methods_decoder" else "读懂这一页的关键术语"
        if module_type == "QuestionStarterPanel":
            return "接下来值得追问的问题"
        return "交互模块"

    @staticmethod
    def _derive_interaction_display_summary(module: Mapping[str, Any]) -> str:
        module_type = str(module.get("module_type") or "").strip()
        if module_type == "GlossaryPanel":
            return "先把阻碍理解的术语讲清楚，再回到正文继续读。"
        if module_type == "QuestionStarterPanel":
            return "把当前理解转成追问，检查你是否真的读懂了这一页。"
        return "这个模块会在需要时补一层互动式解释。"

    @classmethod
    def _materialize_interaction_display_copy(
        cls,
        modules: Sequence[Mapping[str, Any]],
        *,
        page_brief: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        materialized: List[Dict[str, Any]] = []
        for row in list(modules or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            item["display_title"] = cls._prefer_display_copy(
                str(item.get("display_title") or item.get("title") or ""),
                cls._derive_interaction_display_title(item, page_brief=page_brief),
                limit=120,
            )
            item["display_summary"] = cls._prefer_display_copy(
                str(item.get("display_summary") or ""),
                cls._derive_interaction_display_summary(item),
                limit=180,
            )
            materialized.append(item)
        return materialized

    @classmethod
    def _materialize_widget_display_copy(
        cls,
        widgets: Sequence[Mapping[str, Any]],
        *,
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        materialized: List[Dict[str, Any]] = []
        for row in list(widgets or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            target_ids = [str(token or "").strip() for token in list(item.get("target_ids") or []) if str(token or "").strip()]
            primary_target = dict(target_map.get(target_ids[0]) or {}) if target_ids else {}
            focus_label = cls._resolve_target_display_label(primary_target)
            widget_type = str(item.get("widget_type") or "").strip()
            widget_title_fallback = "逐面板理解这张图" if widget_type == "figure-focus-accordion" else "交互探索"
            if focus_label and widget_type == "figure-focus-accordion":
                widget_title_fallback = f"逐面板理解 {focus_label}"
            item["display_title"] = cls._prefer_display_copy(
                str(item.get("display_title") or item.get("title") or ""),
                widget_title_fallback,
                limit=120,
            )
            item["display_summary"] = cls._prefer_display_copy(
                str(item.get("display_summary") or ""),
                "按面板拆开看主图，理解每个部分分别为结果贡献了什么。",
                limit=180,
            )
            props = dict(item.get("props") or {})
            panels = []
            for panel in list(props.get("panels") or []):
                if not isinstance(panel, Mapping):
                    continue
                panel_item = dict(panel)
                label = str(panel_item.get("label") or "").strip()
                focus = str(panel_item.get("focus") or "").strip()
                summary = str(panel_item.get("summary") or "").strip()
                panel_item["display_label"] = re.sub(r"^Panel\s+([A-Z])$", r"分面 \1", label, flags=re.IGNORECASE) if label else ""
                panel_item["display_summary"] = cls._prefer_display_copy(
                    str(panel_item.get("display_summary") or summary),
                    cls._describe_figure_focus(focus, label),
                    limit=220,
                )
                panels.append(panel_item)
            if panels:
                props["panels"] = panels
                item["props"] = props
            materialized.append(item)
        return materialized

    @classmethod
    def _is_fragment_like_excerpt(cls, text: str) -> bool:
        clean = cls._clean_excerpt(text, limit=320)
        if not clean:
            return True
        if re.match(r"^[a-z]", clean):
            return True
        if clean.count(" ") < 4:
            return True
        if re.search(r"\b(?:as|and|or|but|with|for|to|of)\b", clean[:36].lower()) and not re.search(r"[。！？.!?]", clean):
            return True
        return False

    @classmethod
    def _resolve_section_sequence(
        cls,
        reading_path: Sequence[str],
        available_section_types: Sequence[str],
    ) -> List[str]:
        available = [str(item or "").strip() for item in list(available_section_types or []) if str(item or "").strip()]
        if not available:
            return []
        ordered: List[str] = []
        for token in list(reading_path or []):
            section_type = cls.READING_PATH_SECTION_MAP.get(str(token or "").strip().lower())
            if section_type and section_type in available and section_type not in ordered:
                ordered.append(section_type)
        for fallback in [
            "hero",
            "focus_stage",
            "reading_flow",
            "explainer_cluster",
            "supporting_resources",
            "question_lab",
            "story_map",
        ]:
            if fallback in available and fallback not in ordered:
                ordered.append(fallback)
        return ordered

    @classmethod
    def _extract_hostname(cls, href: str) -> str:
        token = str(href or "").strip()
        if not token:
            return ""
        try:
            parsed = urlparse(token if "://" in token else f"https://{token}")
        except Exception:
            return ""
        host = str(parsed.hostname or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host

    @classmethod
    def _resource_domain_score(cls, href: str) -> int:
        host = cls._extract_hostname(href)
        if not host:
            return 0
        for domain in cls.HIGH_VALUE_RESOURCE_DOMAINS:
            if host == domain or host.endswith(f".{domain}"):
                return 100
        for domain in cls.LOW_VALUE_RESOURCE_DOMAINS:
            if host == domain or host.endswith(f".{domain}"):
                return -40
        if host.endswith(".gov") or host.endswith(".edu"):
            return 80
        if host.endswith(".org"):
            return 50
        return 20

    @classmethod
    def _normalize_public_links(cls, links: Sequence[Mapping[str, Any]], limit: int = 3) -> List[Dict[str, str]]:
        ranked: List[tuple[int, str, Dict[str, str]]] = []
        seen: set[str] = set()
        for row in list(links or []):
            if not isinstance(row, Mapping):
                continue
            href = str(row.get("href") or "").strip()
            if not href or href in seen:
                continue
            host = cls._extract_hostname(href)
            if not host:
                continue
            seen.add(href)
            label = str(row.get("label") or host).strip()[:140] or host
            normalized: Dict[str, str] = {
                "label": label,
                "href": href,
                "domain": host,
            }
            snippet = str(row.get("snippet") or row.get("summary") or "").strip()
            if snippet:
                normalized["snippet"] = cls._clean_excerpt(snippet, limit=180)
            ranked.append((cls._resource_domain_score(href), host, normalized))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            return []

        positive = [row for row in ranked if row[0] > 0]
        strong = [row for row in ranked if row[0] >= 50]
        candidate_rows = strong or positive or ranked

        deduped: List[Dict[str, str]] = []
        seen_domains: set[str] = set()
        for score, host, row in candidate_rows:
            if score < 0 and (strong or positive):
                continue
            domain_key = host or row.get("domain") or row.get("href") or ""
            if domain_key in seen_domains:
                continue
            seen_domains.add(domain_key)
            deduped.append(row)
            if len(deduped) >= limit:
                break
        return deduped

    @staticmethod
    def _classify_failure_reason(error_message: str) -> str:
        text = str(error_message or "").strip().lower()
        if not text:
            return "agent_error"
        if "model_not_found" in text or "does not exist" in text or "do not have access" in text:
            return "model_not_found"
        if "timeout" in text:
            return "agent_timeout"
        if "rate limit" in text or "429" in text:
            return "rate_limited"
        return "agent_error"

    @staticmethod
    async def _build_llm() -> Any:
        provider = str(
            getattr(settings, "generative_reader_agent_provider", "")
            or getattr(settings, "reader_agent_provider", "")
            or getattr(settings, "default_llm_provider", "deepseek")
            or "deepseek"
        ).strip()
        llm = await get_llm_service(provider=provider)
        preferred_model = str(
            getattr(settings, "generative_reader_agent_model", "")
            or getattr(settings, "reader_agent_model", "")
            or ""
        ).strip()
        if preferred_model:
            llm.config = dict(getattr(llm, "config", {}) or {})
            llm.config["model"] = preferred_model
        return llm

    @staticmethod
    def _extract_json_dict(raw: str) -> Optional[Dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _compact_tool_trace_for_recovery(tool_trace: Sequence[Mapping[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in list(tool_trace or [])[:limit]:
            if not isinstance(row, Mapping):
                continue
            trace_type = str(row.get("type") or "").strip()
            data = row.get("data")
            if not isinstance(data, Mapping):
                continue
            tool_name = str(data.get("tool") or "").strip()
            entry: Dict[str, Any] = {"type": trace_type, "tool": tool_name}
            if isinstance(data.get("input"), Mapping):
                entry["input"] = dict(data.get("input") or {})
            if trace_type == "observation":
                entry["success"] = bool(data.get("success"))
                output = str(data.get("output") or "").strip()
                if output:
                    entry["output_excerpt"] = output[:500]
            rows.append(entry)
        return rows

    def _build_timeout_recovery_prompt(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        tool_trace: Sequence[Mapping[str, Any]],
    ) -> str:
        compact_targets = self._compact_enrichment_targets(enrichment_bundle, limit=12)
        compact_trace = self._compact_tool_trace_for_recovery(tool_trace)
        return (
            "Recover a generative reader plan after a long-running agent timed out.\n"
            "Return JSON only.\n"
            "Do not call tools. Use only the provided target list and tool observations.\n"
            "Keep the output compact: at most 2 resource modules, 2 interaction modules, and 1 JS widget.\n"
            "If web scrape/search evidence is weak, keep links conservative and say so in meta.notes.\n"
            "Do not rewrite paper body content.\n"
            f"page={int(page)}\n"
            f"user_intent={json.dumps(str(user_intent or '').strip(), ensure_ascii=False)}\n"
            f"enrichment_targets={json.dumps(compact_targets, ensure_ascii=False)}\n"
            f"tool_observations={json.dumps(compact_trace, ensure_ascii=False)}\n"
        )

    async def _recover_plan_from_tool_trace(
        self,
        *,
        llm: Any,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        tool_trace: Sequence[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        compact_trace = self._compact_tool_trace_for_recovery(tool_trace)
        if not compact_trace:
            return None
        try:
            resp = await asyncio.wait_for(
                llm.chat(
                    messages=[{"role": "user", "content": self._build_timeout_recovery_prompt(
                        page=page,
                        user_intent=user_intent,
                        enrichment_bundle=enrichment_bundle,
                        tool_trace=tool_trace,
                    )}],
                    system_prompt=(
                        "You are a recovery formatter for generative reader plans. "
                        "Use only supplied observations. Return valid JSON."
                    ),
                    temperature=0.1,
                    max_tokens=min(int(getattr(settings, "llm_max_tokens", 4096) or 4096), 1400),
                ),
                timeout=30.0,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[GenerativeReaderAgentRuntime] timeout recovery failed: {exc}")
            return None
        return self._extract_json_dict(str((resp or {}).get("content") or ""))

    @staticmethod
    def _compact_enrichment_targets(enrichment_bundle: Mapping[str, Any], limit: int = 48) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in list(enrichment_bundle.get("targets") or [])[:limit]:
            if not isinstance(row, Mapping):
                continue
            rows.append(
                {
                    "target_id": str(row.get("target_id") or "").strip(),
                    "node_id": str(row.get("node_id") or "").strip(),
                    "target_kind": str(row.get("target_kind") or "").strip(),
                    "component_type": str(row.get("component_type") or "").strip(),
                    "title": str(row.get("title") or "").strip(),
                    "excerpt": str(row.get("excerpt") or "").strip()[:280],
                    "section_label": str(row.get("section_label") or "").strip(),
                    "figure_label": str(row.get("figure_label") or "").strip(),
                    "suggested_resource_types": [
                        str(item).strip()
                        for item in list(row.get("suggested_resource_types") or [])[:6]
                        if str(item).strip()
                    ],
                }
            )
        return rows

    @staticmethod
    def _derive_reader_grounding_hints(enrichment_bundle: Mapping[str, Any], limit: int = 6) -> List[Dict[str, str]]:
        rows = GenerativeReaderAgentRuntime._compact_enrichment_targets(enrichment_bundle, limit=limit)
        hints: List[Dict[str, str]] = []
        for row in rows:
            target_id = str(row.get("target_id") or "").strip()
            if not target_id:
                continue
            title = str(row.get("title") or "").strip()
            excerpt = str(row.get("excerpt") or "").strip()
            figure_label = str(row.get("figure_label") or "").strip()
            section_label = str(row.get("section_label") or "").strip()
            query_seed = figure_label or title or excerpt
            query_seed = re.sub(r"\s+", " ", query_seed).strip()
            if len(query_seed) > 180:
                query_seed = query_seed[:180].rsplit(" ", 1)[0].strip()
            knowledge_seed = re.sub(r"[\[\]\(\),.;:]+", " ", f"{section_label} {query_seed}").strip()
            knowledge_seed = re.sub(r"\s+", " ", knowledge_seed)
            hints.append(
                {
                    "target_id": target_id,
                    "paper_read_query": query_seed,
                    "knowledge_search_query": knowledge_seed,
                }
            )
        return hints

    def _build_fallback_plan(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        targets = [row for row in list(enrichment_bundle.get("targets") or []) if isinstance(row, Mapping)]
        figure_target = next((row for row in targets if str(row.get("target_kind") or "") == "figure"), None)
        paragraph_target = next((row for row in targets if str(row.get("target_kind") or "") == "paragraph"), None)
        section_target = next((row for row in targets if str(row.get("target_kind") or "") == "section"), None)

        resource_modules: List[Dict[str, Any]] = []
        interaction_modules: List[Dict[str, Any]] = []
        js_widgets: List[Dict[str, Any]] = []
        rationale: List[str] = [
            "复用清洗后的正文阅读流作为主画布。",
            "围绕信息量最高的正文目标补充少量外部公开资源和交互。",
        ]

        if figure_target:
            resource_modules.append(
                {
                    "module_id": f"res_fig_{page}_1",
                    "module_type": "FigureExplainPanel",
                    "target_ids": [str(figure_target.get("target_id") or "")],
                    "title": str(figure_target.get("figure_label") or "图示解读").strip() or "图示解读",
                    "summary": "先解释这张图展示了什么，再把它和正文分析、外部背景资源连起来。",
                    "links": [],
                    "source": "fallback",
                    "interaction_mode": "expandable_sidecar",
                    "meta": {"priority": "high"},
                }
            )
            js_widgets.append(
                {
                    "widget_id": f"widget_fig_{page}_1",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": [str(figure_target.get("target_id") or "")],
                    "title": "图示探索",
                    "data_requirements": ["figure_explainer", "related_public_resource"],
                    "props": {
                        "collapsed": False,
                        "panels": [
                            {
                                "label": "整图概览",
                                "focus": "figure_overview",
                                "summary": "先整体把握这张图，再只在原始图注有充分依据时展开子面板细节。",
                            },
                        ],
                    },
                    "meta": {"priority": "high"},
                }
            )

        if paragraph_target:
            resource_modules.append(
                {
                    "module_id": f"res_para_{page}_1",
                    "module_type": "RelatedResourceCard",
                    "target_ids": [str(paragraph_target.get("target_id") or "")],
                    "title": str(paragraph_target.get("section_label") or "延伸资源").strip() or "延伸资源",
                    "summary": "补充少量与这段正文直接相关的公开参考资料或背景材料。",
                    "links": [],
                    "source": "fallback",
                    "interaction_mode": "stacked_cards",
                    "meta": {"priority": "medium"},
                }
            )
            glossary_terms = self._derive_glossary_terms(paragraph_target, figure_target)
            interaction_modules.append(
                {
                    "module_id": f"int_para_{page}_1",
                    "module_type": "GlossaryPanel",
                    "target_ids": [str(paragraph_target.get("target_id") or "")],
                    "title": "术语与背景",
                    "props": {"terms": glossary_terms},
                    "source": "fallback",
                    "meta": {"priority": "medium"},
                }
            )

        if section_target:
            question_items = self._derive_question_starters(
                paragraph_target=paragraph_target,
                figure_target=figure_target,
                section_target=section_target,
                user_intent=user_intent,
            )
            interaction_modules.append(
                {
                    "module_id": f"int_sec_{page}_1",
                    "module_type": "QuestionStarterPanel",
                    "target_ids": [str(section_target.get("target_id") or "")],
                    "title": "接下来值得追问的问题",
                    "props": {"questions": question_items},
                    "source": "fallback",
                    "meta": {"priority": "low"},
                }
            )

        story_substrate = self._build_story_substrate(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        page_brief = self._build_page_brief(
            page=int(page),
            user_intent=user_intent,
            story_substrate=story_substrate,
            enrichment_bundle=enrichment_bundle,
        )
        for module in interaction_modules:
            if not isinstance(module, dict):
                continue
            if str(module.get("module_type") or "").strip() != "QuestionStarterPanel":
                continue
            props = dict(module.get("props") or {})
            questions = [str(entry or "").strip() for entry in list(props.get("questions") or []) if str(entry or "").strip()]
            if questions and not list(props.get("qa_pairs") or []):
                props["qa_pairs"] = self._build_question_answer_pairs(
                    questions=questions,
                    page_brief=page_brief,
                    story_substrate=story_substrate,
                )
                module["props"] = props

        return {
            "version": "v1",
            "status": "fallback",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": story_substrate,
            "page_brief": page_brief,
            "rationale": rationale,
            "resource_modules": resource_modules,
            "interaction_modules": interaction_modules,
            "js_widgets": js_widgets,
            "used_tools": [],
            "tool_trace": [],
            "meta": {
                "page": int(page),
                "user_intent": str(user_intent or "").strip(),
                "fallback_reason": "agent_not_run",
                "target_count": len(targets),
            },
        }

    @staticmethod
    def _has_meaningful_modules(plan: Mapping[str, Any]) -> bool:
        return bool(
            list(plan.get("resource_modules") or [])
            or list(plan.get("interaction_modules") or [])
            or list(plan.get("js_widgets") or [])
        )

    def _build_story_substrate(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        targets = [row for row in list(enrichment_bundle.get("targets") or []) if isinstance(row, Mapping)]
        paragraph_targets = [row for row in targets if str(row.get("target_kind") or "") == "paragraph"]
        section_targets = [row for row in targets if str(row.get("target_kind") or "") == "section"]
        figure_targets = [row for row in targets if str(row.get("target_kind") or "") == "figure"]
        table_targets = [row for row in targets if str(row.get("target_kind") or "") == "table"]
        target_lookup = {
            str(item.get("target_id") or "").strip(): dict(item)
            for item in targets
            if str(item.get("target_id") or "").strip()
        }
        coherent_paragraph_targets = [
            row for row in paragraph_targets
            if not self._is_fragment_like_excerpt(str(row.get("excerpt") or row.get("title") or ""))
        ]
        claim_paragraph_targets = coherent_paragraph_targets or paragraph_targets

        main_claims: List[Dict[str, Any]] = []
        for idx, row in enumerate((claim_paragraph_targets or section_targets)[:2], start=1):
            text = self._clean_excerpt(str(row.get("excerpt") or row.get("title") or ""))
            if not text:
                continue
            main_claims.append(
                {
                    "claim_id": f"claim_{idx}",
                    "text": text,
                    "display_text": self._derive_claim_display_text(
                        {"text": text, "source_target_ids": [str(row.get("target_id") or "").strip()]},
                        target_lookup,
                    ),
                    "source_target_ids": [str(row.get("target_id") or "").strip()],
                    "strength": "primary" if idx == 1 else "supporting",
                }
            )

        evidence_units: List[Dict[str, Any]] = []
        for idx, row in enumerate((figure_targets + table_targets + claim_paragraph_targets[:1])[:3], start=1):
            kind = str(row.get("target_kind") or "paragraph").strip()
            title = str(row.get("figure_label") or row.get("title") or kind.title()).strip()
            role = "primary_visual_evidence" if idx == 1 and kind in {"figure", "table"} else "supporting_evidence"
            evidence_units.append(
                {
                    "evidence_id": f"evidence_{idx}",
                    "kind": kind if kind in {"figure", "paragraph", "table", "equation", "section"} else "paragraph",
                    "role": role,
                    "title": title,
                    "source_target_ids": [str(row.get("target_id") or "").strip()],
                }
            )

        glossary_terms = self._derive_glossary_terms(
            paragraph_targets[0] if paragraph_targets else None,
            figure_targets[0] if figure_targets else None,
        )
        terms_to_explain = [
            {
                "term": str(item.get("term") or "").strip(),
                "reason": "domain_specific_term",
                "source_target_ids": [
                    str((paragraph_targets[0] if paragraph_targets else figure_targets[0]).get("target_id") or "").strip()
                ]
                if (paragraph_targets or figure_targets)
                else [],
            }
            for item in glossary_terms[:4]
            if str(item.get("term") or "").strip()
        ]

        joined_text = " ".join(
            self._clean_excerpt(str(row.get("excerpt") or row.get("title") or ""), limit=160)
            for row in (claim_paragraph_targets[:2] + figure_targets[:1])
        ).lower()
        background_gaps: List[Dict[str, Any]] = []
        if "usmle" in joined_text:
            background_gaps.append(
                {
                    "topic": "USMLE 结构",
                    "reason": "读者未必清楚 Step 1、Step 2CK 和 Step 3 之间的区别。",
                    "suggested_resource_type": "related_public_resource",
                }
            )
        if "concordance" in joined_text or "insight" in joined_text or "doi" in joined_text:
            background_gaps.append(
                {
                    "topic": "评估指标",
                    "reason": "这一页使用了论文特定的指标，适合补一层更平实的解释。",
                    "suggested_resource_type": "glossary_panel",
                }
            )

        narrative_turns: List[Dict[str, Any]] = []
        if section_targets:
            narrative_turns.append(
                {
                    "turn_id": "turn_1",
                    "kind": "setup",
                    "label": str(section_targets[0].get("section_label") or section_targets[0].get("title") or "章节上下文").strip(),
                    "target_ids": [str(section_targets[0].get("target_id") or "").strip()],
                }
            )
        if figure_targets:
            narrative_turns.append(
                {
                    "turn_id": f"turn_{len(narrative_turns) + 1}",
                    "kind": "figure_focus",
                    "label": str(figure_targets[0].get("figure_label") or "主图").strip(),
                    "target_ids": [str(figure_targets[0].get("target_id") or "").strip()],
                }
            )
        if claim_paragraph_targets:
            lead_para = claim_paragraph_targets[0]
            narrative_turns.append(
                {
                    "turn_id": f"turn_{len(narrative_turns) + 1}",
                    "kind": "key_finding",
                    "label": self._clean_excerpt(str(lead_para.get("excerpt") or ""), limit=72),
                    "target_ids": [str(lead_para.get("target_id") or "").strip()],
                }
            )
        if len(claim_paragraph_targets) > 1:
            narrative_turns.append(
                {
                    "turn_id": f"turn_{len(narrative_turns) + 1}",
                    "kind": "implication",
                    "label": self._clean_excerpt(str(claim_paragraph_targets[1].get("excerpt") or ""), limit=72),
                    "target_ids": [str(claim_paragraph_targets[1].get("target_id") or "").strip()],
                }
            )

        return {
            "version": "v1",
            "page_id": f"p{int(page)}",
            "main_claims": main_claims,
            "evidence_units": evidence_units,
            "terms_to_explain": terms_to_explain,
            "background_gaps": background_gaps,
            "narrative_turns": narrative_turns,
            "meta": {
                "page": int(page),
                "user_intent": str(user_intent or "").strip(),
                "target_count": len(targets),
            },
        }

    @staticmethod
    def _infer_page_archetype(
        *,
        story_substrate: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
    ) -> str:
        evidence_units = [row for row in list(story_substrate.get("evidence_units") or []) if isinstance(row, Mapping)]
        background_gaps = [row for row in list(story_substrate.get("background_gaps") or []) if isinstance(row, Mapping)]
        terms_to_explain = [row for row in list(story_substrate.get("terms_to_explain") or []) if isinstance(row, Mapping)]
        targets = [row for row in list(enrichment_bundle.get("targets") or []) if isinstance(row, Mapping)]
        section_labels = " ".join(str(row.get("section_label") or row.get("title") or "") for row in targets).lower()
        primary_visual = any(
            str(row.get("kind") or "").strip() in {"figure", "table"}
            and str(row.get("role") or "").strip() == "primary_visual_evidence"
            for row in evidence_units
        )
        if "method" in section_labels or "protocol" in section_labels:
            return "methods_decoder"
        if primary_visual:
            return "figure_explainer"
        if terms_to_explain and any("metric" in str(row.get("topic") or "").lower() for row in background_gaps):
            return "concept_decoder"
        if len([row for row in list(story_substrate.get("main_claims") or []) if isinstance(row, Mapping)]) >= 2:
            return "finding_digest"
        return "context_builder"

    @staticmethod
    def _compose_page_goal(
        *,
        user_intent: str,
        claims: Sequence[Mapping[str, Any]],
        focus_label: str,
        archetype: str,
    ) -> str:
        explicit = str(user_intent or "").strip()
        if explicit:
            return explicit
        lead_claim = str((claims[0] or {}).get("text") or "").strip() if claims else ""
        if archetype == "figure_explainer" and focus_label:
            return f"帮助读者通过 {focus_label} 理解这一页的核心结果。"
        if archetype == "methods_decoder":
            return "把这一页组织成一个能实际读懂的方法 walkthrough，并说明它为什么重要。"
        if archetype == "concept_decoder":
            return "把这一页的指标和领域术语翻译成容易理解、能直接使用的解释。"
        if lead_claim and not GenerativeReaderAgentRuntime._is_english_heavy_text(lead_claim):
            return f"帮助读者理解这一页的主结论：{lead_claim}"
        return "把清洗后的阅读流组织成一个有引导感的解释型页面。"

    @staticmethod
    def _compose_hero_angle(
        *,
        archetype: str,
        focus_label: str,
        page_goal: str,
        claims: Sequence[Mapping[str, Any]],
    ) -> str:
        lead_claim = str((claims[0] or {}).get("text") or "").strip() if claims else ""
        if archetype == "figure_explainer":
            if focus_label and lead_claim and not GenerativeReaderAgentRuntime._is_english_heavy_text(lead_claim):
                return f"从 {focus_label} 开始，用它来解释这一页的核心结论：{lead_claim}"
            if focus_label:
                return f"先把 {focus_label} 当作主要视觉锚点，再回到支撑正文。"
        if archetype == "methods_decoder":
            return "按“设置 - 过程 - 含义”的顺序来组织，让这一页像一份有引导的方法笔记。"
        if archetype == "concept_decoder":
            return "先把这一页的技术词汇讲清楚，再让读者去解释结果。"
        if archetype == "finding_digest" and lead_claim and not GenerativeReaderAgentRuntime._is_english_heavy_text(lead_claim):
            return f"先亮出最强结论，再展示支撑它的证据：{lead_claim}"
        return page_goal

    @staticmethod
    def _is_english_heavy_text(text: str) -> bool:
        return GenerativeReaderAgentRuntime._needs_display_localization(text, short_form=False)

    @staticmethod
    def _derive_experience_hooks(
        *,
        archetype: str,
        focus_label: str,
        background_topics: Sequence[str],
        has_terms: bool,
    ) -> List[str]:
        hooks: List[str] = []
        if archetype == "figure_explainer":
            hooks.append(f"先看 {focus_label or '主图'}，再回到支撑正文。")
            hooks.append("展开面板引导，理解每个子图分别为结果贡献了什么。")
        elif archetype == "methods_decoder":
            hooks.append("先读设置，再把这一页当成一个带引导的方法 walkthrough。")
        elif archetype == "concept_decoder":
            hooks.append("如果这一页出现陌生指标或评价语言，先打开术语解释。")
        else:
            hooks.append("先抓住页面目标，再用关键证据固定你的理解。")
        if has_terms:
            hooks.append("只有当技术术语真的卡住理解时，再去看解释卡片。")
        if background_topics:
            hooks.append(f"只在需要补充 {', '.join(background_topics[:2])} 这类背景时再打开外部资料，不要拿它替代论文。")
        deduped: List[str] = []
        for item in hooks:
            token = str(item or "").strip()
            if token and token not in deduped:
                deduped.append(token)
        return deduped[:4]

    @staticmethod
    def _compose_resource_strategy(
        *,
        archetype: str,
        background_topics: Sequence[str],
    ) -> str:
        if background_topics:
            return (
                f"使用 1-3 个权威公开资源来解释 {', '.join(background_topics[:2])}，"
                "帮助读者理解背景，但不要重复论文自己的论证。"
            )
        if archetype == "figure_explainer":
            return "优先补充和图直接相关的背景，以及一个真正能帮助读懂主视觉证据的高价值来源。"
        if archetype == "methods_decoder":
            return "优先补充定义方法的参考资料或官方文档，帮助读者理解这里描述的流程。"
        return "外部资源要少而精，只在它们能降低读者困惑时再使用。"

    @staticmethod
    def _compose_section_display_summary(
        *,
        section_type: str,
        archetype: str,
        focus_label: str,
        background_topics: Sequence[str],
        resource_strategy: str,
    ) -> str:
        section_token = str(section_type or "").strip()
        focus_token = str(focus_label or "").strip()
        if section_token == "hero":
            return "先明确这一页最值得关注的问题，再进入图和正文。"
        if section_token == "focus_stage":
            if archetype == "figure_explainer" and focus_token:
                return f"先看 {focus_token}，抓住这页最关键的结果和比较对象。"
            return "先抓住最关键的图或证据，再带着问题回到正文。"
        if section_token == "reading_flow":
            return "回到正文，确认作者如何解释这些结果，以及证据是否支撑结论。"
        if section_token == "explainer_cluster":
            return "把读懂这一页必须知道的术语、指标和概念补齐。"
        if section_token == "supporting_resources":
            if background_topics:
                return f"补充理解这页所需的 {', '.join(background_topics[:2])} 等背景，而不是替代论文本身。"
            if resource_strategy:
                return "补充少量高相关的外部资料，帮助理解这页内容。"
            return "补充理解这页所需的少量背景资料，而不是替代论文内容。"
        if section_token == "question_lab":
            return "用几个追问检查自己是否真正理解了这一页。"
        if section_token == "story_map":
            return "在不打扰主阅读面的前提下，补充这页的叙事意图、阅读钩子和工具决策。"
        return "围绕当前页面的核心内容组织一个更易读的阅读入口。"

    @staticmethod
    def _dedupe_strings(rows: Sequence[str], limit: Optional[int] = None) -> List[str]:
        deduped: List[str] = []
        seen: set[str] = set()
        for row in list(rows or []):
            token = str(row or "").strip()
            if not token:
                continue
            normalized = re.sub(r"\s+", " ", token).strip().lower()
            if normalized in seen:
                continue
            deduped.append(token)
            seen.add(normalized)
            if limit and len(deduped) >= limit:
                break
        return deduped

    @staticmethod
    def _derive_content_budget(
        *,
        archetype: str,
        has_focus: bool,
        has_terms: bool,
        has_background_gaps: bool,
    ) -> Dict[str, int]:
        budget = {
            "max_claim_cards": 2,
            "max_hooks": 2,
            "max_resource_modules": 2 if has_background_gaps else 1,
            "max_explainer_modules": 2 if has_terms else 1,
            "max_question_modules": 1,
            "max_widgets": 1 if has_focus else 0,
        }
        if archetype == "methods_decoder":
            budget["max_resource_modules"] = 1
            budget["max_widgets"] = 0
        elif archetype == "concept_decoder":
            budget["max_explainer_modules"] = 3 if has_terms else 2
            budget["max_resource_modules"] = 1
        elif archetype == "context_builder":
            budget["max_claim_cards"] = 1
            budget["max_hooks"] = 1
        return budget

    @classmethod
    def _build_page_storyboard(
        cls,
        *,
        page_goal: str,
        archetype: str,
        primary_focus_target_id: str,
        secondary_support_target_ids: Sequence[str],
        background_gaps: Sequence[Mapping[str, Any]],
        has_terms: bool,
    ) -> List[Dict[str, Any]]:
        storyboard: List[Dict[str, Any]] = [
            {
                "beat_id": "beat_hero",
                "role": "orient",
                "section_type": "hero",
                "title": "开场",
                "purpose": cls._clean_excerpt(page_goal or "先建立这一页的阅读目标。", limit=120),
                "target_ids": [primary_focus_target_id] if primary_focus_target_id else [],
                "priority": 1,
            }
        ]
        if primary_focus_target_id:
            storyboard.append(
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "先看最强证据" if archetype != "figure_explainer" else "拆解这张图",
                    "purpose": "先围绕最关键的图或证据建立理解抓手，不急着把所有信息同时展开。",
                    "target_ids": [primary_focus_target_id],
                    "priority": 2,
                }
            )
        storyboard.append(
            {
                "beat_id": "beat_read",
                "role": "read_support",
                "section_type": "reading_flow",
                "title": "阅读支撑正文" if archetype == "figure_explainer" else "阅读关键段落",
                "purpose": "把清洗后的正文作为主阅读流，避免让辅助卡片替代论文内容本身。",
                "target_ids": cls._dedupe_strings([primary_focus_target_id, *secondary_support_target_ids])[:4],
                "priority": 3,
            }
        )
        if has_terms:
            storyboard.append(
                {
                    "beat_id": "beat_explain",
                    "role": "clarify_terms",
                    "section_type": "explainer_cluster",
                    "title": "读懂关键术语" if archetype != "methods_decoder" else "把方法术语讲明白",
                    "purpose": "只解释真正会阻碍理解的术语和指标，不重复正文已表达的结论。",
                    "target_ids": cls._dedupe_strings([primary_focus_target_id, *secondary_support_target_ids])[:4],
                    "priority": 4,
                }
            )
        if background_gaps:
            storyboard.append(
                {
                    "beat_id": "beat_context",
                    "role": "add_context",
                    "section_type": "supporting_resources",
                    "title": "补充背景与上下文",
                    "purpose": "只补充理解当前页真正缺失的外部背景，控制数量，避免资源堆砌。",
                    "target_ids": cls._dedupe_strings([primary_focus_target_id, *secondary_support_target_ids])[:4],
                    "priority": 5,
                }
            )
        storyboard.append(
            {
                "beat_id": "beat_questions",
                "role": "test_understanding",
                "section_type": "question_lab",
                "title": "继续追问",
                "purpose": "把当前理解转成少量值得继续追问的问题，而不是再堆一轮摘要。",
                "target_ids": cls._dedupe_strings([primary_focus_target_id, *secondary_support_target_ids])[:4],
                "priority": 6,
            }
        )
        return storyboard

    @staticmethod
    def _module_signature(module: Mapping[str, Any], key_name: str) -> str:
        module_type = str(module.get(key_name) or module.get("module_type") or module.get("widget_type") or "").strip().lower()
        target_ids = "|".join(sorted(str(item or "").strip() for item in list(module.get("target_ids") or []) if str(item or "").strip()))
        title = re.sub(r"\s+", " ", str(module.get("display_title") or module.get("title") or "").strip()).lower()
        return f"{module_type}:{target_ids}:{title}"

    @classmethod
    def _prune_ranked_modules(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        limit: int,
        score_fn,
        signature_key: str,
    ) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        ranked = sorted(
            [dict(row) for row in list(rows or []) if isinstance(row, Mapping)],
            key=score_fn,
            reverse=True,
        )
        kept: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in ranked:
            signature = cls._module_signature(row, signature_key)
            if signature in seen:
                continue
            kept.append(row)
            seen.add(signature)
            if len(kept) >= limit:
                break
        return kept

    def _build_page_brief(
        self,
        *,
        page: int,
        user_intent: str,
        story_substrate: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        evidence_units = [row for row in list(story_substrate.get("evidence_units") or []) if isinstance(row, Mapping)]
        background_gaps = [row for row in list(story_substrate.get("background_gaps") or []) if isinstance(row, Mapping)]
        claims = [row for row in list(story_substrate.get("main_claims") or []) if isinstance(row, Mapping)]
        turns = [row for row in list(story_substrate.get("narrative_turns") or []) if isinstance(row, Mapping)]
        targets = [row for row in list(enrichment_bundle.get("targets") or []) if isinstance(row, Mapping)]

        primary_focus_target_id = ""
        for row in evidence_units:
            candidate_ids = [str(item).strip() for item in list(row.get("source_target_ids") or []) if str(item).strip()]
            if candidate_ids:
                primary_focus_target_id = candidate_ids[0]
                break
        if not primary_focus_target_id and targets:
            primary_focus_target_id = str(targets[0].get("target_id") or "").strip()

        secondary_support_target_ids: List[str] = []
        for row in list(claims or []) + list(evidence_units or []):
            for target_id in [str(item).strip() for item in list(row.get("source_target_ids") or []) if str(item).strip()]:
                if target_id and target_id != primary_focus_target_id and target_id not in secondary_support_target_ids:
                    secondary_support_target_ids.append(target_id)
            if len(secondary_support_target_ids) >= 3:
                break

        focus_label = ""
        for row in evidence_units:
            target_ids = [str(item).strip() for item in list(row.get("source_target_ids") or []) if str(item).strip()]
            if primary_focus_target_id and primary_focus_target_id in target_ids:
                focus_label = str(row.get("title") or "").strip()
                break
        archetype = self._infer_page_archetype(
            story_substrate=story_substrate,
            enrichment_bundle=enrichment_bundle,
        )
        page_goal = self._compose_page_goal(
            user_intent=user_intent,
            claims=claims,
            focus_label=focus_label,
            archetype=archetype,
        )

        reading_path: List[str] = []
        if primary_focus_target_id:
            reading_path.extend(["hero_summary", "focus_evidence"])
        reading_path.append("reading_flow")
        if list(story_substrate.get("terms_to_explain") or []):
            reading_path.append("context_explainer")
        if background_gaps:
            reading_path.append("supporting_resources")
        reading_path.append("explore_questions")

        interaction_opportunities: List[str] = []
        if primary_focus_target_id:
            interaction_opportunities.append("expand_focus_panels")
        if list(story_substrate.get("terms_to_explain") or []):
            interaction_opportunities.append("open_term_glossary")
        if background_gaps:
            interaction_opportunities.append("open_supporting_resources")

        resource_gaps = [
            str(row.get("topic") or "").strip()
            for row in background_gaps
            if str(row.get("topic") or "").strip()
        ]
        hero_angle = self._compose_hero_angle(
            archetype=archetype,
            focus_label=focus_label,
            page_goal=page_goal,
            claims=claims,
        )
        experience_hooks = self._dedupe_strings(self._derive_experience_hooks(
            archetype=archetype,
            focus_label=focus_label,
            background_topics=resource_gaps,
            has_terms=bool(list(story_substrate.get("terms_to_explain") or [])),
        ), limit=3)
        resource_strategy = self._compose_resource_strategy(
            archetype=archetype,
            background_topics=resource_gaps,
        )
        storyboard = self._build_page_storyboard(
            page_goal=page_goal,
            archetype=archetype,
            primary_focus_target_id=primary_focus_target_id,
            secondary_support_target_ids=secondary_support_target_ids,
            background_gaps=background_gaps,
            has_terms=bool(list(story_substrate.get("terms_to_explain") or [])),
        )
        content_budget = self._derive_content_budget(
            archetype=archetype,
            has_focus=bool(primary_focus_target_id),
            has_terms=bool(list(story_substrate.get("terms_to_explain") or [])),
            has_background_gaps=bool(background_gaps),
        )
        storyboard_to_reading = {
            "hero": "hero_summary",
            "focus_stage": "focus_evidence",
            "reading_flow": "reading_flow",
            "explainer_cluster": "context_explainer",
            "supporting_resources": "supporting_resources",
            "question_lab": "explore_questions",
        }
        reading_path = [
            storyboard_to_reading.get(str(row.get("section_type") or "").strip(), "")
            for row in storyboard
        ]

        return {
            "version": "v1",
            "page_goal": page_goal,
            "reader_type": "curious_generalist",
            "page_archetype": archetype,
            "hero_angle": hero_angle,
            "primary_focus_target_id": primary_focus_target_id,
            "secondary_support_target_ids": secondary_support_target_ids,
            "reading_path": list(dict.fromkeys([item for item in reading_path if item])),
            "interaction_opportunities": list(dict.fromkeys([item for item in interaction_opportunities if item])),
            "resource_gaps": resource_gaps,
            "experience_hooks": experience_hooks,
            "resource_strategy": resource_strategy,
            "storyboard": storyboard,
            "content_budget": content_budget,
            "meta": {
                "page": int(page),
                "narrative_turn_count": len(turns),
                "include_story_map": False,
            },
        }

    @staticmethod
    def _index_targets(enrichment_bundle: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        rows = [row for row in list(enrichment_bundle.get("targets") or []) if isinstance(row, Mapping)]
        indexed: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            target_id = str(row.get("target_id") or "").strip()
            if not target_id:
                continue
            indexed[target_id] = dict(row)
        return indexed

    @classmethod
    def _module_target_priority(cls, row: Mapping[str, Any], primary_focus_target_id: str) -> int:
        target_ids = {str(item or "").strip() for item in list(row.get("target_ids") or []) if str(item or "").strip()}
        score = 0
        if primary_focus_target_id and primary_focus_target_id in target_ids:
            score += 100
        score += min(len(target_ids), 3) * 5
        return score

    @staticmethod
    def _coerce_budget_value(raw: Any, default: int, *, minimum: int = 0, maximum: int = 6) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _normalize_story_substrate_contract(
        self,
        *,
        story_substrate: Mapping[str, Any],
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        fallback = self._build_story_substrate(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        current = dict(story_substrate or {})
        normalized = dict(fallback)
        normalized["version"] = str(current.get("version") or fallback.get("version") or "v1").strip() or "v1"
        normalized["page_id"] = str(current.get("page_id") or fallback.get("page_id") or f"p{int(page)}").strip() or f"p{int(page)}"

        claims: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(current.get("main_claims") or []), start=1):
            if not isinstance(row, Mapping):
                continue
            claim_id = str(row.get("claim_id") or f"claim_{idx}").strip()
            if not claim_id:
                continue
            claims.append(
                {
                    "claim_id": claim_id,
                    "text": str(row.get("text") or "").strip(),
                    "display_text": str(row.get("display_text") or "").strip(),
                    "source_target_ids": [str(item).strip() for item in list(row.get("source_target_ids") or []) if str(item).strip()],
                    "strength": str(row.get("strength") or "supporting").strip() or "supporting",
                }
            )
        normalized["main_claims"] = claims or list(fallback.get("main_claims") or [])

        evidence_units: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(current.get("evidence_units") or []), start=1):
            if not isinstance(row, Mapping):
                continue
            evidence_id = str(row.get("evidence_id") or f"evidence_{idx}").strip()
            if not evidence_id:
                continue
            evidence_units.append(
                {
                    "evidence_id": evidence_id,
                    "kind": str(row.get("kind") or "paragraph").strip() or "paragraph",
                    "role": str(row.get("role") or "").strip(),
                    "title": str(row.get("title") or "").strip(),
                    "source_target_ids": [str(item).strip() for item in list(row.get("source_target_ids") or []) if str(item).strip()],
                }
            )
        normalized["evidence_units"] = evidence_units or list(fallback.get("evidence_units") or [])

        normalized["terms_to_explain"] = [
            {
                "term": str(row.get("term") or "").strip(),
                "reason": str(row.get("reason") or "").strip(),
                "source_target_ids": [str(item).strip() for item in list(row.get("source_target_ids") or []) if str(item).strip()],
            }
            for row in list(current.get("terms_to_explain") or [])
            if isinstance(row, Mapping) and str(row.get("term") or "").strip()
        ] or list(fallback.get("terms_to_explain") or [])
        normalized["background_gaps"] = [
            {
                "topic": str(row.get("topic") or "").strip(),
                "reason": str(row.get("reason") or "").strip(),
                "suggested_resource_type": str(row.get("suggested_resource_type") or "").strip(),
            }
            for row in list(current.get("background_gaps") or [])
            if isinstance(row, Mapping) and str(row.get("topic") or "").strip()
        ] or list(fallback.get("background_gaps") or [])

        turns: List[Dict[str, Any]] = []
        for idx, row in enumerate(list(current.get("narrative_turns") or []), start=1):
            if not isinstance(row, Mapping):
                continue
            turn_id = str(row.get("turn_id") or f"turn_{idx}").strip()
            kind = str(row.get("kind") or "").strip()
            if not turn_id or not kind:
                continue
            turns.append(
                {
                    "turn_id": turn_id,
                    "kind": kind,
                    "label": str(row.get("label") or "").strip(),
                    "target_ids": [str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()],
                }
            )
        normalized["narrative_turns"] = turns or list(fallback.get("narrative_turns") or [])
        normalized["meta"] = dict(fallback.get("meta") or {}) | dict(current.get("meta") or {})
        return normalized

    def _normalize_page_brief_contract(
        self,
        *,
        page_brief: Mapping[str, Any],
        page: int,
        user_intent: str,
        story_substrate: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        fallback = self._build_page_brief(
            page=int(page),
            user_intent=user_intent,
            story_substrate=story_substrate,
            enrichment_bundle=enrichment_bundle,
        )
        current = dict(page_brief or {})
        normalized = dict(fallback)
        for key in [
            "version",
            "page_goal",
            "reader_type",
            "page_archetype",
            "hero_angle",
            "primary_focus_target_id",
            "resource_strategy",
        ]:
            token = str(current.get(key) or "").strip()
            if token:
                normalized[key] = token
        normalized["secondary_support_target_ids"] = self._dedupe_strings(
            [str(item).strip() for item in list(current.get("secondary_support_target_ids") or []) if str(item).strip()]
        )[:4] or list(fallback.get("secondary_support_target_ids") or [])
        normalized["interaction_opportunities"] = self._dedupe_strings(
            [str(item).strip() for item in list(current.get("interaction_opportunities") or []) if str(item).strip()]
        ) or list(fallback.get("interaction_opportunities") or [])
        normalized["resource_gaps"] = self._dedupe_strings(
            [str(item).strip() for item in list(current.get("resource_gaps") or []) if str(item).strip()]
        )[:4] or list(fallback.get("resource_gaps") or [])
        normalized["experience_hooks"] = self._dedupe_strings(
            [str(item).strip() for item in list(current.get("experience_hooks") or []) if str(item).strip()],
            limit=3,
        ) or list(fallback.get("experience_hooks") or [])

        fallback_budget = dict(fallback.get("content_budget") or {})
        raw_budget = dict(current.get("content_budget") or {})
        normalized["content_budget"] = {
            "max_claim_cards": self._coerce_budget_value(raw_budget.get("max_claim_cards"), int(fallback_budget.get("max_claim_cards") or 2), maximum=4),
            "max_hooks": self._coerce_budget_value(raw_budget.get("max_hooks"), int(fallback_budget.get("max_hooks") or 2), maximum=4),
            "max_resource_modules": self._coerce_budget_value(raw_budget.get("max_resource_modules"), int(fallback_budget.get("max_resource_modules") or 2), maximum=4),
            "max_explainer_modules": self._coerce_budget_value(raw_budget.get("max_explainer_modules"), int(fallback_budget.get("max_explainer_modules") or 2), maximum=4),
            "max_question_modules": self._coerce_budget_value(raw_budget.get("max_question_modules"), int(fallback_budget.get("max_question_modules") or 1), maximum=3),
            "max_widgets": self._coerce_budget_value(raw_budget.get("max_widgets"), int(fallback_budget.get("max_widgets") or 1), maximum=2),
        }

        fallback_storyboard = [dict(row) for row in list(fallback.get("storyboard") or []) if isinstance(row, Mapping)]
        raw_storyboard = [dict(row) for row in list(current.get("storyboard") or []) if isinstance(row, Mapping)]
        storyboard_rows = raw_storyboard or fallback_storyboard
        storyboard: List[Dict[str, Any]] = []
        seen_sections: set[str] = set()
        for idx, row in enumerate(storyboard_rows, start=1):
            section_type = str(row.get("section_type") or "").strip()
            if section_type not in self.EXPERIENCE_SECTION_TYPES or section_type in seen_sections:
                continue
            beat = {
                "beat_id": str(row.get("beat_id") or f"beat_{section_type}_{idx}").strip() or f"beat_{section_type}_{idx}",
                "role": str(row.get("role") or section_type).strip() or section_type,
                "section_type": section_type,
                "title": str(row.get("title") or "").strip() or str(next((item.get("title") for item in fallback_storyboard if str(item.get("section_type") or "").strip() == section_type), section_type)).strip(),
                "purpose": str(row.get("purpose") or "").strip() or str(next((item.get("purpose") for item in fallback_storyboard if str(item.get("section_type") or "").strip() == section_type), "")).strip(),
                "target_ids": self._dedupe_strings([str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()])[:4],
                "priority": idx,
            }
            storyboard.append(beat)
            seen_sections.add(section_type)
        normalized["storyboard"] = storyboard or fallback_storyboard
        storyboard_to_reading = {
            "hero": "hero_summary",
            "focus_stage": "focus_evidence",
            "reading_flow": "reading_flow",
            "explainer_cluster": "context_explainer",
            "supporting_resources": "supporting_resources",
            "question_lab": "explore_questions",
        }
        raw_reading_path = [str(item).strip() for item in list(current.get("reading_path") or []) if str(item).strip()]
        normalized["reading_path"] = raw_reading_path or list(
            dict.fromkeys(
                storyboard_to_reading.get(str(row.get("section_type") or "").strip(), "")
                for row in normalized["storyboard"]
            )
        )
        normalized["reading_path"] = [item for item in normalized["reading_path"] if item]
        normalized["meta"] = dict(fallback.get("meta") or {}) | dict(current.get("meta") or {})
        normalized["meta"]["include_story_map"] = bool(normalized["meta"].get("include_story_map"))
        return normalized

    def _validate_generative_plan_contract(
        self,
        *,
        parsed: Mapping[str, Any],
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        current = dict(parsed or {})
        current["story_substrate"] = self._normalize_story_substrate_contract(
            story_substrate=current.get("story_substrate") or {},
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        current["page_brief"] = self._normalize_page_brief_contract(
            page_brief=current.get("page_brief") or {},
            page=int(page),
            user_intent=user_intent,
            story_substrate=current["story_substrate"],
            enrichment_bundle=enrichment_bundle,
        )
        try:
            validated = ReaderGenerativePlan.model_validate(current).model_dump(mode="python")
            meta = dict(validated.get("meta") or {})
            meta["contract_validation"] = {"status": "validated", "contract": "generative_plan_v1"}
            validated["meta"] = meta
            return validated
        except ValidationError as exc:
            logger.warning("[GenerativeReaderRuntime] generative plan contract fallback page={} errors={}", page, exc.error_count())
            fallback = self._build_fallback_plan(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
            )
            fallback["story_substrate"] = current["story_substrate"]
            fallback["page_brief"] = current["page_brief"]
            fallback["used_tools"] = list(current.get("used_tools") or [])
            fallback["tool_trace"] = list(current.get("tool_trace") or [])
            fallback_meta = dict(current.get("meta") or {})
            fallback_meta["contract_validation"] = {
                "status": "fallback",
                "contract": "generative_plan_v1",
                "error_count": exc.error_count(),
            }
            fallback["meta"] = fallback_meta
            return ReaderGenerativePlan.model_validate(fallback).model_dump(mode="python")

    def _validate_experience_plan_contract(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        current = dict(plan or {})
        current["main_sections"] = self._normalize_experience_section_blocks(
            sections=list(current.get("main_sections") or []),
            resource_modules=list(current.get("supporting_resources") or []),
            interaction_modules=list(current.get("interactive_blocks") or []),
            widget_blocks=list(current.get("widget_blocks") or []),
        )
        try:
            validated = ReaderExperiencePlan.model_validate(current).model_dump(mode="python")
            meta = dict(validated.get("meta") or {})
            meta["contract_validation"] = {"status": "validated", "contract": "experience_plan_v1"}
            validated["meta"] = meta
            return validated
        except ValidationError as exc:
            logger.warning(
                "[GenerativeReaderRuntime] experience plan contract fallback page={} errors={}",
                current.get("focus_page"),
                exc.error_count(),
            )
            hero = dict(current.get("hero") or {})
            focus_page = max(1, int(current.get("focus_page") or 1))
            fallback = {
                "version": "v1",
                "status": str(current.get("status") or "fallback").strip() or "fallback",
                "scope": str(current.get("scope") or "page_focus").strip() or "page_focus",
                "focus_page": focus_page,
                "reader_profile": str(current.get("reader_profile") or "curious_generalist").strip() or "curious_generalist",
                "layout_variant": str(current.get("layout_variant") or "guided_story_stack").strip() or "guided_story_stack",
                "page_story_title": str(current.get("page_story_title") or hero.get("title") or "展开阅读").strip() or "展开阅读",
                "page_story_subtitle": str(current.get("page_story_subtitle") or hero.get("subtitle") or "").strip(),
                "narrative_goal": str(current.get("narrative_goal") or hero.get("summary") or "").strip(),
                "hero": hero,
                "main_sections": [],
                "supporting_resources": [dict(row) for row in list(current.get("supporting_resources") or []) if isinstance(row, Mapping)],
                "interactive_blocks": [dict(row) for row in list(current.get("interactive_blocks") or []) if isinstance(row, Mapping)],
                "widget_blocks": [dict(row) for row in list(current.get("widget_blocks") or []) if isinstance(row, Mapping)],
                "reading_path": [str(item).strip() for item in list(current.get("reading_path") or []) if str(item).strip()],
                "used_tools": [str(item).strip() for item in list(current.get("used_tools") or []) if str(item).strip()],
                "meta": dict(current.get("meta") or {}),
            }
            fallback["meta"]["contract_validation"] = {
                "status": "fallback",
                "contract": "experience_plan_v1",
                "error_count": exc.error_count(),
            }
            return ReaderExperiencePlan.model_validate(fallback).model_dump(mode="python")

    def _normalize_experience_section_blocks(
        self,
        *,
        sections: Sequence[Mapping[str, Any]],
        resource_modules: Sequence[Mapping[str, Any]],
        interaction_modules: Sequence[Mapping[str, Any]],
        widget_blocks: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        resource_lookup = {
            str(row.get("module_id") or "").strip(): row
            for row in resource_modules
            if isinstance(row, Mapping) and str(row.get("module_id") or "").strip()
        }
        interaction_lookup = {
            str(row.get("module_id") or "").strip(): row
            for row in interaction_modules
            if isinstance(row, Mapping) and str(row.get("module_id") or "").strip()
        }
        widget_lookup = {
            str(row.get("widget_id") or "").strip(): row
            for row in widget_blocks
            if isinstance(row, Mapping) and str(row.get("widget_id") or "").strip()
        }
        valid_states = {"ready", "empty", "loading", "partial", "error"}

        def make_ref(
            *,
            block_type: str,
            ref_id: str,
            order: int,
            payload: Mapping[str, Any] | None,
        ) -> Dict[str, Any]:
            item = payload or {}
            module_type = str(item.get("module_type") or item.get("widget_type") or "").strip()
            raw_state = str(
                item.get("state")
                or ((item.get("meta") or {}) if isinstance(item.get("meta"), Mapping) else {}).get("state")
                or ""
            ).strip().lower()
            normalized_state = raw_state if raw_state in valid_states else ("ready" if payload else "empty")
            user_actions, agent_actions = self._derive_block_actions(
                block_type=block_type,
                variant=module_type,
                payload=item,
            )
            ui_actions, event_bindings = self._derive_block_protocol(
                block_type=block_type,
                ref_id=ref_id,
                variant=module_type,
                payload=item,
                user_actions=user_actions,
                agent_actions=agent_actions,
            )
            return {
                "block_id": f"{block_type}:{ref_id}",
                "block_type": block_type,
                "version": "block_ref_v1",
                "ref_id": ref_id,
                "variant": module_type,
                "target_ids": [str(target_id).strip() for target_id in list(item.get("target_ids") or []) if str(target_id).strip()],
                "priority": order,
                "state": normalized_state,
                "data_requirements": [str(value).strip() for value in list(item.get("data_requirements") or []) if str(value).strip()],
                "fallback_policy": "omit",
                "user_actions": user_actions,
                "agent_actions": agent_actions,
                "ui_actions": ui_actions,
                "event_bindings": event_bindings,
                "meta": {},
            }

        normalized_sections: List[Dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            normalized = dict(section)
            block_refs: List[Dict[str, Any]] = []
            existing_blocks = list(section.get("blocks") or [])
            for block in existing_blocks:
                if isinstance(block, Mapping) and str(block.get("block_id") or "").strip():
                    current_block = dict(block)
                    current_block["version"] = str(current_block.get("version") or "block_ref_v1").strip() or "block_ref_v1"
                    current_state = str(current_block.get("state") or "").strip().lower()
                    current_block["state"] = current_state if current_state in valid_states else "ready"
                    block_refs.append(current_block)
            if not block_refs:
                order = 0
                for module_id in [str(item).strip() for item in list(section.get("resource_module_ids") or []) if str(item).strip()]:
                    block_refs.append(make_ref(block_type="resource_module", ref_id=module_id, order=order, payload=resource_lookup.get(module_id)))
                    order += 1
                for module_id in [str(item).strip() for item in list(section.get("interaction_module_ids") or []) if str(item).strip()]:
                    block_refs.append(make_ref(block_type="interaction_module", ref_id=module_id, order=order, payload=interaction_lookup.get(module_id)))
                    order += 1
                for widget_id in [str(item).strip() for item in list(section.get("widget_ids") or []) if str(item).strip()]:
                    block_refs.append(make_ref(block_type="widget", ref_id=widget_id, order=order, payload=widget_lookup.get(widget_id)))
                    order += 1
            block_refs.sort(key=lambda row: (int(row.get("priority") or 0), str(row.get("block_id") or "")))
            normalized["blocks"] = block_refs
            normalized_sections.append(normalized)
        return normalized_sections

    @staticmethod
    def _derive_block_actions(
        *,
        block_type: str,
        variant: str,
        payload: Mapping[str, Any],
    ) -> tuple[List[str], List[str]]:
        normalized_block_type = str(block_type or "").strip()
        normalized_variant = str(variant or "").strip()
        target_ids = [str(item).strip() for item in list(payload.get("target_ids") or []) if str(item).strip()]
        has_links = bool(list(payload.get("links") or []))

        user_actions: List[str] = []
        agent_actions: List[str] = []

        if normalized_block_type == "resource_module":
            if normalized_variant == "FigureExplainPanel":
                user_actions.extend(["focus_evidence", "expand_figure"])
                agent_actions.extend(["ground_figure_explanation", "sync_focus_stage"])
            elif normalized_variant == "RelatedResourceCard":
                user_actions.extend(["open_resource", "inspect_source"] if has_links else ["inspect_source"])
                agent_actions.extend(["retrieve_supporting_resource", "summarize_resource_relevance"])
            else:
                user_actions.append("inspect_resource")
                agent_actions.append("hydrate_resource_block")
        elif normalized_block_type == "interaction_module":
            if normalized_variant == "GlossaryPanel":
                user_actions.extend(["expand_definition", "return_to_reader"])
                agent_actions.extend(["ground_term_definition", "preserve_reader_context"])
            elif normalized_variant == "QuestionStarterPanel":
                user_actions.extend(["start_followup", "compare_evidence"])
                agent_actions.extend(["propose_followup_questions", "route_followup_grounding"])
            else:
                user_actions.append("inspect_interaction")
                agent_actions.append("hydrate_interaction_block")
        elif normalized_block_type == "widget":
            if normalized_variant == "figure-focus-accordion":
                user_actions.extend(["expand_panel", "focus_target"])
                agent_actions.extend(["sync_focus_stage", "highlight_evidence_anchor"])
            else:
                user_actions.append("inspect_widget")
                agent_actions.append("hydrate_widget_block")

        if target_ids and "focus_target" not in user_actions and normalized_block_type != "resource_module":
            user_actions.append("focus_target")
        if target_ids and "sync_focus_stage" not in agent_actions:
            agent_actions.append("sync_focus_stage")

        return user_actions, agent_actions

    @staticmethod
    def _derive_block_protocol(
        *,
        block_type: str,
        ref_id: str,
        variant: str,
        payload: Mapping[str, Any],
        user_actions: Sequence[str],
        agent_actions: Sequence[str],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        target_ids = [str(item).strip() for item in list(payload.get("target_ids") or []) if str(item).strip()]
        primary_target = target_ids[0] if target_ids else ""
        normalized_variant = str(variant or "").strip()

        label_map = {
            "open_resource": "打开资源",
            "inspect_source": "查看来源",
            "expand_panel": "展开图解",
            "focus_target": "定位正文证据",
            "expand_definition": "展开术语解释",
            "return_to_reader": "回到正文",
            "start_followup": "继续追问",
            "compare_evidence": "比较证据",
            "inspect_resource": "查看资源",
            "inspect_interaction": "查看交互",
            "inspect_widget": "查看区块",
        }

        action_target_ref = primary_target or ref_id
        ui_actions: List[Dict[str, Any]] = []
        event_bindings: List[Dict[str, Any]] = []

        for action_name in user_actions:
            event_name = f"block.{action_name}"
            action_id = f"{block_type}:{ref_id}:{action_name}"
            ui_actions.append({
                "action_id": action_id,
                "action_type": action_name,
                "label": label_map.get(action_name, action_name),
                "target_ref": action_target_ref,
                "payload": {
                    "block_ref": ref_id,
                    "block_type": block_type,
                    "variant": normalized_variant,
                },
                "event_name": event_name,
                "agent_handoff": action_name in {"start_followup", "compare_evidence"},
                "meta": {},
            })
            event_bindings.append({
                "event_id": f"event:{block_type}:{ref_id}:{action_name}",
                "event_name": event_name,
                "event_source": "user",
                "event_type": "ui_action",
                "action_ids": [action_id],
                "target_ref": action_target_ref,
                "payload": {"block_ref": ref_id},
                "meta": {},
            })

        if agent_actions:
            event_bindings.append({
                "event_id": f"event:{block_type}:{ref_id}:agent",
                "event_name": "agent.sync",
                "event_source": "agent",
                "event_type": "agent_action",
                "action_ids": [f"{block_type}:{ref_id}:{action_name}" for action_name in agent_actions],
                "target_ref": action_target_ref,
                "payload": {
                    "block_ref": ref_id,
                    "agent_actions": list(agent_actions),
                },
                "meta": {},
            })

        return ui_actions, event_bindings

    def build_experience_plan(
        self,
        *,
        paper_id: int,
        focus_page: int,
        user_intent: str,
        reader_profile: str,
        focus_section_ids: Sequence[str],
        compose_payload: Mapping[str, Any],
        generative_plan: Mapping[str, Any],
    ) -> Dict[str, Any]:
        enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
        target_map = self._index_targets(enrichment_bundle)
        page_brief = dict((generative_plan or {}).get("page_brief") or {})
        story_substrate = dict((generative_plan or {}).get("story_substrate") or {})
        resource_modules = [dict(row) for row in list((generative_plan or {}).get("resource_modules") or []) if isinstance(row, Mapping)]
        interaction_modules = [dict(row) for row in list((generative_plan or {}).get("interaction_modules") or []) if isinstance(row, Mapping)]
        widget_blocks = [dict(row) for row in list((generative_plan or {}).get("js_widgets") or []) if isinstance(row, Mapping)]
        rationale = [str(item).strip() for item in list((generative_plan or {}).get("rationale") or []) if str(item).strip()]
        used_tools = [str(item).strip() for item in list((generative_plan or {}).get("used_tools") or []) if str(item).strip()]
        reading_path = [str(item).strip() for item in list(page_brief.get("reading_path") or []) if str(item).strip()]
        primary_focus_target_id = str(page_brief.get("primary_focus_target_id") or "").strip()
        secondary_support_ids = [str(item).strip() for item in list(page_brief.get("secondary_support_target_ids") or []) if str(item).strip()]
        page_archetype = str(page_brief.get("page_archetype") or "").strip() or "finding_digest"
        hero_angle = str(page_brief.get("hero_angle") or "").strip()
        content_budget = dict(page_brief.get("content_budget") or {})
        max_hooks = int(content_budget.get("max_hooks") or 2)
        experience_hooks = self._dedupe_strings(
            [str(item).strip() for item in list(page_brief.get("experience_hooks") or []) if str(item).strip()],
            limit=max_hooks,
        )
        resource_strategy = str(page_brief.get("resource_strategy") or "").strip()
        storyboard = [dict(row) for row in list(page_brief.get("storyboard") or []) if isinstance(row, Mapping)]
        page_brief_meta = dict(page_brief.get("meta") or {})
        include_story_map = bool(page_brief_meta.get("include_story_map"))
        rationale = self._dedupe_strings(rationale, limit=2)

        def _resource_module_score(row: Mapping[str, Any]) -> int:
            target_score = self._module_target_priority(row, primary_focus_target_id)
            link_scores = [
                self._resource_domain_score(str((link or {}).get("href") or ""))
                for link in list(row.get("links") or [])
                if isinstance(link, Mapping)
            ]
            return target_score + (max(link_scores) if link_scores else 0)

        resource_modules = self._prune_ranked_modules(
            resource_modules,
            limit=int(content_budget.get("max_resource_modules") or 2),
            score_fn=_resource_module_score,
            signature_key="module_type",
        )
        raw_question_modules = [
            row for row in interaction_modules
            if str(row.get("module_type") or "").strip() == "QuestionStarterPanel"
        ]
        raw_explainer_modules = [
            row for row in interaction_modules
            if str(row.get("module_type") or "").strip() != "QuestionStarterPanel"
        ]
        question_modules = self._prune_ranked_modules(
            raw_question_modules,
            limit=int(content_budget.get("max_question_modules") or 1),
            score_fn=lambda row: self._module_target_priority(row, primary_focus_target_id),
            signature_key="module_type",
        )
        explainer_modules = self._prune_ranked_modules(
            raw_explainer_modules,
            limit=int(content_budget.get("max_explainer_modules") or 2),
            score_fn=lambda row: self._module_target_priority(row, primary_focus_target_id),
            signature_key="module_type",
        )
        interaction_modules = [*explainer_modules, *question_modules]
        widget_blocks = self._prune_ranked_modules(
            widget_blocks,
            limit=int(content_budget.get("max_widgets") or 1),
            score_fn=lambda row: self._module_target_priority(row, primary_focus_target_id),
            signature_key="widget_type",
        )
        focus_resource_modules: List[Dict[str, Any]] = []
        supporting_resource_modules: List[Dict[str, Any]] = []
        for row in resource_modules:
            target_ids = {str(item or "").strip() for item in list(row.get("target_ids") or []) if str(item or "").strip()}
            module_type = str(row.get("module_type") or "").strip()
            if (
                primary_focus_target_id
                and primary_focus_target_id in target_ids
                and module_type == "FigureExplainPanel"
            ):
                focus_resource_modules.append(row)
                continue
            supporting_resource_modules.append(row)

        focus_widget_blocks: List[Dict[str, Any]] = []
        question_widget_blocks: List[Dict[str, Any]] = []
        for row in widget_blocks:
            target_ids = {str(item or "").strip() for item in list(row.get("target_ids") or []) if str(item or "").strip()}
            widget_type = str(row.get("widget_type") or "").strip()
            if (
                primary_focus_target_id
                and primary_focus_target_id in target_ids
                and widget_type == "figure-focus-accordion"
            ):
                focus_widget_blocks.append(row)
                continue
            question_widget_blocks.append(row)

        focus_target = dict(target_map.get(primary_focus_target_id) or {})
        compose_focus_details = self._extract_compose_target_details(
            compose_payload=compose_payload,
            target_id=primary_focus_target_id,
        )
        if compose_focus_details:
            merged_focus = dict(focus_target)
            for key, value in compose_focus_details.items():
                if value:
                    merged_focus[key] = value
            focus_target = merged_focus
        focus_label = str(
            focus_target.get("figure_label")
            or focus_target.get("title")
            or focus_target.get("section_label")
            or "当前焦点"
        ).strip()
        hero_title = focus_label or f"论文 {int(paper_id)} 展开阅读"
        page_goal = str(page_brief.get("page_goal") or "").strip()
        top_claims = [row for row in list(story_substrate.get("main_claims") or []) if isinstance(row, Mapping)]
        hero_subtitle = ""
        if hero_angle:
            hero_subtitle = self._clean_excerpt(hero_angle, limit=160)
        elif top_claims and not self._is_english_heavy_text(str(top_claims[0].get("text") or "").strip()):
            hero_subtitle = self._clean_excerpt(str(top_claims[0].get("text") or ""), limit=140)
        if not hero_subtitle:
            hero_subtitle = page_goal or "基于清洗后阅读流构建的引导式页面体验。"
        hero_summary = page_goal or hero_subtitle
        if top_claims and not self._is_english_heavy_text(str(top_claims[0].get("text") or "").strip()):
            hero_summary = self._clean_excerpt(
                f"{hero_angle or page_goal} 关键结论：{str(top_claims[0].get('text') or '').strip()}",
                limit=240,
            )
        elif rationale:
            hero_summary = rationale[0]
        if page_archetype == "figure_explainer" and focus_label:
            hero_title = f"{focus_label} 说明了什么"
        elif page_archetype == "methods_decoder":
            hero_title = "这个方法是如何工作的"
        elif page_archetype == "concept_decoder":
            hero_title = "读懂这一页的核心概念"
        elif page_archetype == "finding_digest" and top_claims:
            hero_title = "这一页的关键发现"

        section_candidates = [str(item).strip() for item in list(focus_section_ids or []) if str(item).strip()]
        story_turn_ids: List[str] = []
        for row in list(story_substrate.get("narrative_turns") or []):
            if not isinstance(row, Mapping):
                continue
            for item in list(row.get("target_ids") or []):
                token = str(item or "").strip()
                if token and token not in story_turn_ids:
                    story_turn_ids.append(token)

        reading_targets: List[str] = []
        for item in [primary_focus_target_id, *secondary_support_ids, *section_candidates, *story_turn_ids]:
            token = str(item or "").strip()
            if token and token not in reading_targets:
                reading_targets.append(token)
        if not reading_targets:
            reading_targets = list(target_map.keys())[:6]

        if page_archetype == "figure_explainer":
            section_titles = {
                "focus_stage": "拆解这张图",
                "reading_flow": "阅读支撑正文",
                "explainer_cluster": "读懂关键指标",
                "supporting_resources": "补充背景与上下文",
                "question_lab": "继续往下追问",
            }
        elif page_archetype == "methods_decoder":
            section_titles = {
                "focus_stage": "先看关键设置",
                "reading_flow": "按步骤读方法",
                "explainer_cluster": "解释核心机制",
                "supporting_resources": "补充可靠的方法背景",
                "question_lab": "进一步检验这个方法",
            }
        elif page_archetype == "concept_decoder":
            section_titles = {
                "focus_stage": "先抓住核心例子",
                "reading_flow": "回到原文阅读",
                "explainer_cluster": "把概念讲明白",
                "supporting_resources": "补足外部背景",
                "question_lab": "检查你是否真正理解",
            }
        else:
            section_titles = {
                "focus_stage": "先看最强证据",
                "reading_flow": "阅读关键段落",
                "explainer_cluster": "理解核心想法",
                "supporting_resources": "可靠的补充材料",
                "question_lab": "继续探索",
            }
        section_summaries = {
            section_type: self._compose_section_display_summary(
                section_type=section_type,
                archetype=page_archetype,
                focus_label=focus_label,
                background_topics=page_brief.get("resource_gaps") or [],
                resource_strategy=resource_strategy,
            )
            for section_type in [
                "hero",
                "focus_stage",
                "reading_flow",
                "explainer_cluster",
                "supporting_resources",
                "question_lab",
            ]
        }

        section_entries: Dict[str, Dict[str, Any]] = {
            "hero": {
                "section_id": "hero",
                "section_type": "hero",
                "title": "开场",
                "display_title": "开场",
                "summary": section_summaries["hero"],
                "display_summary": section_summaries["hero"],
                "target_ids": [primary_focus_target_id] if primary_focus_target_id else [],
                "layout_variant": "editorial_hero",
                "resource_module_ids": [],
                "interaction_module_ids": [],
                "widget_ids": [],
                "meta": {"reader_profile": str(reader_profile or "curious_generalist").strip() or "curious_generalist"},
            },
            "focus_stage": {
                "section_id": "focus_stage",
                "section_type": "focus_stage",
                "title": section_titles["focus_stage"],
                "display_title": section_titles["focus_stage"],
                "summary": section_summaries["focus_stage"],
                "display_summary": section_summaries["focus_stage"],
                "target_ids": [primary_focus_target_id] if primary_focus_target_id else [],
                "layout_variant": "figure_plus_support",
                "resource_module_ids": [str(row.get("module_id") or "") for row in focus_resource_modules],
                "interaction_module_ids": [],
                "widget_ids": [str(row.get("widget_id") or "") for row in focus_widget_blocks],
                "meta": {"focus_label": focus_label},
            },
            "reading_flow": {
                "section_id": "reading_flow",
                "section_type": "reading_flow",
                "title": section_titles["reading_flow"],
                "display_title": section_titles["reading_flow"],
                "summary": section_summaries["reading_flow"],
                "display_summary": section_summaries["reading_flow"],
                "target_ids": reading_targets,
                "layout_variant": "prose_stream",
                "resource_module_ids": [],
                "interaction_module_ids": [],
                "widget_ids": [],
                "meta": {"preserve_provenance": True},
            },
        }
        if explainer_modules:
            section_entries["explainer_cluster"] = {
                "section_id": "explainers",
                "section_type": "explainer_cluster",
                "title": section_titles["explainer_cluster"],
                "display_title": section_titles["explainer_cluster"],
                "summary": section_summaries["explainer_cluster"],
                "display_summary": section_summaries["explainer_cluster"],
                "target_ids": sorted({str(item).strip() for row in explainer_modules for item in list(row.get("target_ids") or []) if str(item).strip()}),
                "layout_variant": "stacked_cards",
                "resource_module_ids": [],
                "interaction_module_ids": [str(row.get("module_id") or "") for row in explainer_modules],
                "widget_ids": [],
                "meta": {},
            }
        if supporting_resource_modules:
            section_entries["supporting_resources"] = {
                "section_id": "resources",
                "section_type": "supporting_resources",
                "title": section_titles["supporting_resources"],
                "display_title": section_titles["supporting_resources"],
                "summary": section_summaries["supporting_resources"],
                "display_summary": section_summaries["supporting_resources"],
                "target_ids": sorted({str(item).strip() for row in supporting_resource_modules for item in list(row.get("target_ids") or []) if str(item).strip()}),
                "layout_variant": "resource_shelf",
                "resource_module_ids": [str(row.get("module_id") or "") for row in supporting_resource_modules],
                "interaction_module_ids": [],
                "widget_ids": [],
                "meta": {},
            }
        if question_modules or question_widget_blocks:
            section_entries["question_lab"] = {
                "section_id": "question_lab",
                "section_type": "question_lab",
                "title": section_titles["question_lab"],
                "display_title": section_titles["question_lab"],
                "summary": section_summaries["question_lab"],
                "display_summary": section_summaries["question_lab"],
                "target_ids": sorted({str(item).strip() for row in question_modules + question_widget_blocks for item in list(row.get("target_ids") or []) if str(item).strip()}),
                "layout_variant": "interactive_grid",
                "resource_module_ids": [],
                "interaction_module_ids": [str(row.get("module_id") or "") for row in question_modules],
                "widget_ids": [str(row.get("widget_id") or "") for row in question_widget_blocks],
                "meta": {},
            }
        if include_story_map and (rationale or experience_hooks or used_tools):
            section_entries["story_map"] = {
                "section_id": "story_map",
                "section_type": "story_map",
                "title": "页面幕后",
                "display_title": "页面幕后",
                "summary": "在不打扰主阅读面的前提下，补充这页的叙事意图、阅读钩子和工具决策。",
                "display_summary": "在不打扰主阅读面的前提下，补充这页的叙事意图、阅读钩子和工具决策。",
                "target_ids": sorted({str(item).strip() for item in [primary_focus_target_id, *secondary_support_ids] if str(item).strip()}),
                "layout_variant": "compact_meta",
                "resource_module_ids": [],
                "interaction_module_ids": [],
                "widget_ids": [],
                "meta": {
                    "rationale": rationale[:4],
                    "hooks": experience_hooks[:4],
                    "used_tools": used_tools,
                },
            }

        storyboard_by_section = {
            str(row.get("section_type") or "").strip(): dict(row)
            for row in storyboard
            if str(row.get("section_type") or "").strip()
        }
        for section_type, beat in storyboard_by_section.items():
            if section_type not in section_entries:
                continue
            entry = section_entries[section_type]
            beat_title = str(beat.get("title") or "").strip()
            beat_purpose = str(beat.get("purpose") or "").strip()
            beat_target_ids = [str(item).strip() for item in list(beat.get("target_ids") or []) if str(item).strip()]
            if beat_title:
                entry["title"] = beat_title
                entry["display_title"] = beat_title
            if beat_purpose:
                entry_meta = dict(entry.get("meta") or {})
                entry_meta["planner_purpose"] = beat_purpose
                entry_meta["planner_role"] = str(beat.get("role") or "").strip()
                entry_meta["planner_beat_id"] = str(beat.get("beat_id") or "").strip()
                entry["meta"] = entry_meta
            if beat_target_ids:
                entry["target_ids"] = self._dedupe_strings([*beat_target_ids, *list(entry.get("target_ids") or [])])

        storyboard_sequence = [
            str(row.get("section_type") or "").strip()
            for row in sorted(storyboard, key=lambda row: int(row.get("priority") or 0))
            if str(row.get("section_type") or "").strip() in section_entries
        ]
        ordered_section_types = storyboard_sequence or self._resolve_section_sequence(reading_path, list(section_entries.keys()))
        layout_variant = self._derive_experience_layout_variant(
            page_brief=page_brief,
            has_focus=bool(primary_focus_target_id),
            has_explainers=bool(explainer_modules),
            has_resources=bool(resource_modules),
            has_widgets=bool(widget_blocks),
        )
        ordered_sections: List[Dict[str, Any]] = [
            section_entries[section_type]
            for section_type in ordered_section_types
            if section_type in section_entries
        ]
        main_sections = self._assign_experience_regions(
            ordered_sections,
            layout_variant=layout_variant,
        )

        return self._validate_experience_plan_contract({
            "version": "v1",
            "status": str((generative_plan or {}).get("status") or "done").strip() or "done",
            "scope": "section" if section_candidates else ("page_focus" if focus_page else "paper"),
            "focus_page": int(focus_page),
            "reader_profile": str(reader_profile or "curious_generalist").strip() or "curious_generalist",
            "layout_variant": layout_variant,
            "page_story_title": hero_title,
            "page_story_subtitle": hero_subtitle,
            "narrative_goal": page_goal or hero_summary,
            "hero": {
                "title": hero_title,
                "display_title": self._prefer_display_copy(hero_title, "展开阅读"),
                "subtitle": hero_subtitle,
                "display_subtitle": self._prefer_display_copy(hero_subtitle, page_goal or "围绕这一页组织一个更易读的解释入口。", limit=180),
                "summary": hero_summary,
                "display_summary": self._prefer_display_copy(hero_summary, page_goal or hero_subtitle, limit=240),
                "focus_label": focus_label,
                "target_ids": [primary_focus_target_id] if primary_focus_target_id else [],
                "claim_ids": [str(row.get("claim_id") or "").strip() for row in top_claims[:2] if str(row.get("claim_id") or "").strip()],
                "meta": {
                    "paper_id": int(paper_id),
                    "focus_page": int(focus_page),
                    "hero_angle": hero_angle,
                },
            },
            "main_sections": main_sections,
            "supporting_resources": resource_modules,
            "interactive_blocks": interaction_modules,
            "widget_blocks": [*focus_widget_blocks, *question_widget_blocks],
            "reading_path": list(dict.fromkeys(reading_path or ["hero_summary", "focus_evidence", "reading_flow", "supporting_resources", "explore_questions"])),
            "used_tools": used_tools,
            "meta": {
                "paper_id": int(paper_id),
                "focus_page": int(focus_page),
                "focus_section_ids": section_candidates,
                "user_intent": str(user_intent or "").strip(),
                "derived_from": "generative_reader_plan",
                "page_archetype": page_archetype,
                "experience_hooks": experience_hooks,
                "layout_variant": layout_variant,
                "display_copy_contract": "display_copy_v1",
                "content_budget": content_budget,
                "storyboard": storyboard,
            },
        })

    @classmethod
    def _derive_experience_layout_variant(
        cls,
        *,
        page_brief: Mapping[str, Any],
        has_focus: bool,
        has_explainers: bool,
        has_resources: bool,
        has_widgets: bool,
    ) -> str:
        archetype = str(page_brief.get("page_archetype") or "").strip()
        reading_path = [str(item or "").strip().lower() for item in list(page_brief.get("reading_path") or []) if str(item or "").strip()]
        if archetype == "methods_decoder":
            return "guided_story_stack"
        if archetype == "concept_decoder":
            return "explainer_first"
        if archetype == "figure_explainer" and has_focus:
            return "focus_figure_split"
        if has_explainers and not has_focus and reading_path[:1] == ["context_explainer"]:
            return "explainer_first"
        if has_resources and has_widgets:
            return "resource_augmented_reader"
        if has_focus:
            return "focus_figure_split"
        return "guided_story_stack"

    @classmethod
    def _assign_experience_regions(
        cls,
        ordered_sections: Sequence[Dict[str, Any]],
        *,
        layout_variant: str,
    ) -> List[Dict[str, Any]]:
        footer_types = {"story_map"}
        if layout_variant == "guided_story_stack":
            sidebar_types: set[str] = set()
        elif layout_variant == "explainer_first":
            sidebar_types = {"supporting_resources"}
        elif layout_variant == "focus_figure_split":
            sidebar_types = {"explainer_cluster", "supporting_resources"}
        else:
            sidebar_types = {"explainer_cluster", "supporting_resources"}

        materialized: List[Dict[str, Any]] = []
        for section in ordered_sections:
            cloned = dict(section)
            section_type = str(cloned.get("section_type") or "").strip()
            if section_type in footer_types:
                region = "footer"
            elif section_type in sidebar_types:
                region = "sidebar"
            else:
                region = "main"
            cloned["section_region"] = region
            materialized.append(cloned)
        return materialized

    def build_seed_plan(
        self,
        *,
        page: int,
        user_intent: str,
        compose_payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
        plan = self._build_fallback_plan(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        meta = dict(plan.get("meta") or {})
        meta["seed_plan"] = True
        meta["fallback_reason"] = "seed_plan"
        plan["meta"] = meta
        return plan

    @staticmethod
    def _describe_figure_focus(focus: str, label: str = "") -> str:
        token = str(focus or "").strip().lower()
        if token == "figure_overview":
            return "先把整张图当成进入这一页的主要视觉入口。"
        if token == "concordance_metrics":
            return "突出展示答案与解释之间的一致性，以及模型回答与自身推理相互对齐的程度。"
        if token == "insight_breakdown":
            return "展示不同考试类型和题目表述下，解释性洞见是如何上升或下降的。"
        if token == "prevalence_data":
            return "关注有意义洞见出现的频率，以及最高出现率落在哪些部分。"
        if token:
            return f"聚焦这张图中的 {token.replace('_', ' ')}。"
        clean_label = str(label or "").strip()
        if clean_label:
            return f"查看 {clean_label} 所承载的关键证据。"
        return "查看这个图示面板承载的主要证据。"

    def _normalize_widget_panels(self, parsed: Dict[str, Any]) -> None:
        widgets = list(parsed.get("js_widgets") or [])
        for widget in widgets:
            if not isinstance(widget, dict):
                continue
            if str(widget.get("widget_type") or "").strip() != "figure-focus-accordion":
                continue
            props = widget.get("props")
            if not isinstance(props, dict):
                continue
            panels = list(props.get("panels") or [])
            normalized: List[Dict[str, Any]] = []
            for item in panels:
                if not isinstance(item, Mapping):
                    continue
                label = str(item.get("label") or "").strip()
                summary = str(item.get("summary") or "").strip()
                focus = str(item.get("focus") or "").strip()
                if not summary:
                    summary = self._describe_figure_focus(focus, label)
                normalized.append(
                    {
                        **dict(item),
                        "label": label,
                        "summary": summary,
                    }
                )
            if normalized:
                props["panels"] = normalized
                widget["props"] = props

    @staticmethod
    def _is_generic_figure_focus_panel(panel: Mapping[str, Any]) -> bool:
        label = str(panel.get("label") or "").strip().lower()
        focus = str(panel.get("focus") or "").strip().lower()
        summary = str(panel.get("summary") or "").strip().lower()
        if focus in {"concordance_metrics", "insight_breakdown", "prevalence_data", "figure_overview"}:
            return True
        if label in {
            "panel a: overall concordance",
            "panel b: insight by exam type",
            "panel c: insight frequency",
            "figure overview",
        }:
            return True
        return summary in {
            GenerativeReaderAgentRuntime._describe_figure_focus("concordance_metrics", "Panel A").lower(),
            GenerativeReaderAgentRuntime._describe_figure_focus("insight_breakdown", "Panel B").lower(),
            GenerativeReaderAgentRuntime._describe_figure_focus("prevalence_data", "Panel C").lower(),
            "start with the figure as a whole, then unfold panel-level details only when the source caption clearly supports them.".lower(),
        }

    @staticmethod
    def _extract_focus_panels_from_target(target: Mapping[str, Any]) -> List[Dict[str, str]]:
        raw = " ".join(
            [
                str(target.get("full_text") or "").strip(),
                str(target.get("title") or "").strip(),
                str(target.get("excerpt") or "").strip(),
            ]
        ).strip()
        if not raw:
            return []
        compact = re.sub(r"\s+", " ", raw)
        matches = list(
            re.finditer(
                r"(?<![A-Za-z0-9])([A-D])\s*[:.)-]\s*(.+?)(?=(?<![A-Za-z0-9])[A-D]\s*[:.)-]\s*|$)",
                compact,
                flags=re.IGNORECASE,
            )
        )
        panels: List[Dict[str, str]] = []
        seen_labels: set[str] = set()
        for match in matches:
            label = str(match.group(1) or "").upper().strip()
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            summary = re.sub(r"\s+", " ", str(match.group(2) or "").strip())
            summary = summary.rstrip(" ;,")
            if not summary:
                continue
            panels.append(
                {
                    "label": f"Panel {label}",
                    "focus": f"panel_{label.lower()}",
                    "summary": GenerativeReaderAgentRuntime._clean_excerpt(summary, limit=180),
                }
            )
        return panels

    @staticmethod
    def _flatten_ui_nodes(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        flattened: List[Dict[str, Any]] = []
        for row in list(rows or []):
            if not isinstance(row, Mapping):
                continue
            node = dict(row)
            flattened.append(node)
            children = row.get("children")
            if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
                flattened.extend(GenerativeReaderAgentRuntime._flatten_ui_nodes(children))
        return flattened

    @classmethod
    def _extract_compose_target_details(
        cls,
        *,
        compose_payload: Mapping[str, Any],
        target_id: str,
    ) -> Dict[str, Any]:
        token = str(target_id or "").strip()
        if not token:
            return {}
        node_id = token.split(":")[-1]
        ui_plan = dict((compose_payload or {}).get("ui_plan") or {})
        flat_nodes = cls._flatten_ui_nodes(list(ui_plan.get("components") or []))
        for row in flat_nodes:
            if str(row.get("id") or "").strip() != node_id:
                continue
            node_type = str(row.get("type") or "").strip()
            props = dict(row.get("props") or {})
            if node_type == "FigurePanel":
                source_label = str(props.get("source_label") or "").strip()
                title = str(props.get("title") or "").strip()
                caption = str(props.get("caption") or "").strip()
                full_text = " ".join(part for part in [source_label, title, caption] if part).strip()
                return {
                    "title": title or source_label,
                    "figure_label": source_label,
                    "excerpt": cls._clean_excerpt(caption, limit=280),
                    "full_text": full_text,
                }
            text_parts: List[str] = []
            paragraphs = props.get("paragraphs")
            if isinstance(paragraphs, Sequence) and not isinstance(paragraphs, (str, bytes)):
                for item in paragraphs:
                    if isinstance(item, Mapping):
                        value = str(item.get("text") or "").strip()
                        if value:
                            text_parts.append(value)
            text_value = " ".join(text_parts).strip()
            if text_value:
                return {
                    "title": str(props.get("title") or row.get("title") or "").strip(),
                    "excerpt": cls._clean_excerpt(text_value, limit=280),
                    "full_text": text_value,
                }
            return {}
        return {}

    @staticmethod
    def _build_question_answer_pairs(
        *,
        questions: Sequence[str],
        page_brief: Mapping[str, Any],
        story_substrate: Mapping[str, Any],
    ) -> List[Dict[str, str]]:
        prompts = [str(item or "").strip() for item in list(questions or []) if str(item or "").strip()]
        if not prompts:
            return []
        claims = [row for row in list(story_substrate.get("main_claims") or []) if isinstance(row, Mapping)]
        primary_claim = str((claims[0] or {}).get("text") or "").strip() if claims else ""
        secondary_claim = str((claims[1] or {}).get("text") or "").strip() if len(claims) > 1 else ""
        hero_angle = str(page_brief.get("hero_angle") or "").strip()
        resource_gaps = [str(item or "").strip() for item in list(page_brief.get("resource_gaps") or []) if str(item or "").strip()]
        answers: List[Dict[str, str]] = []
        for idx, question in enumerate(prompts[:4]):
            lower = question.lower()
            answer = ""
            if "goal" in lower or "support" in lower:
                answer = hero_angle or primary_claim or "这一页围绕主图及其支撑正文展开，适合先抓住图，再回到段落理解。"
            elif "main takeaway" in lower or "matter most" in lower:
                answer = primary_claim or secondary_claim or "这一页最重要的信息通常来自焦点图中的结果，以及紧随其后的解释段落。"
            elif "which result" in lower or "supports" in lower:
                answer = primary_claim or "最能支撑本页主结论的，通常是图中的核心证据和正文里的第一段解释。"
            elif "outside context" in lower or "context" in lower:
                if resource_gaps:
                    answer = f"最值得补充的外部背景是 {resource_gaps[0]}，因为它能帮助非专业读者理解这一页，而不是替代论文本身。"
                else:
                    answer = "外部背景最有用的地方，在于解释这一页提到的专业术语、指标或基准体系。"
            else:
                answer = primary_claim or hero_angle or "阅读这一页时，最好先看焦点图，再用周围正文去理解发生了什么变化，以及为什么重要。"
            answers.append(
                {
                    "question": question,
                    "answer": answer,
                    "confidence": "guided",
                }
            )
        return answers

    def _polish_widgets(
        self,
        *,
        widgets: Sequence[Mapping[str, Any]],
        page_brief: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
        compose_payload: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        polished: List[Dict[str, Any]] = []
        primary_focus_target_id = str(page_brief.get("primary_focus_target_id") or "").strip()
        focus_target = dict(target_map.get(primary_focus_target_id) or {})
        compose_focus_details = self._extract_compose_target_details(
            compose_payload=compose_payload or {},
            target_id=primary_focus_target_id,
        )
        if compose_focus_details:
            merged_focus = dict(focus_target)
            for key, value in compose_focus_details.items():
                if value:
                    merged_focus[key] = value
            focus_target = merged_focus
        focus_label = str(focus_target.get("figure_label") or focus_target.get("title") or "").strip()
        archetype = str(page_brief.get("page_archetype") or "").strip()
        for row in list(widgets or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            widget_type = str(item.get("widget_type") or "").strip()
            title = str(item.get("title") or "").strip()
            if widget_type == "figure-focus-accordion":
                if self._is_generic_module_title(title) or title.lower() in {"figure exploration", "explore figure", "explore the figure"}:
                    item["title"] = f"逐面板理解 {focus_label}" if focus_label else "逐面板理解这张图"
                props = dict(item.get("props") or {})
                panels = list(props.get("panels") or [])
                caption_panels = self._extract_focus_panels_from_target(focus_target)
                generic_existing = bool(panels) and all(
                    isinstance(panel, Mapping) and self._is_generic_figure_focus_panel(panel)
                    for panel in panels
                )
                if caption_panels and (generic_existing or len(caption_panels) >= len(panels)):
                    props["panels"] = caption_panels
                    item["props"] = props
                elif not caption_panels and generic_existing:
                    props["panels"] = [
                        {
                            "label": "整图概览",
                            "focus": "figure_overview",
                            "summary": self._clean_excerpt(
                                str(focus_target.get("excerpt") or focus_target.get("title") or "").strip(),
                                limit=220,
                            )
                            or "先把这张图当成进入这一页的主要视觉入口。",
                        }
                    ]
                    item["props"] = props
                meta = dict(item.get("meta") or {})
                if archetype == "figure_explainer":
                    meta.setdefault("story_role", "primary_interactive_guide")
                item["meta"] = meta
            polished.append(item)
        return polished

    @staticmethod
    def _derive_glossary_terms(
        paragraph_target: Optional[Mapping[str, Any]],
        figure_target: Optional[Mapping[str, Any]],
    ) -> List[Dict[str, str]]:
        text = " ".join(
            [
                str((paragraph_target or {}).get("title") or ""),
                str((paragraph_target or {}).get("excerpt") or ""),
                str((figure_target or {}).get("title") or ""),
                str((figure_target or {}).get("excerpt") or ""),
            ]
        ).lower()
        terms: List[Dict[str, str]] = []

        def _append(term: str, definition: str) -> None:
            if any(item.get("term") == term for item in terms):
                return
            terms.append({"term": term, "definition": definition})

        if "concordance" in text:
            _append(
                "Concordance",
                "指模型输出与论文评估标准中的正确答案或解释模式之间的一致程度。",
            )
        if "insight" in text:
            _append(
                "Insight",
                "指超出“答对”本身的、有解释价值的观察；论文会衡量这种洞见出现的频率。",
            )
        if "doi" in text or "density of insight" in text:
            _append(
                "Density of Insight (DOI)",
                "论文中的一个特定指标，用来描述相对于可选答案数量，解释性洞见出现得有多充分。",
            )
        if "usmle" in text:
            _append(
                "USMLE",
                "美国医师执照考试序列，论文在这里把它作为评估模型表现的重要基准。",
            )
        if not terms:
            _append(
                "阅读背景",
                "这个面板补充与当前页面相关的简短定义，帮助读者不离开页面也能继续理解正文。",
            )
        return terms[:4]

    @classmethod
    def _rewrite_generic_reader_copy(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._rewrite_generic_reader_copy(item) for item in value]
        if isinstance(value, Mapping):
            return {str(key): cls._rewrite_generic_reader_copy(val) for key, val in value.items()}
        if not isinstance(value, str):
            return value

        text = str(value or "").strip()
        if not text:
            return value

        lower = text.lower()
        if lower in cls.GENERIC_COPY_REWRITES:
            return cls.GENERIC_COPY_REWRITES[lower]

        replacements = [
            (r"^What (Fig(?:ure)?\s*\d+[A-Za-z]?) reveals$", r"\1 说明了什么"),
            (r"^Start from (Fig(?:ure)?\s*\d+[A-Za-z]?) and use it to interpret the page's core claim:\s*(.+)$", r"从 \1 开始，用它来理解这一页的核心结论：\2"),
            (r"^Start with (Fig(?:ure)?\s*\d+[A-Za-z]?) before reading the supporting passage\.?$", r"先看 \1，再回到支撑正文。"),
            (r"^Expand the panel guide to decode what each sub-figure contributes to the result\.?$", "展开面板引导，理解每个子图分别为结果贡献了什么。"),
            (r"^Use the explainer cards only when a technical term blocks your understanding\.?$", "只有当技术术语真的卡住理解时，再去看解释卡片。"),
            (r"^Help the reader understand the main claim through the figure and key metrics\.?$", "帮助读者通过主图和关键指标理解这一页的核心结论。"),
            (
                r"^Use 1-3 authoritative public resources to clarify (.+) without repeating the paper's argument\.?$",
                r"使用 1 到 3 个权威公开来源补充 \1 的背景，但不要重复论文已经表达的论点。",
            ),
            (
                r"^Open outside context only for (.+), not as a substitute for the paper\.?$",
                r"只在需要补充 \1 这类背景时再打开外部资料，不要拿它替代论文。",
            ),
        ]
        for pattern, replacement in replacements:
            if re.match(pattern, text, flags=re.IGNORECASE):
                return re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return value

    @staticmethod
    def _derive_question_starters(
        *,
        paragraph_target: Optional[Mapping[str, Any]],
        figure_target: Optional[Mapping[str, Any]],
        section_target: Optional[Mapping[str, Any]],
        user_intent: str,
    ) -> List[str]:
        section_label = str((section_target or {}).get("section_label") or (paragraph_target or {}).get("section_label") or "this section").strip()
        figure_label = str((figure_target or {}).get("figure_label") or "the figure").strip()
        intent = str(user_intent or "").strip()
        prompts = [
            f"{figure_label} 对 {section_label.lower()} 的核心结论是什么？",
            "这一页里，哪条结果最直接支撑作者的主结论？",
            "如果要把这一页讲给非专业读者听，最重要的两点是什么？",
        ]
        if intent:
            prompts.insert(0, f"这一页是如何服务于这个阅读目标的：{intent}？")
        return prompts[:4]

    @staticmethod
    def _parse_json_like(text: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(str(text or "").strip())
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @classmethod
    def _extract_public_links_from_tool_trace(cls, tool_trace: Sequence[Mapping[str, Any]], limit: int = 4) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        seen: set[str] = set()

        def _append(label: str, href: str, snippet: str = "") -> None:
            clean_href = str(href or "").strip()
            if not clean_href or clean_href in seen:
                return
            seen.add(clean_href)
            row: Dict[str, str] = {"label": str(label or clean_href).strip()[:120], "href": clean_href}
            if snippet:
                row["snippet"] = cls._clean_excerpt(snippet, limit=180)
            links.append(row)

        for row in list(tool_trace or []):
            if len(links) >= limit:
                break
            if not isinstance(row, Mapping) or str(row.get("type") or "").strip() != "observation":
                continue
            data = row.get("data")
            if not isinstance(data, Mapping) or not bool(data.get("success")):
                continue
            tool_name = str(data.get("tool") or "").strip()
            payload = data.get("data") if isinstance(data.get("data"), Mapping) else {}
            output_text = str(data.get("output") or "").strip()

            if tool_name == "web_search":
                structured = payload.get("structured_content") if isinstance(payload, Mapping) else {}
                results = []
                if isinstance(structured, Mapping):
                    maybe_results = structured.get("results")
                    if isinstance(maybe_results, list):
                        results = maybe_results
                if not results and output_text.startswith("{"):
                    parsed = cls._parse_json_like(output_text)
                    maybe_results = parsed.get("results") if isinstance(parsed, dict) else None
                    if isinstance(maybe_results, list):
                        results = maybe_results
                for item in results:
                    if not isinstance(item, Mapping):
                        continue
                    _append(
                        str(item.get("title") or item.get("url") or "Public resource"),
                        str(item.get("url") or ""),
                        str(item.get("snippet") or ""),
                    )
                    if len(links) >= limit:
                        break
                continue

            if tool_name == "web_scrape":
                source_url = ""
                if isinstance(data.get("input"), Mapping):
                    source_url = str((data.get("input") or {}).get("url") or "").strip()
                label = "Scraped source"
                structured = payload.get("structured_content") if isinstance(payload, Mapping) else {}
                if isinstance(structured, Mapping):
                    metadata = structured.get("metadata")
                    if isinstance(metadata, Mapping):
                        label = str(metadata.get("title") or label).strip() or label
                _append(label, source_url, str(data.get("output") or ""))

        return cls._normalize_public_links(links, limit=limit)

    @classmethod
    def _normalize_resource_modules(cls, modules: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for row in list(modules or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            links = cls._normalize_public_links(list(item.get("links") or []), limit=3)
            if links:
                item["links"] = links
            normalized.append(item)
        return normalized

    @staticmethod
    def _is_generic_module_title(title: str) -> bool:
        token = str(title or "").strip().lower()
        return token in {
            "",
            "related resources",
            "glossary and background",
            "glossary",
            "figure explainer",
            "figure exploration",
            "suggested follow-up questions",
        }

    def _polish_resource_modules(
        self,
        *,
        modules: Sequence[Mapping[str, Any]],
        page_brief: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized = self._normalize_resource_modules(modules)
        archetype = str(page_brief.get("page_archetype") or "").strip()
        resource_strategy = str(page_brief.get("resource_strategy") or "").strip()
        polished: List[Dict[str, Any]] = []
        for row in normalized:
            item = dict(row)
            module_type = str(item.get("module_type") or "").strip()
            target_ids = [str(item).strip() for item in list(item.get("target_ids") or []) if str(item).strip()]
            primary_target = dict(target_map.get(target_ids[0]) or {}) if target_ids else {}
            focus_label = str(primary_target.get("figure_label") or primary_target.get("title") or "").strip()
            section_label = str(primary_target.get("section_label") or "").strip()
            title = str(item.get("title") or "").strip()
            summary = self._clean_excerpt(str(item.get("summary") or "").strip(), limit=220)
            links = list(item.get("links") or [])
            high_value_links = [
                row for row in links
                if self._resource_domain_score(str((row or {}).get("href") or "")) >= 50
            ]

            if module_type == "FigureExplainPanel":
                if self._is_generic_module_title(title):
                    item["title"] = f"如何阅读 {focus_label}" if focus_label else "如何阅读关键图示"
                if not summary or summary.lower().startswith("summarize the figure"):
                    item["summary"] = (
                        f"把 {focus_label or '这张图'} 当成这页的主要视觉锚点，并把每个面板重新连回论文的核心结论。"
                    )
            elif module_type == "RelatedResourceCard":
                if self._is_generic_module_title(title):
                    if high_value_links:
                        trusted_domains = [
                            self._extract_hostname(str((row or {}).get("href") or ""))
                            for row in high_value_links
                        ]
                        first_domain = trusted_domains[0] if trusted_domains else ""
                        if any(domain.endswith("usmle.org") for domain in trusted_domains):
                            item["title"] = "USMLE 官方背景"
                        elif first_domain:
                            item["title"] = "可靠的外部背景"
                        elif section_label:
                            item["title"] = f"{section_label} 的背景补充"
                        else:
                            item["title"] = "可靠背景"
                    elif links:
                        first_domain = self._extract_hostname(str((links[0] or {}).get("href") or ""))
                        if first_domain.endswith("usmle.org"):
                            item["title"] = "USMLE 官方背景"
                        elif first_domain:
                            item["title"] = "背景参考"
                        elif section_label:
                            item["title"] = f"{section_label} 的背景补充"
                        else:
                            item["title"] = "值得打开的背景资料"
                    elif section_label:
                        item["title"] = f"{section_label} 的背景补充"
                    else:
                        item["title"] = "值得打开的背景资料"
                if not summary or summary.lower().startswith("attach a small set"):
                    if high_value_links and resource_strategy:
                        item["summary"] = resource_strategy
                    elif high_value_links:
                        item["summary"] = "打开一到两个可靠的外部来源，补充理解所需背景，但不替代论文正文。"
                    elif archetype == "figure_explainer":
                        item["summary"] = "补充一到两个公共来源，帮助非专业读者读懂这张图，但不替代论文。"
                    else:
                        item["summary"] = "补充少量公共来源，帮助你更好理解这一段正文。"

                meta = dict(item.get("meta") or {})
                if high_value_links:
                    meta["source_quality"] = "trusted"
                    item["links"] = high_value_links[:3]
                elif links:
                    meta["source_quality"] = "limited"
                    item["links"] = links[:2]
                else:
                    meta["source_quality"] = "none"
                item["meta"] = meta
            polished.append(item)
        return polished

    def _polish_interaction_modules(
        self,
        *,
        modules: Sequence[Mapping[str, Any]],
        page_brief: Mapping[str, Any],
        story_substrate: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        archetype = str(page_brief.get("page_archetype") or "").strip()
        polished: List[Dict[str, Any]] = []
        for row in list(modules or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            module_type = str(item.get("module_type") or "").strip()
            title = str(item.get("title") or "").strip()
            if module_type == "GlossaryPanel" and self._is_generic_module_title(title):
                item["title"] = "读懂这一页的关键术语" if archetype != "methods_decoder" else "把方法术语讲明白"
            if module_type == "QuestionStarterPanel" and self._is_generic_module_title(title):
                item["title"] = "接下来值得追问的问题"
            if module_type == "QuestionStarterPanel":
                props = dict(item.get("props") or {})
                questions = [str(entry or "").strip() for entry in list(props.get("questions") or []) if str(entry or "").strip()]
                qa_pairs = [row for row in list(props.get("qa_pairs") or []) if isinstance(row, Mapping)]
                if questions and not qa_pairs:
                    props["qa_pairs"] = self._build_question_answer_pairs(
                        questions=questions,
                        page_brief=page_brief,
                        story_substrate=story_substrate,
                    )
                    item["props"] = props
            polished.append(item)
        return polished

    def _recover_plan_deterministically(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        compose_payload: Optional[Mapping[str, Any]] = None,
        tool_trace: Sequence[Mapping[str, Any]],
        used_tools: Sequence[str],
    ) -> Optional[Dict[str, Any]]:
        links = self._extract_public_links_from_tool_trace(tool_trace)
        if not links and not list(used_tools or []):
            return None

        recovered = self._build_fallback_plan(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        recovered["status"] = "done"
        modules = [row for row in list(recovered.get("resource_modules") or []) if isinstance(row, dict)]
        if modules:
            modules[0]["links"] = links[:2]
            modules[0]["source"] = "web" if links else "fallback"
            meta = dict(modules[0].get("meta") or {})
            meta.setdefault("notes", "Recovered from tool trace after agent timeout.")
            modules[0]["meta"] = meta
        if len(modules) > 1:
            modules[1]["links"] = links[2:4]
            modules[1]["source"] = "web" if len(links) > 2 else "fallback"
        recovered["resource_modules"] = self._normalize_resource_modules(modules)
        return self._finalize_plan(
            parsed=recovered,
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
            used_tools=used_tools,
            tool_trace=tool_trace,
        )

    def _build_agent_prompt(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        compose_payload: Mapping[str, Any],
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> str:
        compact_targets = self._compact_enrichment_targets(enrichment_bundle)
        grounding_hints = self._derive_reader_grounding_hints(enrichment_bundle)
        scheme_choice = dict((compose_payload or {}).get("scheme_choice") or {})
        quality_report = dict((compose_payload or {}).get("quality_report") or {})
        page_assets = [
            {
                "kind": str(item.get("kind") or "").strip(),
                "label": str(item.get("label") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "href": str(item.get("href") or "").strip(),
            }
            for item in list((compose_payload or {}).get("assets") or [])[:12]
            if isinstance(item, Mapping)
        ]
        adjacent_refs = [
            {
                "page": int(item.get("page") or 0),
                "relation": str(item.get("relation") or "").strip(),
                "reference_only": bool(item.get("reference_only")),
                "source": str(item.get("source") or "").strip(),
                "text": self._clean_excerpt(str(item.get("text") or "").strip(), limit=1200),
            }
            for item in list(adjacent_page_context or [])
            if isinstance(item, Mapping) and str(item.get("text") or "").strip()
        ]
        output_schema = {
            "version": "v1",
            "status": "done",
            "shell_mode": "resource_augmented_reader",
            "story_substrate": {
                "page_id": "p7",
                "main_claims": [{"claim_id": "claim_1", "text": "core takeaway", "source_target_ids": ["p7:paragraph_1"], "strength": "primary"}],
                "evidence_units": [{"evidence_id": "evidence_1", "kind": "figure", "role": "primary_visual_evidence", "title": "Fig 3", "source_target_ids": ["p7:figure_1"]}],
                "terms_to_explain": [{"term": "Concordance", "reason": "domain_specific_term", "source_target_ids": ["p7:paragraph_1"]}],
                "background_gaps": [{"topic": "USMLE structure", "reason": "reader may lack context", "suggested_resource_type": "related_public_resource"}],
                "narrative_turns": [{"turn_id": "turn_1", "kind": "figure_focus", "label": "Start from Fig 3", "target_ids": ["p7:figure_1"]}],
                "meta": {}
            },
            "page_brief": {
                "page_goal": "Help the reader understand the main claim through the figure and key metrics.",
                "reader_type": "curious_generalist",
                "page_archetype": "figure_explainer",
                "hero_angle": "Start from Figure 3, then use the key metrics to decode what the page is really claiming.",
                "primary_focus_target_id": "p7:figure_1",
                "secondary_support_target_ids": ["p7:paragraph_1"],
                "reading_path": ["hero_summary", "focus_evidence", "reading_flow", "context_explainer", "supporting_resources", "explore_questions"],
                "interaction_opportunities": ["expand_focus_panels", "open_term_glossary"],
                "resource_gaps": ["USMLE structure"],
                "experience_hooks": [
                    "Start with the figure before reading the supporting prose.",
                    "Open the glossary only when a metric blocks your understanding."
                ],
                "resource_strategy": "Use 1-3 authoritative public resources to clarify USMLE context without repeating the paper's argument.",
                "storyboard": [
                    {"beat_id": "beat_hero", "role": "orient", "section_type": "hero", "title": "开场", "purpose": "Establish the reading goal first.", "target_ids": ["p7:figure_1"], "priority": 1},
                    {"beat_id": "beat_focus", "role": "focus_evidence", "section_type": "focus_stage", "title": "拆解这张图", "purpose": "Use the strongest evidence as the anchor.", "target_ids": ["p7:figure_1"], "priority": 2},
                    {"beat_id": "beat_read", "role": "read_support", "section_type": "reading_flow", "title": "阅读支撑正文", "purpose": "Keep the cleaned paper prose as the main reading flow.", "target_ids": ["p7:paragraph_1"], "priority": 3}
                ],
                "content_budget": {"max_claim_cards": 2, "max_hooks": 2, "max_resource_modules": 2, "max_explainer_modules": 2, "max_question_modules": 1, "max_widgets": 1},
                "meta": {"include_story_map": False}
            },
            "rationale": ["why this module arrangement helps reading"],
            "resource_modules": [
                {
                    "module_id": "res_001",
                    "module_type": "RelatedResourceCard",
                    "target_ids": ["p7:paragraph_1"],
                    "title": "Related resources",
                    "summary": "Short explanation",
                    "links": [{"label": "example", "href": "https://example.com"}],
                    "source": "web",
                    "interaction_mode": "stacked_cards",
                    "meta": {"priority": "medium"},
                }
            ],
            "interaction_modules": [
                {
                    "module_id": "int_001",
                    "module_type": "GlossaryPanel",
                    "target_ids": ["p7:paragraph_1"],
                    "title": "Glossary",
                    "props": {"terms": ["insight density"]},
                    "source": "agent",
                    "meta": {},
                }
            ],
            "js_widgets": [
                {
                    "widget_id": "widget_001",
                    "widget_type": "figure-focus-accordion",
                    "target_ids": ["p7:figure_1"],
                    "title": "Figure exploration",
                    "data_requirements": ["figure_explainer"],
                    "props": {
                        "panels": [
                            {
                                "label": "Panel A",
                                "focus": "concordance_metrics",
                                "summary": "What the panel shows and why it matters for the page's main takeaway."
                            }
                        ]
                    },
                    "meta": {},
                }
            ],
            "used_tools": ["paper_read"],
            "tool_trace": [],
            "meta": {"notes": "optional"},
        }
        return (
            "Design a generative reader enhancement plan for one paper page.\n"
            "Return JSON only.\n"
            "This is NOT body extraction. The reading flow already exists.\n"
            "Your job is to first infer the page story, then add external/public-resource modules and interactive JS widgets around the existing reading flow.\n"
            "Hard rules:\n"
            "1) Never rewrite or replace the body content.\n"
            "2) Only target existing enrichment targets.\n"
            "2.5) Infer a compact story_substrate and page_brief before deciding modules. Think in terms of page experience, not a pile of cards.\n"
            "3) Prefer modules that clarify the page: glossary, related public resources, figure explainer, methods explainer, contrast module, question starter.\n"
            "4) Keep metadata/DOI/header/footer out of the plan unless they materially support a resource module.\n"
            "5) If you use tool output, summarize it conservatively and do not invent facts.\n"
            "6) JS widgets should be lightweight interactive modules, not full-page rewrites.\n"
            "7) Focus on reading enhancement, not decoration.\n"
            "8) The final answer must be valid JSON matching the schema example.\n"
            "9) Tools are optional. Choose them autonomously based on what the page needs.\n"
            "10) Use the smallest useful set of tools; do not call tools mechanically just because they are available.\n"
            "11) Reader-native tools can help ground the page, while web_search/web_scrape can help attach public resources; choose what is proportionate.\n"
            "12) If the page references systems such as exams, benchmarks, institutions, training pathways, or evaluation frameworks, strongly consider attaching 1-3 authoritative public resources that help an unfamiliar reader understand the page.\n"
            "13) Prefer official or primary sources for public resources; avoid generic low-value links.\n"
            "14) If you keep an external/public URL in a resource module, use web_scrape when it materially improves confidence in the summary.\n"
            "15) If scrape is unavailable or unnecessary, keep the link but say the summary is search-derived in meta.notes rather than pretending it was deeply read.\n"
            "16) JS widget panels must include reader-facing detail; do not leave figure-focus accordion panels without summaries.\n"
            "17) Keep the plan compact: usually no more than 2 resource modules, 2 interaction modules, and 1 JS widget.\n"
            "18) Do not call the same tool repeatedly for near-duplicate queries; stop once the top targets are grounded enough to draft the JSON.\n"
            "19) Keep internal reasoning, tool usage, and schema handling in English if helpful, but generate all user-facing copy in Simplified Chinese.\n"
            "20) For reader-visible fields such as titles, summaries, labels, questions, answers, hooks, and explanatory text, reply in Simplified Chinese.\n"
            "21) If adjacent_page_context is provided, treat it as reference-only continuity context. It may help resolve sentence continuation or section carry-over, but it must never override the current page as the primary evidence source.\n"
            f"user_intent={json.dumps(str(user_intent or '').strip(), ensure_ascii=False)}\n"
            f"page={int(page)}\n"
            f"scheme_choice={json.dumps(scheme_choice, ensure_ascii=False)}\n"
            f"quality_report={json.dumps({'overall': quality_report.get('overall'), 'layout_monotony': quality_report.get('layout_monotony')}, ensure_ascii=False)}\n"
            f"page_assets={json.dumps(page_assets, ensure_ascii=False)}\n"
            f"enrichment_targets={json.dumps(compact_targets, ensure_ascii=False)}\n"
            f"reader_grounding_hints={json.dumps(grounding_hints, ensure_ascii=False)}\n"
            f"adjacent_page_context={json.dumps(adjacent_refs, ensure_ascii=False)}\n"
            f"output_schema_example={json.dumps(output_schema, ensure_ascii=False)}\n"
        )

    def _finalize_plan(
        self,
        *,
        parsed: Dict[str, Any],
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        compose_payload: Optional[Mapping[str, Any]] = None,
        used_tools: Sequence[str],
        tool_trace: Sequence[Mapping[str, Any]],
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        parsed.setdefault("version", "v1")
        parsed.setdefault("status", "done")
        parsed.setdefault("shell_mode", "resource_augmented_reader")
        story_substrate = parsed.get("story_substrate")
        if not isinstance(story_substrate, Mapping):
            story_substrate = self._build_story_substrate(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
            )
        page_brief = parsed.get("page_brief")
        if not isinstance(page_brief, Mapping):
            page_brief = self._build_page_brief(
                page=int(page),
                user_intent=user_intent,
                story_substrate=story_substrate,
                enrichment_bundle=enrichment_bundle,
            )
        parsed["story_substrate"] = dict(story_substrate)
        parsed["page_brief"] = dict(page_brief)
        parsed.setdefault("rationale", [])
        parsed.setdefault("resource_modules", [])
        parsed.setdefault("interaction_modules", [])
        parsed.setdefault("js_widgets", [])
        target_map = self._index_targets(enrichment_bundle)
        parsed["resource_modules"] = self._polish_resource_modules(
            modules=parsed.get("resource_modules") or [],
            page_brief=page_brief,
            target_map=target_map,
        )
        parsed["interaction_modules"] = self._polish_interaction_modules(
            modules=parsed.get("interaction_modules") or [],
            page_brief=page_brief,
            story_substrate=story_substrate,
        )
        self._normalize_widget_panels(parsed)
        parsed["js_widgets"] = self._polish_widgets(
            widgets=parsed.get("js_widgets") or [],
            page_brief=page_brief,
            target_map=target_map,
            compose_payload=compose_payload,
        )
        if not self._has_meaningful_modules(parsed):
            fallback_seed = self._build_fallback_plan(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
            )
            parsed["resource_modules"] = self._polish_resource_modules(
                modules=fallback_seed.get("resource_modules") or [],
                page_brief=page_brief,
                target_map=target_map,
            )
            parsed["interaction_modules"] = self._polish_interaction_modules(
                modules=fallback_seed.get("interaction_modules") or [],
                page_brief=page_brief,
                story_substrate=story_substrate,
            )
            fallback_widgets = list(fallback_seed.get("js_widgets") or [])
            fallback_payload = {"js_widgets": fallback_widgets}
            self._normalize_widget_panels(fallback_payload)
            parsed["js_widgets"] = self._polish_widgets(
                widgets=fallback_payload.get("js_widgets") or [],
                page_brief=page_brief,
                target_map=target_map,
                compose_payload=compose_payload,
            )
        parsed["story_substrate"] = self._rewrite_generic_reader_copy(parsed.get("story_substrate") or {})
        parsed["page_brief"] = self._rewrite_generic_reader_copy(parsed.get("page_brief") or {})
        parsed["rationale"] = self._rewrite_generic_reader_copy(list(parsed.get("rationale") or []))
        parsed["resource_modules"] = self._rewrite_generic_reader_copy(list(parsed.get("resource_modules") or []))
        parsed["interaction_modules"] = self._rewrite_generic_reader_copy(list(parsed.get("interaction_modules") or []))
        parsed["js_widgets"] = self._rewrite_generic_reader_copy(list(parsed.get("js_widgets") or []))
        parsed["story_substrate"] = self._materialize_story_display_copy(
            parsed.get("story_substrate") or {},
            target_map=target_map,
        )
        parsed["resource_modules"] = self._materialize_resource_display_copy(
            parsed.get("resource_modules") or [],
            page_brief=parsed.get("page_brief") or {},
            target_map=target_map,
        )
        parsed["interaction_modules"] = self._materialize_interaction_display_copy(
            parsed.get("interaction_modules") or [],
            page_brief=parsed.get("page_brief") or {},
        )
        parsed["js_widgets"] = self._materialize_widget_display_copy(
            parsed.get("js_widgets") or [],
            target_map=target_map,
        )
        parsed["used_tools"] = list(dict.fromkeys([str(item).strip() for item in list(used_tools or []) if str(item).strip()]))
        parsed["tool_trace"] = list(tool_trace or [])
        meta = dict(parsed.get("meta") or {})
        meta.setdefault("page", int(page))
        meta.setdefault("user_intent", str(user_intent or "").strip())
        meta.setdefault("target_count", len(list(enrichment_bundle.get("targets") or [])))
        used_tool_set = {str(item).strip() for item in list(used_tools or []) if str(item).strip()}
        if used_tool_set and any(name in used_tool_set for name in self.READER_NATIVE_TOOLS):
            meta.setdefault("grounding_strategy", "reader_native_assist")
        elif used_tool_set:
            meta.setdefault("grounding_strategy", "web_only")
        else:
            meta.setdefault("grounding_strategy", "no_tools")
        if "web_scrape" in used_tool_set:
            meta.setdefault("public_resource_grounding", "search_plus_scrape")
        elif "web_search" in used_tool_set:
            meta.setdefault("public_resource_grounding", "search_only")
        else:
            meta.setdefault("public_resource_grounding", "reader_native_only")
        adjacent_refs = [
            {
                "page": int(item.get("page") or 0),
                "relation": str(item.get("relation") or "").strip(),
                "reference_only": bool(item.get("reference_only")),
                "source": str(item.get("source") or "").strip(),
            }
            for item in list(adjacent_page_context or [])
            if isinstance(item, Mapping) and int(item.get("page") or 0) > 0
        ]
        if adjacent_refs:
            meta["adjacent_page_context"] = adjacent_refs
        meta.setdefault("display_copy_contract", "display_copy_v1")
        if not self._has_meaningful_modules(parsed):
            meta.setdefault("fallback_reason", "empty_module_plan")
        parsed["meta"] = meta
        return self._validate_generative_plan_contract(
            parsed=parsed,
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )

    async def build_plan(
        self,
        *,
        user_id: int,
        page: int,
        user_intent: str,
        compose_payload: Mapping[str, Any],
        tool_registry: Optional[Any] = None,
        allowed_tool_names: Optional[Sequence[str]] = None,
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
        fallback_plan = self._build_fallback_plan(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )

        allowed_tools = {str(item).strip() for item in list(allowed_tool_names or []) if str(item).strip()}
        if not allowed_tools:
            allowed_tools = resolve_generative_reader_agent_tool_whitelist()
        if not allowed_tools:
            return self._finalize_plan(
                parsed=fallback_plan,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                used_tools=[],
                tool_trace=[],
                adjacent_page_context=adjacent_page_context,
            )

        llm = await self._build_llm()
        registry = tool_registry or build_generative_reader_tool_registry(
            user_id=int(user_id),
            allowed_tool_names=sorted(list(allowed_tools)),
        )
        agent = GenerativeReaderAgentCore(
            llm_service=llm,
            tool_registry=registry,
            allowed_tool_names=sorted(list(allowed_tools)),
            max_iterations=max(2, min(int(getattr(settings, "generative_reader_agent_max_iterations", 6) or 6), 4)),
            runtime_context=AgentRuntimeContext(
                user_id=int(user_id),
                channel="generative_reader",
                conversation_id=None,
                notebook_id=None,
            ),
        )
        prompt = self._build_agent_prompt(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
            adjacent_page_context=adjacent_page_context,
        )
        answer_text = ""
        tool_trace: List[Dict[str, Any]] = []
        used_tools: List[str] = []
        agent_error_message = ""
        try:
            async def _run() -> None:
                nonlocal answer_text, agent_error_message
                async for event in agent.run(messages=[{"role": "user", "content": prompt}], stream=True):
                    et = str((event or {}).get("type") or "")
                    data = (event or {}).get("data")
                    if et == "action" and isinstance(data, dict):
                        tool_name = str(data.get("tool") or "").strip()
                        if tool_name:
                            used_tools.append(tool_name)
                        tool_trace.append({"type": et, "data": data})
                    elif et == "observation":
                        tool_trace.append({"type": et, "data": data})
                    elif et == "answer":
                        answer_text = str(data or "")
                    elif et == "error":
                        agent_error_message = str(data or "").strip()
                        tool_trace.append({"type": et, "data": data})
                    elif et == "done" and isinstance(data, dict):
                        done_answer = str(data.get("answer") or "").strip()
                        if done_answer:
                            answer_text = done_answer

            timeout_seconds = max(10.0, float(getattr(settings, "generative_reader_agent_timeout_seconds", 150) or 150))
            await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            recovered = await self._recover_plan_from_tool_trace(
                llm=llm,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                tool_trace=tool_trace,
            )
            if isinstance(recovered, dict):
                recovered = self._finalize_plan(
                    parsed=recovered,
                    page=int(page),
                    user_intent=user_intent,
                    enrichment_bundle=enrichment_bundle,
                    compose_payload=compose_payload,
                    used_tools=used_tools,
                    tool_trace=tool_trace,
                    adjacent_page_context=adjacent_page_context,
                )
                recovered_meta = dict(recovered.get("meta") or {})
                recovered_meta.pop("fallback_reason", None)
                recovered_meta.setdefault("recovered_from", "agent_timeout")
                recovered["meta"] = recovered_meta
                return recovered
            deterministic = self._recover_plan_deterministically(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                tool_trace=tool_trace,
                used_tools=used_tools,
            )
            if isinstance(deterministic, dict):
                deterministic_meta = dict(deterministic.get("meta") or {})
                deterministic_meta.pop("fallback_reason", None)
                deterministic_meta.setdefault("recovered_from", "agent_timeout_deterministic")
                deterministic["meta"] = deterministic_meta
                return deterministic
            fallback_plan["meta"]["fallback_reason"] = "agent_timeout"
            fallback_plan["tool_trace"] = tool_trace
            fallback_plan["used_tools"] = list(dict.fromkeys(used_tools))
            return self._finalize_plan(
                parsed=fallback_plan,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                used_tools=used_tools,
                tool_trace=tool_trace,
                adjacent_page_context=adjacent_page_context,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[GenerativeReaderAgentRuntime] build_plan failed: {exc}")
            fallback_plan["meta"]["fallback_reason"] = "agent_exception"
            fallback_plan["tool_trace"] = tool_trace
            fallback_plan["used_tools"] = list(dict.fromkeys(used_tools))
            return self._finalize_plan(
                parsed=fallback_plan,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                used_tools=used_tools,
                tool_trace=tool_trace,
                adjacent_page_context=adjacent_page_context,
            )

        if agent_error_message and not str(answer_text or "").strip():
            fallback_plan["meta"]["fallback_reason"] = self._classify_failure_reason(agent_error_message)
            fallback_plan["meta"]["agent_error_message"] = agent_error_message[:400]
            fallback_plan["tool_trace"] = tool_trace
            fallback_plan["used_tools"] = list(dict.fromkeys(used_tools))
            return self._finalize_plan(
                parsed=fallback_plan,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                used_tools=used_tools,
                tool_trace=tool_trace,
                adjacent_page_context=adjacent_page_context,
            )

        parsed = self._extract_json_dict(answer_text)
        if not isinstance(parsed, dict):
            fallback_plan["meta"]["fallback_reason"] = (
                self._classify_failure_reason(agent_error_message)
                if agent_error_message
                else "agent_answer_not_json"
            )
            if agent_error_message:
                fallback_plan["meta"]["agent_error_message"] = agent_error_message[:400]
            fallback_plan["tool_trace"] = tool_trace
            fallback_plan["used_tools"] = list(dict.fromkeys(used_tools))
            return self._finalize_plan(
                parsed=fallback_plan,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                used_tools=used_tools,
                tool_trace=tool_trace,
            )

        return self._finalize_plan(
            parsed=parsed,
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
            used_tools=used_tools,
            tool_trace=tool_trace,
            adjacent_page_context=adjacent_page_context,
        )


_runtime: Optional[GenerativeReaderAgentRuntime] = None


def get_generative_reader_agent_runtime() -> GenerativeReaderAgentRuntime:
    global _runtime
    if _runtime is None:
        _runtime = GenerativeReaderAgentRuntime()
    return _runtime
