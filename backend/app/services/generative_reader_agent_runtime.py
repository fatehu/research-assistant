"""
Generative reader agent runtime.

Builds a structured generative plan on top of an existing reader compose payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from html import unescape
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
    PREPRINT_RESOURCE_DOMAINS = (
        "medrxiv.org",
        "biorxiv.org",
        "arxiv.org",
        "osf.io",
        "ssrn.com",
        "researchsquare.com",
    )
    LOW_VALUE_RESOURCE_DOMAINS = (
        "usmlestrike.com",
        "academically.com",
        "csdn.net",
        "medium.com",
        "blogspot.com",
        "wordpress.com",
        "wikipedia.org",
        "researchgate.net",
        "oreate.ai",
        "oreateai.com",
        "zhihu.com",
        "linkedin.com",
        "weproedu.com",
        "medvily.com",
        "medtigo.com",
        "facebook.com",
        "baigemed.com",
        "system.com",
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
        "read the supporting passage": "完整阅读本页内容",
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
            return latin_count >= max(48, cjk_count * 6)
        if short_form:
            return latin_count >= 6
        return latin_count >= 24 and latin_count > cjk_count * 4

    @staticmethod
    def _resolve_target_display_label(target: Mapping[str, Any]) -> str:
        return str(
            target.get("figure_label")
            or target.get("title")
            or target.get("section_label")
            or ""
        ).strip()

    @classmethod
    def _trim_terminal_punctuation(cls, text: str) -> str:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=220)
        if not clean:
            return ""
        return re.sub(r"[。！？!?；;：:，,.\s]+$", "", clean).strip()

    @staticmethod
    def _join_display_terms(terms: Sequence[str]) -> str:
        tokens = [str(item or "").strip() for item in list(terms or []) if str(item or "").strip()]
        if not tokens:
            return ""
        if len(tokens) == 1:
            return tokens[0]
        if len(tokens) == 2:
            return f"{tokens[0]}和{tokens[1]}"
        return "、".join(tokens[:-1]) + f"和{tokens[-1]}"

    @classmethod
    def _extract_grounding_terms(cls, text: str, *, limit: int = 4) -> List[str]:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=280), limit=280)
        if not clean:
            return []

        patterns = (
            (r"洞见出现频率", "洞见出现频率"),
            (r"洞见出现比例", "洞见出现比例"),
            (r"一致性和洞见", "一致性和洞见"),
            (r"题目编码方式", "题目编码方式"),
            (r"考试类型", "考试类型"),
            (r"解释质量", "解释质量"),
            (r"非显而易见(?:性)?", "非显而易见性"),
            (r"新颖性", "新颖性"),
            (r"有效性", "有效性"),
            (r"一致性", "一致性"),
            (r"洞见", "洞见"),
            (r"\b(?:frequency of insight|insight frequency)\b", "洞见出现频率"),
            (r"\b(?:insight prevalence|prevalence of insight)\b", "洞见出现比例"),
            (r"\b(?:concordance and insight|insight and concordance)\b", "一致性和洞见"),
            (r"\bquestion encoding formats?\b", "题目编码方式"),
            (r"\bexam types?\b", "考试类型"),
            (r"\bexplanation quality\b", "解释质量"),
            (r"\bnon[- ]obvious(?:ness)?\b", "非显而易见性"),
            (r"\bnovelty\b", "新颖性"),
            (r"\bvalidity\b", "有效性"),
            (r"\bconcordance\b", "一致性"),
            (r"\binsights?\b", "洞见"),
            (r"\bfrequency\b", "出现频率"),
            (r"\bprevalence\b", "出现比例"),
            (r"\bChatGPT\b", "ChatGPT"),
            (r"\bUSMLE\b", "USMLE"),
            (r"\bStep\s*1\b", "Step 1"),
            (r"\bStep\s*2(?:CK)?\b", "Step 2"),
            (r"\bStep\s*3\b", "Step 3"),
        )

        ranked: List[tuple[int, str]] = []
        for pattern, replacement in patterns:
            match = re.search(pattern, clean, flags=re.IGNORECASE)
            if match:
                ranked.append((match.start(), replacement))
        for match in re.finditer(r"(?<!\d)\d+(?:\.\d+)?%(?!\d)", clean):
            ranked.append((match.start(), match.group(0)))

        ranked.sort(key=lambda item: item[0])
        ordered = cls._dedupe_strings([replacement for _, replacement in ranked])
        filtered: List[str] = []
        for term in ordered:
            if any(term != existing and term in existing for existing in filtered):
                continue
            filtered = [existing for existing in filtered if not (existing != term and existing in term)]
            filtered.append(term)
            if len(filtered) >= limit:
                break
        return filtered[:limit]

    @classmethod
    def _extract_grounding_numbers(cls, text: str, *, limit: int = 3) -> List[str]:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=280), limit=280)
        if not clean:
            return []
        numbers = re.findall(r"(?<!\d)\d+(?:\.\d+)?%(?!\d)", clean)
        return cls._dedupe_strings(numbers, limit=limit)

    @classmethod
    def _titleworthy_grounding_terms(cls, terms: Sequence[str], *, limit: int = 2) -> List[str]:
        filtered = [
            cls._clean_excerpt(str(term or "").strip(), limit=24)
            for term in list(terms or [])
            if cls._clean_excerpt(str(term or "").strip(), limit=24)
            and cls._clean_excerpt(str(term or "").strip(), limit=24) not in {"USMLE", "ChatGPT", "Step 1", "Step 2", "Step 3"}
            and not (
                cls._clean_excerpt(str(term or "").strip(), limit=24).isascii()
                and re.search(r"[a-z]", cls._clean_excerpt(str(term or "").strip(), limit=24))
                and cls._clean_excerpt(str(term or "").strip(), limit=24).lower()
                == cls._clean_excerpt(str(term or "").strip(), limit=24)
            )
            and cls._clean_excerpt(str(term or "").strip(), limit=24).lower() not in {
                "guide",
                "claim",
                "result",
                "results",
                "summary",
                "figure",
                "panel",
            }
        ]
        return cls._dedupe_strings(filtered, limit=limit)

    @classmethod
    def _comparison_focus_phrase(cls, terms: Sequence[str], *, limit: int = 2) -> str:
        worthy_terms = cls._titleworthy_grounding_terms(terms, limit=limit)
        joined_terms = cls._join_display_terms(worthy_terms)
        if not joined_terms:
            return ""
        if len(worthy_terms) == 1:
            return f"和{joined_terms}有关的差异"
        return f"{joined_terms}之间的对照"

    @classmethod
    def _compose_target_grounding_statement(
        cls,
        target: Mapping[str, Any],
        *,
        segment_type: str = "",
        focus_label: str = "",
    ) -> str:
        current = dict(target or {})
        label = cls._resolve_target_display_label(current)
        title = cls._clean_excerpt(str(current.get("title") or "").strip(), limit=60)
        section_label = cls._clean_excerpt(str(current.get("section_label") or "").strip(), limit=60)
        raw_summary = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(
                current.get("full_text")
                or current.get("excerpt")
                or current.get("summary")
                or current.get("title")
                or current.get("section_label")
                or "",
                limit=360,
            ),
            limit=360,
        )
        target_kind = str(current.get("kind") or current.get("target_kind") or "").strip().lower()
        terms = cls._extract_grounding_terms(raw_summary)
        numbers = cls._extract_grounding_numbers(raw_summary)
        joined_terms = cls._join_display_terms(terms[:4])
        focus_token = cls._clean_excerpt(str(focus_label or "").strip(), limit=60)
        if not current and not raw_summary and not label:
            return ""
        lowered = raw_summary.lower()

        if target_kind in {"figure", "table", "equation"}:
            if "concordance" in lowered and "insight" in lowered and "usmle" in lowered:
                return cls._clean_excerpt(
                    (
                        f"{label or focus_token or '这张图'} 把 USMLE 相关结果放进同一张比较图里，"
                        "重点是看一致性和洞见怎么一起变化。"
                    ),
                    limit=300,
                )
            if "concordance" in lowered and "insight" in lowered:
                return cls._clean_excerpt(
                    (
                        f"{label or focus_token or '这张图'} 把一致性和洞见放在一起比较，"
                        "重点是看这两个维度是不是一起变化。"
                    ),
                    limit=300,
                )

        if target_kind not in {"figure", "table", "equation"}:
            if numbers and ("insight" in lowered or "洞见" in raw_summary):
                lead = f"{section_label or title or '这段正文'} 先给出一个总体结果：{numbers[0]} 的回答出现过至少一个显著洞见。"
                if (
                    len(numbers) >= 2
                    and re.search(r"\b(?:decreased|decrement|declined|drop(?:ped)?|lower)\b", lowered)
                    and re.search(r"\bStep\s*2(?:CK)?\b", raw_summary, flags=re.IGNORECASE)
                ):
                    lead = (
                        f"{lead} 但到了 Step 2，这个指标又下降了 {numbers[1]}，"
                        "说明同一个模型在不同考试阶段给出的解释质量并不整齐。"
                    )
                return cls._clean_excerpt(lead, limit=320)
            if re.search(r"\bwe first examined the frequency of insight\b", lowered):
                return cls._clean_excerpt(
                    (
                        f"{section_label or title or '这段正文'} 先交代洞见到底有多常见，"
                        f"{focus_token + ' 里的比较' if focus_token and segment_type == 'body' else '后面的解释'}"
                        "都是围绕这个基线展开。"
                    ),
                    limit=300,
                )
            generic_frequency = re.search(r"\bwe first examined the frequency of ([a-z][a-z\s-]+)", lowered)
            if generic_frequency:
                localized = cls._join_display_terms(cls._extract_grounding_terms(generic_frequency.group(1), limit=2)) or "这个现象"
                return cls._clean_excerpt(
                    f"{section_label or title or '这段正文'} 先说明{localized}出现得有多频繁，后面的解释都拿这个结果当起点。",
                    limit=280,
                )

        if raw_summary and not cls._needs_display_localization(raw_summary, short_form=len(raw_summary) <= 120):
            if target_kind in {"figure", "table", "equation"} and cls._has_reader_facing_predicate(raw_summary):
                return cls._clean_excerpt(
                    f"{label or focus_token or '这张图'} 主要在讲 {cls._trim_terminal_punctuation(raw_summary)}。",
                    limit=300,
                )
            if target_kind not in {"figure", "table", "equation"} and cls._has_reader_facing_predicate(raw_summary):
                return cls._clean_excerpt(
                    f"{section_label or title or '这段正文'} 在这里先交代了 {cls._trim_terminal_punctuation(raw_summary)}。",
                    limit=300,
                )

        if target_kind in {"figure", "table", "equation"}:
            if joined_terms:
                return cls._clean_excerpt(
                    (
                        f"{label or focus_token or '这张图'} 把{joined_terms}放到同一个比较框架里，"
                        "所以这一页真正要说明的不是单个点，而是这些维度之间怎么一起拉开差距。"
                    ),
                    limit=300,
                )
            if label or focus_token:
                return cls._clean_excerpt(
                    f"{label or focus_token} 集中呈现了这一页最关键的比较，正文后面都在解释这些差异为什么成立。",
                    limit=280,
                )
            return "这张图集中呈现了这一页最关键的比较。"

        section_token = section_label or title or "这段正文"
        if joined_terms:
            connector = (
                f"，把 {focus_token} 里的比较落到更具体的现象上"
                if focus_token and segment_type == "body"
                else ""
            )
            return cls._clean_excerpt(
                f"{section_token} 把{joined_terms}讲得更具体{connector}，让读者知道这些差异到底落在什么地方。",
                limit=300,
            )
        if focus_token and segment_type == "body":
            return f"{section_token} 把 {focus_token} 里的比较结果落成作者真正想强调的判断。"
        return f"{section_token} 把当前页的关键结果继续展开，让前面的比较变成能理解的结论。"

    @classmethod
    def _ordered_grounding_targets_for_segment(
        cls,
        *,
        segment_type: str,
        target_ids: Sequence[str],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        rows = [
            dict(target_map.get(str(target_id).strip()) or {})
            for target_id in list(target_ids or [])
            if str(target_id).strip() and dict(target_map.get(str(target_id).strip()) or {})
        ]
        if not rows:
            return []

        figure_rows = [
            row for row in rows
            if str(row.get("kind") or row.get("target_kind") or "").strip().lower() in {"figure", "table", "equation"}
        ]
        prose_rows = [
            row for row in rows
            if str(row.get("kind") or row.get("target_kind") or "").strip().lower() not in {"figure", "table", "equation"}
        ]
        if segment_type == "opening":
            return [*figure_rows[:1], *prose_rows[:1]] or rows[:2]
        if segment_type in {"figure", "focus"}:
            return figure_rows[:1] or rows[:1]
        if segment_type == "body":
            return prose_rows[:2] or rows[:2]
        return rows[:2]

    @classmethod
    def _compose_segment_grounding_copy(
        cls,
        *,
        segment_type: str,
        target_ids: Sequence[str],
        target_map: Mapping[str, Mapping[str, Any]],
        focus_label: str,
        limit: int = 320,
    ) -> str:
        statements: List[str] = []
        for target in cls._ordered_grounding_targets_for_segment(
            segment_type=segment_type,
            target_ids=target_ids,
            target_map=target_map,
        ):
            statement = cls._compose_target_grounding_statement(
                target,
                segment_type=segment_type,
                focus_label=focus_label,
            )
            statement = cls._clean_excerpt(statement, limit=220) if statement else ""
            if not statement:
                continue
            if any(statement in existing or existing in statement for existing in statements):
                continue
            statements.append(statement)
        return cls._clean_excerpt(" ".join(statements), limit=limit) if statements else ""

    @classmethod
    def _extract_source_block_ids_from_ui_node(
        cls,
        row: Mapping[str, Any],
    ) -> List[str]:
        normalized: List[str] = []
        for raw in list(row.get("source_block_ids") or []):
            token = str(raw or "").strip()
            if token and token not in normalized:
                normalized.append(token)
        for anchor in list(row.get("source_anchor_refs") or []):
            if not isinstance(anchor, Mapping):
                continue
            for raw in (
                anchor.get("canonical_block_id"),
                dict(anchor.get("source_anchor") or {}).get("canonical_block_id"),
            ):
                token = str(raw or "").strip()
                if token and token not in normalized:
                    normalized.append(token)
        return normalized[:8]

    @classmethod
    def _compose_segment_grounding_title(
        cls,
        *,
        segment_type: str,
        target_ids: Sequence[str],
        target_map: Mapping[str, Mapping[str, Any]],
        focus_label: str,
    ) -> str:
        ordered_targets = cls._ordered_grounding_targets_for_segment(
            segment_type=segment_type,
            target_ids=target_ids,
            target_map=target_map,
        )
        if not ordered_targets:
            return ""

        primary = dict(ordered_targets[0] or {})
        secondary = dict(ordered_targets[1] or {}) if len(ordered_targets) > 1 else {}
        primary_label = cls._resolve_target_display_label(primary) or cls._clean_excerpt(str(focus_label or "").strip(), limit=60)
        primary_terms = cls._extract_grounding_terms(
            cls._sanitize_reader_facing_text(
                primary.get("summary") or primary.get("excerpt") or primary.get("title") or "",
                limit=220,
            ),
            limit=2,
        )
        primary_terms = cls._titleworthy_grounding_terms(primary_terms, limit=2)
        secondary_terms = cls._extract_grounding_terms(
            cls._sanitize_reader_facing_text(
                secondary.get("summary") or secondary.get("excerpt") or secondary.get("title") or "",
                limit=220,
            ),
            limit=2,
        )
        secondary_terms = cls._titleworthy_grounding_terms(secondary_terms, limit=2)
        secondary_numbers = cls._extract_grounding_numbers(
            cls._sanitize_reader_facing_text(
                secondary.get("summary") or secondary.get("excerpt") or secondary.get("title") or "",
                limit=220,
            ),
            limit=1,
        )
        section_label = cls._clean_excerpt(
            str(primary.get("section_label") or primary.get("title") or "").strip(),
            limit=48,
        )

        if segment_type == "opening":
            if primary_label and (secondary_numbers or secondary_terms or primary_terms):
                return f"{primary_label} 与这一页的主结论"
            if primary_label:
                return f"{primary_label} 与这一页的主线"
        if segment_type in {"figure", "focus"}:
            if primary_label and primary_terms:
                return f"看清 {primary_label} 里 {cls._join_display_terms(primary_terms)} 的关系"
            if primary_label:
                return f"看清 {primary_label} 里的关键比较"
        if segment_type == "body":
            if section_label and not cls._needs_display_localization(section_label, short_form=True) and (secondary_numbers or primary_terms):
                return f"{section_label} 如何解释前面的结果"
            if primary_terms:
                return f"正文如何解释 {cls._join_display_terms(primary_terms)} 这个结果"
            if secondary_numbers:
                return "正文如何解释这个结果"
            if section_label:
                return f"{section_label} 如何把结果讲清楚"
            if section_label:
                return f"{section_label} 如何把结果讲清楚"
        return ""

    @classmethod
    def _adjacent_bridge_has_specific_continuity(cls, cue_text: str) -> bool:
        clean = cls._strip_adjacent_bridge_provenance(str(cue_text or "").strip())
        if not clean:
            return False
        if cls._adjacent_bridge_surface_needs_repair(clean):
            return False
        generic_bridge_patterns = (
            r"读到这里时，先接上前文关于(?:图示|图表|正文|线索|背景|方法|说明).{0,8}(?:铺垫|线索)",
            r"读到这里时，先把前文那条线索接上",
            r"读到这里时，先把这条前后文线索接上",
        )
        if any(re.search(pattern, clean) for pattern in generic_bridge_patterns) and not re.search(
            r"\b(?:Fig(?:ure)?|Table|Equation)\s*\d+[A-Za-z]?\b",
            clean,
            flags=re.IGNORECASE,
        ):
            return False
        subject = cls._extract_adjacent_bridge_subject(clean)
        if not subject:
            return False
        generic_patterns = (
            r"^(?:图示|图表|正文|内容|线索|背景|结果|比较)(?:阅读|说明|主线|内容|结果)?$",
            r"^(?:上一页|前文|后文|当前页)(?:内容|线索|部分)?$",
            r"^(?:图示说明|图示阅读|方法说明|章节衔接|前后文衔接)$",
        )
        if any(re.search(pattern, subject) for pattern in generic_patterns):
            return False
        if (
            any(token in subject for token in ("图示", "图表", "正文", "线索", "铺垫", "背景", "说明", "方法", "比较", "结果"))
            and not re.search(r"\b(?:Fig(?:ure)?|Table|Equation)\s*\d+[A-Za-z]?\b", subject, flags=re.IGNORECASE)
        ):
            stripped = re.sub(r"[图示图表正文线索铺垫背景说明方法比较结果前后当前页关于的与和、\s]", "", subject)
            if len(stripped) <= 2:
                return False
        return True

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
        grounded_statement = cls._compose_target_grounding_statement(
            primary_target,
            segment_type="figure" if target_kind in {"figure", "table"} else "body",
            focus_label=target_label,
        )
        if grounded_statement:
            return grounded_statement
        if target_kind in {"figure", "table"}:
            return f"{target_label or '主图'} 承载了这一页最关键的结果比较。"
        if section_label and not cls._needs_display_localization(section_label, short_form=True):
            return f"{section_label} 段落给出了这一页的重要结论，并解释这些结果如何落到作者的判断上。"
        return "这一段正文包含本页的重要结论，并解释前面的比较为什么能成立。"

    @classmethod
    def _overlay_teacher_spine_with_packet_copy(
        cls,
        *,
        teacher_spine: Mapping[str, Any],
        beat_guidance: Mapping[str, Any],
    ) -> Dict[str, Any]:
        current = dict(teacher_spine or {})
        packets_by_section = {
            str(key or "").strip(): dict(value or {})
            for key, value in dict(beat_guidance or {}).get("packets_by_section", {}).items()
            if str(key or "").strip() and isinstance(value, Mapping)
        }
        section_map = {
            "focus_stage": ("focus_guidance", 240),
            "reading_flow": ("body_guidance", 240),
            "supporting_resources": ("support_guidance", 220),
        }
        for section_type, (field_name, limit) in section_map.items():
            packet = packets_by_section.get(section_type) or {}
            if not packet:
                continue
            preferred = cls._compose_beat_native_summary(
                beat={"section_type": section_type},
                packet=packet,
                default_summary=str(current.get(field_name) or "").strip(),
                limit=limit,
                prefer_default_if_reader_ready=False,
            )
            if preferred and cls._should_preserve_authored_reader_copy(
                preferred,
                section_type=section_type,
                limit=limit,
            ):
                current[field_name] = preferred
        focus_summary = str(current.get("focus_guidance") or "").strip()
        opening = str(current.get("opening") or "").strip()
        if (
            focus_summary
            and cls._should_preserve_authored_reader_copy(focus_summary, section_type="focus_stage", limit=220)
            and (
                not opening
                or "定下了这一页的主线" in opening
                or "最关键的比较摆出来" in opening
            )
        ):
            current["opening"] = cls._clean_excerpt(focus_summary, limit=220)
        return current

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
            return f"{focus_label} 的关键比较" if focus_label else "关键图示里的核心比较"
        if module_type == "RelatedResourceCard":
            return "读到这里再补的背景"
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
        section_label = str(primary_target.get("section_label") or "").strip()
        module_type = str(module.get("module_type") or "").strip()
        resource_strategy = str(page_brief.get("resource_strategy") or "").strip()
        combined_text = " ".join(
            [
                str(module.get("title") or "").strip(),
                str(module.get("summary") or "").strip(),
                *[
                    " ".join(
                        part for part in [
                            str(item.get("label") or "").strip(),
                            str(item.get("snippet") or "").strip(),
                            str(item.get("href") or "").strip(),
                        ]
                        if part
                    )
                    for item in list(module.get("links") or [])
                    if isinstance(item, Mapping)
                ],
            ]
        ).strip()
        combined_lower = combined_text.lower()
        if module_type == "FigureExplainPanel":
            return f"{focus_label or '这张图'} 把当前页最重要的比较集中在一起，正文会继续解释这些差异为什么重要。"
        if module_type == "RelatedResourceCard":
            if not list(module.get("links") or []):
                return "这组背景资料只负责帮你读懂图里的比较对象和现实含义，不替代正文。"
            if "usmle" in combined_lower or re.search(r"\bstep\s*[123](?:ck)?\b", combined_lower):
                return "这里补的是考试阶段和评估对象的背景，这样再看当前比较时，就知道图里不同类别究竟在比什么。"
            if any(token in combined_lower for token in ("guideline", "specification", "content outline", "official", "官方")):
                return "这里补的是官方定义和范围，帮助你判断作者当前拿来比较的对象各自代表什么。"
            if any(token in combined_lower for token in ("concordance", "insight", "metric", "指标", "评估")):
                return "这里补的是图里几个评估维度的背景，帮助你区分作者到底在比较结果本身，还是在比较解释质量。"
            if section_label and not cls._needs_display_localization(section_label, short_form=True):
                return f"这里只补 {section_label} 缺的那层背景，帮助判断作者为什么会这样比较。"
            if focus_label:
                return f"这里只补理解 {focus_label} 所需的背景，让你知道图里的比较对象各自代表什么。"
        if resource_strategy and not cls._needs_display_localization(resource_strategy):
            return cls._clean_excerpt(f"这些背景资料围绕当前页真正缺失的上下文展开：{resource_strategy}", limit=240)
        return "这里补的是理解当前比较所需的背景，而不是重复论文已经讲清的部分。"

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
            fallback_title = cls._derive_resource_display_title(item, target_map=target_map)
            raw_title = cls._clean_excerpt(str(item.get("display_title") or item.get("title") or ""), limit=120)
            if raw_title in {"补充背景与上下文", "读到这里再补背景", "读到这里再补的背景", "外部背景", "延伸资源"}:
                raw_title = ""
            item["display_title"] = cls._prefer_display_copy(raw_title, fallback_title, limit=120)

            fallback_summary = cls._derive_resource_display_summary(item, page_brief=page_brief, target_map=target_map)
            raw_summary = cls._clean_excerpt(str(item.get("display_summary") or item.get("summary") or ""), limit=240)
            if raw_summary and (
                cls._is_generic_reference_summary(raw_summary)
                or cls._looks_like_generic_helper_summary(raw_summary)
                or cls._is_generic_narrative_summary(raw_summary)
                or cls._looks_like_reader_instruction_copy(raw_summary, section_type="supporting_resources")
            ):
                raw_summary = ""
            item["display_summary"] = cls._prefer_display_copy(raw_summary, fallback_summary, limit=240)
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
            return "这里补充的是理解当前结果所需的术语和指标含义，让前面的比较更容易读懂。"
        if module_type == "QuestionStarterPanel":
            return "把刚读懂的内容变成几个追问，帮助你继续核对而不是再看一遍摘要。"
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
                "这个交互把主图拆成几个关键部分，帮助读者理解每个部分分别在支持什么结果。",
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
    def _storyboard_to_reading_path(
        cls,
        storyboard: Sequence[Mapping[str, Any]],
    ) -> List[str]:
        storyboard_to_reading = {
            "hero": "hero_summary",
            "focus_stage": "focus_evidence",
            "reading_flow": "reading_flow",
            "explainer_cluster": "context_explainer",
            "supporting_resources": "supporting_resources",
            "question_lab": "explore_questions",
            "story_map": "story_map",
        }
        return [
            item
            for item in dict.fromkeys(
                storyboard_to_reading.get(str(row.get("section_type") or "").strip(), "")
                for row in list(storyboard or [])
                if isinstance(row, Mapping)
            )
            if item
        ]

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
    def _is_low_value_domain(cls, host: str) -> bool:
        normalized = str(host or "").strip().lower()
        if not normalized:
            return False
        return any(
            normalized == domain or normalized.endswith(f".{domain}")
            for domain in cls.LOW_VALUE_RESOURCE_DOMAINS
        )

    @staticmethod
    def _low_value_domain_justification(link: Mapping[str, Any]) -> str:
        meta = dict(link.get("meta") or {}) if isinstance(link.get("meta"), Mapping) else {}
        for key in (
            "low_value_justification",
            "domain_justification",
            "justification",
            "link_justification",
        ):
            token = str(link.get(key) or meta.get(key) or "").strip()
            if token:
                return token
        if bool(link.get("allow_low_value_domain")) or bool(meta.get("allow_low_value_domain")):
            return "explicit_override"
        return ""

    @classmethod
    def _sanitize_reader_facing_text(
        cls,
        value: Any,
        *,
        limit: int = 220,
        depth: int = 0,
    ) -> str:
        if value is None or depth > 4:
            return ""
        if isinstance(value, Mapping):
            pieces: List[str] = []
            for key in (
                "markdown",
                "content",
                "text",
                "summary",
                "snippet",
                "excerpt",
                "body",
                "answer",
                "description",
                "value",
                "title",
            ):
                if key not in value:
                    continue
                piece = cls._sanitize_reader_facing_text(value.get(key), limit=limit, depth=depth + 1)
                if piece:
                    pieces.append(piece)
            if not pieces:
                for raw in value.values():
                    piece = cls._sanitize_reader_facing_text(raw, limit=limit, depth=depth + 1)
                    if piece:
                        pieces.append(piece)
                    if len(pieces) >= 3:
                        break
            return cls._clean_excerpt(" ".join(cls._dedupe_strings(pieces, limit=3)), limit=limit)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            pieces = [
                cls._sanitize_reader_facing_text(item, limit=limit, depth=depth + 1)
                for item in list(value or [])[:3]
            ]
            return cls._clean_excerpt(
                " ".join(cls._dedupe_strings([piece for piece in pieces if piece], limit=3)),
                limit=limit,
            )

        text = str(value or "").strip()
        if not text:
            return ""

        parsed = cls._extract_json_dict(text)
        if parsed is not None:
            return cls._sanitize_reader_facing_text(parsed, limit=limit, depth=depth + 1)

        text = cls._apply_light_repair_text(unescape(text))
        text = text.replace('"""', " ")
        text = re.sub(r"```(?:json|markdown|md|html|text)?", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"(?is)<(?:script|style)\b.*?>.*?</(?:script|style)>", " ", text)
        text = re.sub(r"(?is)<!--.*?-->", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = re.sub(
            r"(?im)^\s*\[(?:检索诊断|搜索诊断|search diagnostics?|retrieval diagnostics?|tool diagnostics?)\][^\n\r]*$",
            " ",
            text,
        )
        text = re.sub(
            r"\[(?:检索诊断|搜索诊断|search diagnostics?|retrieval diagnostics?|tool diagnostics?)\]",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"(?im)^\s*(?:diagnostics?|debug|trace)\s*:\s*[^\n\r]*$", " ", text)
        if text.lstrip().startswith("{") or re.search(r'["\'](?:markdown|content|html|text|summary|snippet|excerpt)["\']\s*:', text):
            text = re.sub(
                r'["\']?(?:markdown|content|html|text|summary|snippet|excerpt)["\']?\s*:\s*',
                " ",
                text,
                flags=re.IGNORECASE,
            )
            text = text.replace("{", " ").replace("}", " ")
        text = cls._strip_reader_surface_noise(text)
        text = re.sub(r"\s+", " ", text).strip(" ,;:-")
        return cls._clean_excerpt(text, limit=limit)

    @classmethod
    def _is_reader_surface_noise(cls, text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return True
        lower = clean.lower()
        if re.search(
            r"\b(?:search failed|request has been blocked|rate limit|too many requests|quota exceeded|service unavailable|forbidden|429)\b",
            lower,
        ):
            return True
        if any(
            marker in clean
            for marker in (
                "这一拍",
                "重点目标",
                "追问和检查点",
                "本段已调用",
                "运行期追加",
                "实际执行",
                "不是继续堆新信息",
                "工具沿着这一目标",
                "围绕：",
                "planner output",
                "planning brief",
                "tool trace",
            )
        ):
            return True
        if re.search(r"(?:当前位置|首页)\s*>\s*", clean):
            return True
        if any(
            token in clean
            for token in (
                "威普爱生教育",
                "weproedu",
                "weproedu.com",
                "zhihu.com",
                "linkedin.com",
                "相关阅读",
                "上一篇",
                "下一篇",
                "当前位置：首页",
                "医疗类",
            )
        ):
            return True
        if clean.startswith("### ") and not cls._has_reader_facing_predicate(clean):
            return True
        return False

    @classmethod
    def _strip_reader_surface_noise(cls, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        segments = [
            piece.strip()
            for piece in re.split(r"(?:[\n\r]+|(?<=[.!?。！？])\s+)", raw)
            if piece and piece.strip()
        ]
        kept = [piece for piece in segments if not cls._is_reader_surface_noise(piece)]
        if kept:
            return " ".join(kept)
        return "" if cls._is_reader_surface_noise(raw) else raw

    @classmethod
    def _apply_light_repair_text(cls, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        repaired = text
        repaired = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", repaired)
        repaired = re.sub(r"(?<=\d)(?=[a-z])", " ", repaired)
        repaired = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", repaired)
        repaired = re.sub(r"\s+", " ", repaired)
        return repaired.strip()

    @classmethod
    def _resource_domain_score(cls, href: str) -> int:
        host = cls._extract_hostname(href)
        if not host:
            return 0
        for domain in cls.HIGH_VALUE_RESOURCE_DOMAINS:
            if host == domain or host.endswith(f".{domain}"):
                return 100
        for domain in cls.PREPRINT_RESOURCE_DOMAINS:
            if host == domain or host.endswith(f".{domain}"):
                return 35
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
            path = str(urlparse(href).path or "").strip().lower()
            score = cls._resource_domain_score(href)
            justification = cls._low_value_domain_justification(row)
            if score < 0 and not justification:
                continue
            raw_label = cls._sanitize_reader_facing_text(str(row.get("label") or host), limit=140) or host
            raw_snippet = cls._sanitize_reader_facing_text(
                str(row.get("snippet") or row.get("summary") or ""),
                limit=180,
            )
            if ("youtube.com" in host or host == "youtu.be") and (
                "/shorts/" in path or not justification
            ):
                continue
            if (
                cls._looks_like_hype_marketing_copy(raw_label)
                or cls._looks_like_hype_marketing_copy(raw_snippet)
            ) and score < 100 and not justification:
                continue
            seen.add(href)
            label = host
            if raw_label and not cls._needs_display_localization(raw_label, short_form=True) and (
                not cls._looks_like_heading_only(raw_label) or bool(justification)
            ):
                label = raw_label
            elif "usmle.org" in host:
                label = "USMLE 官方说明"
            elif "pubmed" in host or "ncbi.nlm.nih.gov" in host:
                label = "PubMed 摘要页"
            elif "medrxiv.org" in host:
                label = "medRxiv 预印本"
            elif "youtube.com" in host:
                label = "相关视频"
            normalized: Dict[str, str] = {
                "label": label,
                "href": href,
                "domain": host,
            }
            snippet = cls._best_reader_facing_excerpt(
                raw_snippet,
                tool_name="public_link",
                domain_score=score,
                limit=160,
            )
            if (
                snippet
                and cls._is_reader_ready_summary(snippet)
                and not cls._needs_display_localization(snippet)
                and not cls._looks_like_hype_marketing_copy(snippet)
            ):
                normalized["snippet"] = snippet
            if justification:
                normalized["justification"] = cls._clean_excerpt(justification, limit=140)
            ranked.append((score, host, normalized))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            return []

        positive = [row for row in ranked if row[0] > 0]
        strong = [row for row in ranked if row[0] >= 60]
        candidate_rows = list(strong)
        if not candidate_rows:
            candidate_rows = positive or ranked

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
    def _split_reader_facing_sentences(text: str) -> List[str]:
        working = str(text or "").strip()
        if not working:
            return []
        working = working.replace("•", ". ").replace("|", ". ")
        working = re.sub(r"(?<![A-Za-z0-9])([A-D])\s*[:.)-]\s*", ". ", working)
        parts = re.split(r"(?<=[.!?。！？])\s+|\s*;\s+|\s{2,}", working)
        output: List[str] = []
        for part in parts:
            token = GenerativeReaderAgentRuntime._clean_excerpt(part, limit=260)
            if token:
                output.append(token)
        return output

    @classmethod
    def _has_reader_facing_predicate(cls, text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        if re.search(r"(说明|显示|提示|解释|帮助|强调|比较|增加|降低)", clean):
            return True
        return bool(
            re.search(
                r"\b(?:shows?|showed|explains?|suggests?|indicates?|clarifies?|means|helps?|mentions?|outlines?|"
                r"describes?|covers?|provides?|supports?|frames?|contrasts?|compares?|var(?:y|ies)|"
                r"differ(?:s|ed)?|increase(?:s|d)?|decrease(?:s|d)?|associated|linked|separates?|"
                r"continues?|bridges?|carries?)\b",
                lower,
            )
            or re.search(r"(延续|承接|衔接|串起来|接着)", clean)
        )

    @classmethod
    def _looks_like_reader_metadata(cls, text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return True
        lower = clean.lower()
        if cls._is_reader_surface_noise(clean):
            return True
        if re.match(r"(?i)^(?:paper_read|knowledge_search|web_search|web_scrape)\s*:\s*\{", clean):
            return True
        if (
            ("{" in clean or "}" in clean)
            and re.search(
                r"""(?i)['"]?(?:query|top_k|max_results|only_main_content|formats|url|request_origin)['"]?\s*:""",
                clean,
            )
        ):
            return True
        if any(
            token in lower
            for token in (
                "https://",
                "http://",
                "doi.org/",
                "open access",
                "citation:",
                "received",
                "accepted",
                "published",
                "all rights reserved",
                "editor:",
                "copyright",
                "[来源",
                "页码:",
                "文档:",
                "plos digital health",
            )
        ):
            return True
        if re.search(r"\b\d+\s*/\s*\d+\b", lower):
            return True
        if re.fullmatch(r"(?i)(?:fig(?:ure)?\.?\s*\d+[a-z]?|panel\s+[a-d])", clean):
            return True
        return False

    @classmethod
    def _is_low_signal_reader_excerpt(cls, text: str) -> bool:
        clean = cls._clean_excerpt(text, limit=220)
        if not clean:
            return True
        lower = clean.lower()
        if cls._looks_like_reader_metadata(clean):
            return True
        if cls._looks_like_exam_prompt_fragment(clean):
            return True
        if cls._looks_like_heading_only(clean):
            return True
        if cls._looks_like_personal_blog_narrative(clean):
            return True
        if cls._looks_like_exam_prep_marketing_copy(clean):
            return True
        has_predicate = cls._has_reader_facing_predicate(clean)
        if len(clean) <= 48 and cls._is_fragment_like_excerpt(clean):
            cjk_count = len(re.findall(r"[\u3400-\u9fff]", clean))
            if has_predicate and cjk_count >= 8:
                return False
            return True
        if (
            len(clean) <= 72
            and not has_predicate
            and re.search(
                r"\b(?:overview|summary|context|background|guide|guidance|resource|glossary|terms?|"
                r"introduction|supplement|supplementary)\b",
                lower,
            )
        ):
            return True
        if len(clean) <= 36 and clean.count(" ") < 5 and not has_predicate:
            return True
        return False

    @staticmethod
    def _looks_like_exam_prompt_fragment(text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        return bool(
            re.search(
                r"\b(?:explain your rationale|for each choice|which of the following|select all that apply|best answer|choose the correct|"
                r"multiple[- ]choice|question stem|answer choices?)\b",
                lower,
            )
        )

    @classmethod
    def _looks_like_heading_only(cls, text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        heading = re.sub(r"^#+\s*", "", clean).strip()
        if not heading:
            return False
        if clean.startswith("#") and not cls._has_reader_facing_predicate(heading):
            return True
        if len(heading) <= 120 and not cls._has_reader_facing_predicate(heading):
            if re.fullmatch(r"[A-Z][A-Za-z0-9'\"()\-: ]{8,}", heading):
                return True
        return False

    @staticmethod
    def _looks_like_personal_blog_narrative(text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        if re.search(r"(?:我|我们).*(?:感觉|经历|适应|成长|压力|心得|故事|分享|工作环境|职场|生活)", clean):
            return True
        if re.search(
            r"\b(?:i|we)\b.*\b(?:feel|felt|experience|experienced|adapt|growth|story|journey|stress|workplace|life)\b",
            lower,
        ):
            return True
        return False

    @staticmethod
    def _looks_like_exam_prep_marketing_copy(text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        if re.search(r"\b(?:uworld|kaplan|first aid|mrcp)\b", lower):
            return True
        if any(
            token in clean
            for token in (
                "无从下手",
                "应试者",
                "详情请见图",
                "单独章节",
                "培训",
                "教育",
                "系列里面",
                "皇家医师",
                "英联邦",
                "国外行医",
                "海外行医",
            )
        ):
            return True
        return False

    @staticmethod
    def _looks_like_hype_marketing_copy(text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        if any(
            token in clean
            for token in (
                "革命性变革",
                "深入解读",
                "估值",
                "医生都在用",
                "医生上岗",
                "爆款",
                "刷屏",
                "短视频",
                "大模型创业",
                "融资",
            )
        ):
            return True
        return bool(
            re.search(
                r"\b(?:open evidence|valuation|billion|viral|shorts?|all doctors|everyone is using|revolutionary|game changer|"
                r"product launch|startup|funding|investment|ai doctor)\b",
                lower,
            )
        )

    @staticmethod
    def _has_public_web_relevance_anchor(text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        return bool(
            re.search(
                r"\b(?:usmle|step\s*[123]|exam|question|insight|concordance|accuracy|figure|table|method|result|study|paper|"
                r"model|dataset|trial|medical|clinical|patient|prompt|reasoning)\b",
                lower,
            )
            or re.search(r"(论文|研究|图|表|结果|方法|考试|题目|医学|临床|患者|模型|数据集|推理)", clean)
        )

    @classmethod
    def _collect_reader_anchor_terms(cls, *values: Any, limit: int = 10) -> List[str]:
        terms: List[str] = []
        seen: set[str] = set()
        stopwords = {
            "this", "that", "with", "from", "into", "what", "when", "where", "which", "using",
            "help", "page", "paper", "current", "focus", "reader", "background", "result", "context",
            "作者", "读者", "当前页", "这一页", "背景", "结果", "正文", "解释", "结构", "评估指标",
        }
        stable_terms = {
            "一致性",
            "洞见",
            "洞见出现频率",
            "洞见出现比例",
            "一致性和洞见",
            "题目编码方式",
            "考试类型",
            "解释质量",
            "非显而易见性",
            "非显而易见的洞见",
            "新颖性",
            "有效性",
            "USMLE",
            "ChatGPT",
            "Step 1",
            "Step 2",
            "Step 3",
        }
        english_scaffold_terms = {
            "augment",
            "augmented",
            "augmentation",
            "guide",
            "guided",
            "reader",
            "reading",
            "resource",
            "resources",
            "support",
            "supporting",
            "context",
            "background",
            "focus",
            "opening",
            "body",
            "section",
            "panel",
            "figure",
            "summary",
            "result",
            "results",
            "anchor",
            "module",
            "packet",
            "planner",
            "narrative",
            "explainer",
            "explain",
        }

        def _normalize(token: Any) -> str:
            clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(token, limit=120), limit=120)
            if not clean:
                return ""
            clean = re.sub(r"[“”\"'`]+", "", clean).strip(" ,;:，。：；")
            if not clean:
                return ""
            lower = clean.lower()
            replacements = (
                (r"\b(?:concordance and insight|insight and concordance)\b", "一致性和洞见"),
                (r"\bconcordance\b", "一致性"),
                (r"\binsights?\b", "洞见"),
                (r"\b(?:insight prevalence|frequency of insight|insight frequency)\b", "洞见出现频率"),
                (r"\bexplanation quality\b", "解释质量"),
                (r"\bnon[- ]obvious insights?\b", "非显而易见的洞见"),
                (r"\bquestion encoding formats?\b", "题目编码方式"),
                (r"\bexam types?\b", "考试类型"),
                (r"\bchat\s*gpt\b", "ChatGPT"),
                (r"\busmle\b", "USMLE"),
                (r"\bstep\s*1\b", "Step 1"),
                (r"\bstep\s*2(?:ck)?\b", "Step 2"),
                (r"\bstep\s*3\b", "Step 3"),
                (r"\bnovelty\b", "新颖性"),
                (r"\bvalidity\b", "有效性"),
                (r"\bnon[- ]obvious(?:ness)?\b", "非显而易见性"),
                (r"\b(?:insight prevalence|prevalence of insight|prevalence)\b", "洞见出现比例"),
                (r"\b(?:frequency of insight|insight frequency)\b", "洞见出现频率"),
            )
            for pattern, replacement in replacements:
                if re.search(pattern, lower, flags=re.IGNORECASE):
                    clean = replacement
                    lower = replacement.lower()
                    break
            if lower in stopwords:
                return ""
            if clean in {"结构", "评估指标", "指标", "上下文", "差异", "比较对象", "比较框架"}:
                return ""
            if (
                cls._looks_like_internal_planner_copy(clean)
                or cls._looks_like_reader_metadata(clean)
                or cls._looks_like_reader_instruction_copy(clean, section_type="reading_flow")
                or cls._looks_like_outcome_support_copy(clean, section_type="reading_flow")
                or cls._looks_like_primary_evidence_dump(clean, section_type="reading_flow")
                or cls._contains_long_raw_english_span(clean)
            ):
                return ""
            if re.match(r"^[的且并而及或个在把将与和中]+", clean):
                return ""
            if re.search(r"(?:至少一个|回答中|保持一致|产生了|表明其解释)", clean):
                return ""
            if re.search(r"(?<!\d)\d+(?:\.\d+)?%(?!\d)", clean):
                return ""
            if (
                clean not in stable_terms
                and not re.search(r"[A-Za-z0-9]", clean)
                and (
                    (len(clean) >= 6 and cls._is_fragment_like_excerpt(clean))
                    or re.search(
                        r"(?:表明其|具有|潜力|存在差异|步骤中|回答中|产生了|至少一个|保持一致|总体结果|判断点)",
                        clean,
                    )
                )
            ):
                return ""
            if cls._has_reader_facing_predicate(clean) and len(clean) > 20:
                return ""
            if re.search(r"[。！？!?；;，,]", clean):
                return ""
            if clean.isascii():
                if lower in english_scaffold_terms:
                    return ""
                if re.fullmatch(r"[a-z][a-z0-9.+-]{2,24}", clean):
                    return ""
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9.+-]{1,3}", clean) and clean.upper() not in {"AI"}:
                    return ""
                if len(re.findall(r"[A-Za-z]", clean)) > 14 and clean not in {"Concordance", "ChatGPT"}:
                    return ""
                if len(clean.split()) > 3:
                    return ""
            elif len(clean) > 12 and not re.search(r"[A-Za-z0-9]", clean):
                return ""
            return clean

        def _add(token: str) -> None:
            clean = _normalize(token)
            if not clean:
                return
            key = clean.lower()
            if key in seen or key in stopwords:
                return
            seen.add(key)
            terms.append(clean)

        for value in values:
            text = cls._clean_excerpt(cls._sanitize_reader_facing_text(value, limit=200), limit=200)
            if not text:
                continue
            for candidate in cls._extract_grounding_terms(text, limit=6):
                _add(candidate)
            for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", text):
                lowered = token.lower()
                if lowered in stopwords:
                    continue
                if token.isascii() and len(token) < 4 and not re.search(r"\d", token):
                    continue
                _add(token)
                if len(terms) >= limit:
                    return terms
            if len(terms) >= limit:
                return terms
        return terms[:limit]

    @classmethod
    def _has_anchor_term_overlap(
        cls,
        text: str,
        anchor_terms: Sequence[str],
        *,
        min_matches: int = 1,
    ) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        match_count = 0
        for term in list(anchor_terms or []):
            token = re.sub(r"\s+", " ", str(term or "").strip())
            if not token:
                continue
            token_lower = token.lower()
            if token_lower in lower:
                match_count += 1
            elif re.search(r"[A-Za-z]", token) and any(
                fragment.lower() in lower
                for fragment in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", token)
            ):
                match_count += 1
            if match_count >= min_matches:
                return True
        return False

    @classmethod
    def _summary_content_core(cls, text: str) -> str:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return ""
        return re.sub(
            r"^(?:先抓住这一拍里的图示重点|先讲清楚这一拍为什么值得在意|"
            r"把刚形成的理解转成后续追问和检查点|这一段应该帮助读者停下来整理理解|"
            r"先看图里最关键的一点|先把背景补齐|把它放回前后文里看|先把这个概念讲清楚|"
            r"先补一层必要的方法背景|再看一条有帮助的外部对照|先抓住图里最值得注意的信息|"
            r"先补上理解当前内容需要的背景|先把当前内容放回前后文里|先把会卡住理解的术语讲清楚|"
            r"先给一条有帮助的外部对照)[：:。]?\s*",
            "",
            clean,
        ).strip()

    @classmethod
    def _is_generic_reference_summary(cls, text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        core = cls._summary_content_core(clean) or clean
        return any(
            marker in core
            for marker in (
                "帮助补上理解当前内容所需的官方背景",
                "补上了读图前需要先知道的背景",
                "补上了理解方法时需要的背景说明",
                "帮助把当前内容和前后文串起来",
                "可以作为当前内容的外部对照参考",
                "提供了继续理解当前内容的补充线索",
            )
        )

    @classmethod
    def _looks_like_internal_planner_copy(cls, text: str) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        lower = clean.lower()
        if any(
            token in lower
            for token in (
                "resource_augmented_reader",
                "storyboard",
                "guided beat",
                "planner output",
                "tool_enrichment_packet",
                "figure-focus-accordion",
            )
        ):
            return True
        if re.search(
            r"(清洗后(?:的)?(?:正文)?阅读流|主画布|解释型页面|引导式页面|挂载到侧边|交互模块栈|资源模块栈)",
            clean,
        ):
            return True
        if re.search(
            r"(?:复用|基于|围绕).{0,12}(?:阅读流|正文流).{0,16}(?:主画布|页面体验|解释入口)",
            clean,
        ):
            return True
        return False

    @classmethod
    def _looks_like_reader_instruction_copy(
        cls,
        text: str,
        *,
        section_type: str = "",
    ) -> bool:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return False
        if cls._looks_like_internal_planner_copy(clean):
            return True
        if section_type == "question_lab" and re.search(r"(?:问题|追问|检查点|验证)", clean):
            return False
        if any(
            marker in clean
            for marker in (
                "再回正文",
                "回到正文",
                "继续读正文",
                "按什么顺序理解",
                "阅读顺序",
                "读到这里",
                "按页内顺序",
                "不替代正文",
                "主阅读面",
                "阅读主线",
                "先别急着",
            )
        ):
            return True
        if re.search(
            r"^(?:先|再|接着|顺着|沿着|遇到|读到这里|如果|只在|只有当|这一步|先用|先把|先看|再看|先补|先抓住|先掌握|把注意力放在)",
            clean,
        ):
            return True
        return bool(
            re.search(
                r"\b(?:start with|then return to|go back to|look at the figure first|read the body next|"
                r"guided lesson|reading order|how to read|open the glossary|only when)\b",
                clean.lower(),
            )
        )

    @classmethod
    def _looks_like_outcome_support_copy(
        cls,
        text: str,
        *,
        section_type: str = "",
    ) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=280), limit=280)
        if not clean or section_type == "question_lab":
            return False
        if any(
            marker in clean
            for marker in (
                "关键结论：",
                "重点看作者怎样",
                "顺着当前页正文往下读",
                "顺着正文往下读",
                "也别忘了它是在接前文",
                "别忘了它是在接前文",
                "足以支撑",
            )
        ):
            return True
        if re.search(r"[“\"]([^”\"]{18,})[”\"]", clean) and re.search(r"(?<!\d)\d+(?:\.\d+)?%(?!\d)", clean):
            return True
        return False

    @classmethod
    def _is_natural_explanatory_reader_copy(
        cls,
        text: Any,
        *,
        section_type: str = "",
        limit: int = 260,
    ) -> bool:
        clean = cls._sanitize_reader_facing_text(text, limit=limit)
        clean = cls._clean_excerpt(clean, limit=limit) if clean else ""
        if not clean:
            return False
        if cls._looks_like_reader_instruction_copy(clean, section_type=section_type):
            return False
        if cls._looks_like_outcome_support_copy(clean, section_type=section_type):
            return False
        return cls._is_reader_ready_summary(clean)

    @classmethod
    def _is_reader_ready_summary(cls, text: str) -> bool:
        clean = cls._sanitize_reader_facing_text(text, limit=260)
        if not clean:
            return False
        core = cls._summary_content_core(clean) or clean
        if (
            cls._looks_like_internal_planner_copy(core)
            or cls._looks_like_reader_metadata(core)
            or cls._looks_like_exam_prompt_fragment(core)
            or cls._looks_like_heading_only(core)
            or cls._looks_like_personal_blog_narrative(core)
            or cls._looks_like_exam_prep_marketing_copy(core)
            or cls._looks_like_hype_marketing_copy(core)
            or cls._is_generic_reference_summary(clean)
        ):
            return False
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", core))
        latin_count = len(re.findall(r"[A-Za-z]", core))
        if latin_count >= 28 and cjk_count <= 2:
            return False
        if latin_count >= max(40, cjk_count * 8) and cls._is_low_signal_reader_excerpt(core):
            return False
        return True

    @classmethod
    def _looks_like_primary_evidence_dump(
        cls,
        text: str,
        *,
        section_type: str = "",
    ) -> bool:
        clean = cls._sanitize_reader_facing_text(text, limit=320)
        if not clean:
            return False
        core = cls._summary_content_core(clean) or clean
        lower = core.lower()
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", core))
        latin_count = len(re.findall(r"[A-Za-z]", core))
        if cls._looks_like_internal_planner_copy(core):
            return True
        if cls._needs_display_localization(core):
            return True
        if cls._looks_like_reader_metadata(core) or cls._looks_like_heading_only(core):
            return True
        if re.match(
            r"(?i)^(?:fig(?:ure)?\.?\s*\d+[a-z]?(?:\s*[:.：]|$)|panel\s+[a-d](?:\s*[:.：]|$)|we\s+(?:first\s+)?(?:examined|evaluated|found|observed|measured)|"
            r"the\s+(?:figure|panel|results?)\b|concordance and insight of\b)",
            core,
        ):
            return True
        if (
            section_type in {"hero", "focus_stage", "reading_flow"}
            and latin_count >= max(36, cjk_count * 6)
            and len(core) >= 96
        ):
            return True
        if latin_count >= max(56, cjk_count * 8):
            return True
        if lower.startswith("target learner") and latin_count >= 18:
            return True
        return False

    @classmethod
    def _should_use_display_copy_as_primary(
        cls,
        *,
        raw_value: Any,
        display_value: Any,
        section_type: str = "",
    ) -> bool:
        raw = cls._sanitize_reader_facing_text(raw_value, limit=260)
        display = cls._sanitize_reader_facing_text(display_value, limit=260)
        if not display:
            return False
        if not raw:
            return True
        if cls._needs_display_localization(raw, short_form=len(raw) <= 120):
            return True
        if cls._looks_like_primary_evidence_dump(raw, section_type=section_type):
            return True
        if cls._looks_like_reader_instruction_copy(raw, section_type=section_type):
            return True
        if not cls._is_natural_explanatory_reader_copy(raw, section_type=section_type, limit=260):
            return True
        return False

    @classmethod
    def _should_preserve_authored_reader_copy(
        cls,
        value: Any,
        *,
        section_type: str = "",
        limit: int = 260,
    ) -> bool:
        clean = cls._sanitize_reader_facing_text(value, limit=limit)
        clean = cls._clean_excerpt(clean, limit=limit) if clean else ""
        if not clean:
            return False
        if cls._looks_like_internal_planner_copy(clean):
            return False
        if cls._needs_display_localization(clean, short_form=limit <= 120):
            return False
        if cls._looks_like_primary_evidence_dump(clean, section_type=section_type):
            return False
        if cls._looks_like_generic_helper_summary(clean):
            return False
        if cls._is_generic_narrative_summary(clean):
            return False
        if cls._looks_like_outcome_support_copy(clean, section_type=section_type):
            return False
        if cls._looks_like_hype_marketing_copy(clean):
            return False
        if cls._is_low_signal_reader_excerpt(clean):
            return False
        if cls._looks_like_reader_instruction_copy(clean, section_type=section_type):
            return False
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", clean))
        latin_count = len(re.findall(r"[A-Za-z]", clean))
        if cjk_count == 0 and latin_count < 12:
            return False
        return cls._is_natural_explanatory_reader_copy(
            clean,
            section_type=section_type,
            limit=limit,
        )

    @staticmethod
    def _section_type_to_segment_type(section_type: str) -> str:
        return {
            "hero": "opening",
            "focus_stage": "figure",
            "reading_flow": "body",
            "supporting_resources": "wrapup",
            "explainer_cluster": "body",
            "question_lab": "body",
        }.get(str(section_type or "").strip(), "body")

    @classmethod
    def _should_replace_with_guided_title(
        cls,
        current_title: Any,
        *,
        section_type: str = "",
        limit: int = 120,
    ) -> bool:
        clean = cls._sanitize_reader_facing_text(current_title, limit=limit)
        clean = cls._clean_excerpt(clean, limit=limit) if clean else ""
        if not clean:
            return True
        if cls._looks_like_internal_planner_copy(clean) or cls._is_reader_surface_noise(clean):
            return True
        if cls._needs_display_localization(clean, short_form=True):
            return True
        if cls._is_generic_module_title(clean):
            return True
        if re.fullmatch(r"(?i)(?:fig(?:ure)?\.?\s*\d+[a-z]?|table\s*\d+[a-z]?|equation\s*\d+[a-z]?|\(\d+\))", clean):
            return True
        if re.match(r"^(?:如何阅读 .+|读到这里再补的背景|可靠的外部背景|帮助读懂 .+ 的背景|读懂这一页的关键术语|逐面板理解 .+)$", clean):
            return True
        return cls._manuscript_title_needs_repair(
            segment_type=cls._section_type_to_segment_type(section_type),
            title=clean,
        )

    @classmethod
    def _rewrite_reader_facing_reference(
        cls,
        *,
        label: str,
        snippet: str,
        objective: str,
    ) -> str:
        label_text = cls._sanitize_reader_facing_text(label, limit=80)
        snippet_text = cls._best_reader_facing_excerpt(
            snippet,
            tool_name="public_link",
            limit=140,
        ) or cls._sanitize_reader_facing_text(snippet, limit=140)
        if snippet_text and not cls._is_low_signal_reader_excerpt(snippet_text):
            return snippet_text
        resource_label = label_text if label_text and not cls._looks_like_heading_only(label_text) else "这份公开资料"
        if resource_label in {"USMLE 官方说明", "PubMed 摘要页"}:
            resource_label = "这份公开资料"
        objective_map = {
            "figure_context": f"{resource_label} 补上了读图前需要先知道的背景。",
            "why_it_matters": f"{resource_label} 帮助补上理解当前内容所需的官方背景。",
            "method_background": f"{resource_label} 补上了理解方法时需要的背景说明。",
            "term_explain": f"{resource_label} 可以帮助补足相关术语的背景。",
            "continuation_bridge": f"{resource_label} 帮助把当前内容和前后文串起来。",
            "external_comparison": f"{resource_label} 可以作为当前内容的外部对照参考。",
        }
        return cls._clean_excerpt(
            objective_map.get(objective, f"{resource_label} 提供了继续理解当前内容的补充线索。"),
            limit=180,
        )

    @classmethod
    def _score_reader_facing_text(
        cls,
        text: str,
        *,
        tool_name: str = "",
        domain_score: int = 0,
    ) -> int:
        clean = cls._clean_excerpt(text, limit=280)
        if not clean:
            return -999

        lower = clean.lower()
        score = 0
        length = len(clean)

        if cls._is_reader_surface_noise(clean):
            return -999
        if cls._looks_like_exam_prompt_fragment(clean):
            return -999
        if tool_name in {"web_search", "web_scrape", "knowledge_search", "public_link"} and cls._looks_like_personal_blog_narrative(clean):
            return -999
        if cls._looks_like_hype_marketing_copy(clean):
            return -999

        if 45 <= length <= 180:
            score += 24
        elif 24 <= length <= 220:
            score += 14
        elif length > 240:
            score -= 12

        if re.search(r"[.!?。！？]$", clean):
            score += 8
        if not cls._is_fragment_like_excerpt(clean):
            score += 14
        if cls._has_reader_facing_predicate(clean):
            score += 10
        if cls._looks_like_reader_metadata(clean):
            score -= 40
        if cls._is_low_signal_reader_excerpt(clean):
            score -= 18
        if "target learner" in lower:
            score += 120
        if re.search(r"(?i)\bfig(?:ure)?\.?\s*\d+[a-z]?\b", clean):
            score -= 10
        if re.search(r"(?i)\b(fig(?:ure)?\.?\s*\d+[a-z]?)\b.*\b\1\b", clean):
            score -= 18
        if len(re.findall(r"(?<![A-Za-z0-9])[A-D]\s*[:.)-]\s", clean)) >= 2:
            score -= 24
        if clean.count(":") + clean.count("：") >= 3:
            score -= 10
        if re.match(r"(?i)^concordance and insight of .* on .*\.?$", clean) and not cls._has_reader_facing_predicate(clean):
            score -= 26
        if cls._looks_like_heading_only(clean):
            score -= 70
        if tool_name in {"web_search", "web_scrape", "public_link"} and domain_score < 80 and not cls._has_public_web_relevance_anchor(clean):
            score -= 36

        if tool_name == "web_scrape":
            score += 8
        elif tool_name == "paper_read":
            score += 6
        elif tool_name == "knowledge_search":
            score += 4

        if domain_score >= 80:
            score += 18
        elif domain_score >= 50:
            score += 10
        elif domain_score > 0:
            score += 4

        return score

    @classmethod
    def _sanitize_experience_meta_text(cls, value: Any, *, limit: int = 220) -> str:
        clean = cls._sanitize_reader_facing_text(value, limit=limit)
        if not clean:
            return ""
        if (
            cls._looks_like_heading_only(clean)
            or cls._looks_like_personal_blog_narrative(clean)
            or cls._looks_like_exam_prep_marketing_copy(clean)
            or cls._looks_like_hype_marketing_copy(clean)
            or cls._looks_like_internal_planner_copy(clean)
            or cls._is_low_signal_reader_excerpt(clean)
        ):
            return ""
        return cls._clean_excerpt(clean, limit=limit)

    @classmethod
    def _strip_reader_caption_block(cls, text: str) -> str:
        compact = re.sub(r"\s+", " ", str(text or "").strip())
        if not compact:
            return ""
        match = re.search(r"(?i)\bfig(?:ure)?\.?\s*\d+[a-z]?\s*[:.]", compact)
        if not match:
            return compact
        tail = compact[match.start():]
        if not re.search(r"(?<![A-Za-z0-9])[A-D]\s*[:.)-]\s", tail):
            return compact
        end_match = re.search(r"(?i)(https?://\S+|doi\.org/\S+|\bplos\b|\bopen access\b|\bcitation\b)", tail)
        if not end_match:
            return compact
        stripped = re.sub(r"\s+", " ", f"{compact[:match.start()]} {tail[end_match.end():]}").strip()
        return stripped or compact

    @classmethod
    def _best_reader_facing_excerpt(
        cls,
        value: Any,
        *,
        tool_name: str = "",
        domain_score: int = 0,
        limit: int = 220,
    ) -> str:
        initial = cls._sanitize_reader_facing_text(value, limit=max(limit * 4, 720))
        if not initial:
            return ""

        initial = re.sub(r"(?i)\[来源\d+\]\s*", " ", initial)
        initial = re.sub(r"(?i)\b文档\s*:\s*[^|]+(?:\|\s*页码\s*:\s*\d+)?", " ", initial)
        initial = re.sub(r"(?i)\b(fig(?:ure)?\.?\s*\d+[a-z]?)\b(?:\s+\1\b)+", r"\1", initial)
        initial = re.sub(r"\s+", " ", initial).strip(" ,;:-")

        candidate_bases: List[str] = []
        stripped = cls._strip_reader_caption_block(initial)
        if stripped and stripped != initial:
            candidate_bases.append(stripped)
        candidate_bases.append(initial)

        candidates: List[str] = []
        for base in candidate_bases:
            clean_base = re.sub(r"\s+", " ", str(base or "").strip()).strip(" ,;:-")
            if not clean_base:
                continue
            metadata_trimmed = re.sub(
                r"(?i)^(?:plos(?:\s+[a-z]+){0,4}\s+\d+\s*/\s*\d+\s*|open access\s+|citation:\s*[^.!?]{0,120}\s*)+",
                "",
                clean_base,
            ).strip()
            if metadata_trimmed and metadata_trimmed != clean_base:
                clean_base = metadata_trimmed
            candidates.append(clean_base)

            trimmed = re.sub(
                r"(?i)^(?:(?:fig(?:ure)?\.?\s*\d+[a-z]?|panel\s+[a-d])\s*[:.\-–)]\s*)+",
                "",
                clean_base,
            ).strip()
            if trimmed and trimmed != clean_base:
                candidates.append(trimmed)

            sentences = cls._split_reader_facing_sentences(clean_base)
            candidates.extend(sentences[:6])
            for index in range(len(sentences) - 1):
                pair = f"{sentences[index]} {sentences[index + 1]}".strip()
                if pair:
                    candidates.append(pair)

        best = ""
        best_score = -999
        seen: set[str] = set()
        for candidate in candidates:
            normalized = re.sub(r"\s+", " ", str(candidate or "").strip()).strip(" ,;:-")
            if not normalized:
                continue
            dedupe_key = normalized.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            clipped = cls._clean_excerpt(normalized, limit=limit)
            score = cls._score_reader_facing_text(
                clipped,
                tool_name=tool_name,
                domain_score=domain_score,
            )
            if score > best_score:
                best = clipped
                best_score = score

        if not best:
            return ""
        if best_score < -20 and cls._looks_like_reader_metadata(best):
            return ""
        if tool_name != "public_link" and cls._is_low_signal_reader_excerpt(best) and best_score < 18:
            return ""
        return best

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

    @classmethod
    def _compact_tool_trace_for_experience(
        cls,
        tool_trace: Sequence[Mapping[str, Any]],
        *,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
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
            if str(data.get("beat_id") or "").strip():
                entry["beat_id"] = str(data.get("beat_id") or "").strip()
            if trace_type == "observation":
                entry["success"] = bool(data.get("success"))
                if str(data.get("request_origin") or "").strip():
                    entry["request_origin"] = str(data.get("request_origin") or "").strip()
                output = cls._sanitize_experience_meta_text(data.get("output"), limit=200)
                if output:
                    entry["output_excerpt"] = output
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
            "Recover the most useful current-page-anchored explanations, bridges, and resources from the tool observations.\n"
            "If web scrape/search evidence is weak, keep links conservative and say so in meta.notes.\n"
            "You may author reader-facing display copy directly, but stay faithful to the supplied evidence and avoid raw source dumping.\n"
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
            logger.warning(
                f"[GenerativeReaderAgentRuntime] timeout recovery failed: {exc.__class__.__name__}: {exc}"
            )
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

    @classmethod
    def _compact_guided_beats_for_generation(
        cls,
        guided_beats: Sequence[Mapping[str, Any]],
        *,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in list(guided_beats or [])[:limit]:
            if not isinstance(row, Mapping):
                continue
            rows.append(
                {
                    "beat_id": str(row.get("beat_id") or "").strip(),
                    "role": str(row.get("role") or row.get("section_type") or "").strip(),
                    "section_type": str(row.get("section_type") or "").strip(),
                    "title": cls._clean_excerpt(str(row.get("title") or "").strip(), limit=80),
                    "reader_goal": cls._clean_excerpt(str(row.get("reader_goal") or "").strip(), limit=140),
                    "continuity_note": cls._clean_excerpt(str(row.get("continuity_note") or "").strip(), limit=160),
                    "target_ids": cls._dedupe_strings(
                        [str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()],
                        limit=6,
                    ),
                    "tool_objectives": cls._dedupe_strings(
                        [str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()],
                        limit=4,
                    ),
                    "block_stack": cls._dedupe_strings(
                        [str(item).strip() for item in list(row.get("block_stack") or []) if str(item).strip()],
                        limit=4,
                    ),
                    "drop_notes": cls._dedupe_strings(
                        [str(item).strip() for item in list(row.get("drop_notes") or []) if str(item).strip()],
                        limit=3,
                    ),
                    "priority": int(row.get("priority") or 0),
                }
            )
        return rows

    @classmethod
    def _compact_page_dossier_for_generation(
        cls,
        page_dossier: Mapping[str, Any],
    ) -> Dict[str, Any]:
        dossier = dict(page_dossier or {})
        current_page = dict(dossier.get("current_page") or {})
        return {
            "focus_page": int(dossier.get("focus_page") or current_page.get("page") or 0),
            "current_page": {
                "page": int(current_page.get("page") or 0),
                "build_mode": str(current_page.get("build_mode") or "").strip(),
                "pipeline_version": str(current_page.get("pipeline_version") or "").strip(),
                "status": str(current_page.get("status") or "").strip(),
                "degraded_reason": str(current_page.get("degraded_reason") or "").strip(),
                "decision_log": [
                    cls._clean_excerpt(str(item).strip(), limit=120)
                    for item in list(current_page.get("decision_log") or [])
                    if str(item).strip()
                ][:6],
                "targets": [
                    {
                        "target_id": str(item.get("target_id") or "").strip(),
                        "kind": str(item.get("kind") or item.get("target_kind") or "").strip(),
                        "title": cls._clean_excerpt(str(item.get("title") or "").strip(), limit=80),
                        "summary": cls._clean_excerpt(str(item.get("summary") or item.get("excerpt") or "").strip(), limit=160),
                    }
                    for item in list(current_page.get("targets") or [])
                    if isinstance(item, Mapping)
                ][:10],
                "assets": [
                    {
                        "kind": str(item.get("kind") or "").strip(),
                        "label": cls._clean_excerpt(str(item.get("label") or "").strip(), limit=80),
                        "source": str(item.get("source") or "").strip(),
                    }
                    for item in list(current_page.get("assets") or [])
                    if isinstance(item, Mapping)
                ][:8],
                "quality": dict(current_page.get("quality") or {}),
            },
        }

    @classmethod
    def _compact_planning_brief_for_generation(
        cls,
        planning_brief: Mapping[str, Any],
    ) -> Dict[str, Any]:
        brief = dict(planning_brief or {})
        return {
            "summary": cls._clean_excerpt(str(brief.get("summary") or "").strip(), limit=220),
            "page_archetype_hint": str(brief.get("page_archetype_hint") or "").strip(),
            "continuity_mode": str(brief.get("continuity_mode") or "").strip(),
            "primary_focus_label": cls._clean_excerpt(str(brief.get("primary_focus_label") or "").strip(), limit=100),
            "page_dossier_topics": cls._dedupe_strings(
                [cls._query_fragment(item, limit=80) for item in list(brief.get("page_dossier_topics") or []) if cls._query_fragment(item, limit=80)],
                limit=6,
            ),
            "reader_goal": cls._clean_excerpt(str(brief.get("reader_goal") or "").strip(), limit=220),
            "hero_angle_hint": cls._clean_excerpt(str(brief.get("hero_angle_hint") or "").strip(), limit=220),
            "recommended_sections": cls._dedupe_strings(
                [str(item).strip() for item in list(brief.get("recommended_sections") or []) if str(item).strip()],
                limit=8,
            ),
            "resource_gap_topics": cls._dedupe_strings(
                [str(item).strip() for item in list(brief.get("resource_gap_topics") or []) if str(item).strip()],
                limit=5,
            ),
            "tool_hints": cls._dedupe_strings(
                [str(item).strip() for item in list(brief.get("tool_hints") or []) if str(item).strip()],
                limit=6,
            ),
            "body_flow_target_ids": cls._dedupe_strings(
                [str(item).strip() for item in list(brief.get("body_flow_target_ids") or []) if str(item).strip()],
                limit=12,
            ),
            "guided_beat_seed": cls._compact_guided_beats_for_generation(
                [dict(item) for item in list(brief.get("guided_beat_seed") or []) if isinstance(item, Mapping)],
                limit=8,
            ),
            "tool_budget": dict(brief.get("tool_budget") or {}),
        }

    @classmethod
    def _compact_planner_output_for_generation(
        cls,
        planner_output: Mapping[str, Any],
    ) -> Dict[str, Any]:
        output = dict(planner_output or {})
        return {
            "version": str(output.get("version") or "").strip(),
            "page_objective": cls._clean_excerpt(str(output.get("page_objective") or "").strip(), limit=220),
            "narrative_strategy": cls._clean_excerpt(str(output.get("narrative_strategy") or "").strip(), limit=220),
            "section_strategy": cls._dedupe_strings(
                [str(item).strip() for item in list(output.get("section_strategy") or []) if str(item).strip()],
                limit=8,
            ),
            "guided_beats": cls._compact_guided_beats_for_generation(
                [dict(item) for item in list(output.get("guided_beats") or []) if isinstance(item, Mapping)],
                limit=8,
            ),
            "tool_requests": [
                {
                    "beat_id": str(item.get("beat_id") or "").strip(),
                    "tool": str(item.get("tool") or "").strip(),
                    "arguments": {
                        key: value
                        for key, value in dict(item.get("arguments") or {}).items()
                        if key in {"query", "url", "top_k", "max_results"}
                    },
                    "reason": cls._clean_excerpt(str(item.get("reason") or "").strip(), limit=120),
                    "priority": str(item.get("priority") or "").strip(),
                }
                for item in list(output.get("tool_requests") or [])
                if isinstance(item, Mapping)
            ][:6],
            "resource_objectives": cls._dedupe_strings(
                [str(item).strip() for item in list(output.get("resource_objectives") or []) if str(item).strip()],
                limit=5,
            ),
            "widget_focus": cls._clean_excerpt(str(output.get("widget_focus") or "").strip(), limit=100),
            "page_generation_notes": cls._dedupe_strings(
                [str(item).strip() for item in list(output.get("page_generation_notes") or []) if str(item).strip()],
                limit=6,
            ),
        }

    @classmethod
    def _compact_tool_enrichment_packet_for_generation(
        cls,
        tool_enrichment_packet: Mapping[str, Any],
    ) -> Dict[str, Any]:
        packet = dict(tool_enrichment_packet or {})
        return {
            "version": str(packet.get("version") or "").strip(),
            "requested_tools": [
                {
                    "beat_id": str(item.get("beat_id") or "").strip(),
                    "tool": str(item.get("tool") or "").strip(),
                    "reason": cls._clean_excerpt(str(item.get("reason") or "").strip(), limit=120),
                    "priority": str(item.get("priority") or "").strip(),
                }
                for item in list(packet.get("requested_tools") or [])
                if isinstance(item, Mapping)
            ][:6],
            "runtime_requested_tools": [
                {
                    "beat_id": str(item.get("beat_id") or "").strip(),
                    "tool": str(item.get("tool") or "").strip(),
                    "request_origin": str(item.get("request_origin") or "planner").strip() or "planner",
                    "reason": cls._clean_excerpt(str(item.get("reason") or "").strip(), limit=120),
                    "priority": str(item.get("priority") or "").strip(),
                }
                for item in list(packet.get("runtime_requested_tools") or [])
                if isinstance(item, Mapping)
            ][:8],
            "executed_tools": cls._dedupe_strings(
                [str(item).strip() for item in list(packet.get("executed_tools") or []) if str(item).strip()],
                limit=6,
            ),
            "resource_objectives": cls._dedupe_strings(
                [str(item).strip() for item in list(packet.get("resource_objectives") or []) if str(item).strip()],
                limit=5,
            ),
            "page_generation_notes": cls._dedupe_strings(
                [
                    cls._sanitize_experience_meta_text(item, limit=160)
                    for item in list(packet.get("page_generation_notes") or [])
                    if cls._sanitize_experience_meta_text(item, limit=160)
                ],
                limit=6,
            ),
            "budget_summary": dict(packet.get("budget_summary") or {}),
            "tool_findings": [
                {
                    "beat_id": str(item.get("beat_id") or "").strip(),
                    "tool": str(item.get("tool") or "").strip(),
                    "success": bool(item.get("success")),
                    "output_excerpt": cls._sanitize_experience_meta_text(item.get("output_excerpt"), limit=220),
                    "error": cls._clean_excerpt(str(item.get("error") or "").strip(), limit=120),
                    "request_origin": str(item.get("request_origin") or "planner").strip() or "planner",
                }
                for item in list(packet.get("tool_findings") or [])
                if isinstance(item, Mapping)
            ][:8],
            "public_links": cls._normalize_public_links(
                [dict(item) for item in list(packet.get("public_links") or []) if isinstance(item, Mapping)],
                limit=4,
            ),
            "beat_packets": [
                {
                    "beat_id": str(item.get("beat_id") or "").strip(),
                    "title": cls._clean_excerpt(str(item.get("title") or "").strip(), limit=80),
                    "reader_goal": cls._clean_excerpt(str(item.get("reader_goal") or "").strip(), limit=140),
                    "tool_objectives": cls._dedupe_strings(
                        [str(term).strip() for term in list(item.get("tool_objectives") or []) if str(term).strip()],
                        limit=4,
                    ),
                    "requested_tools": [
                        {
                            "tool": str(tool_row.get("tool") or "").strip(),
                            "request_origin": str(tool_row.get("request_origin") or "planner").strip() or "planner",
                            "reason": cls._clean_excerpt(str(tool_row.get("reason") or "").strip(), limit=100),
                            "priority": str(tool_row.get("priority") or "").strip(),
                        }
                        for tool_row in list(item.get("requested_tools") or [])
                        if isinstance(tool_row, Mapping)
                    ][:3],
                    "tool_accounting": dict(item.get("tool_accounting") or {}),
                    "tool_findings": [
                        {
                            "tool": str(tool_row.get("tool") or "").strip(),
                            "success": bool(tool_row.get("success")),
                            "output_excerpt": cls._sanitize_experience_meta_text(tool_row.get("output_excerpt"), limit=180),
                            "error": cls._clean_excerpt(str(tool_row.get("error") or "").strip(), limit=100),
                            "request_origin": str(tool_row.get("request_origin") or "planner").strip() or "planner",
                        }
                        for tool_row in list(item.get("tool_findings") or [])
                        if isinstance(tool_row, Mapping)
                    ][:3],
                    "public_links": cls._normalize_public_links(
                        [dict(link) for link in list(item.get("public_links") or []) if isinstance(link, Mapping)],
                        limit=3,
                    ),
                }
                for item in list(packet.get("beat_packets") or [])
                if isinstance(item, Mapping)
                and (
                    list(item.get("requested_tools") or [])
                    or list(item.get("tool_findings") or [])
                    or list(item.get("public_links") or [])
                )
            ][:8],
        }

    @classmethod
    def _compact_tool_enrichment_packet_for_experience(
        cls,
        tool_enrichment_packet: Mapping[str, Any],
    ) -> Dict[str, Any]:
        packet = dict(tool_enrichment_packet or {})
        compact = cls._compact_tool_enrichment_packet_for_generation(packet)

        def _compact_findings(rows: Sequence[Mapping[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
            compact_rows: List[Dict[str, Any]] = []
            for item in list(rows or []):
                if not isinstance(item, Mapping):
                    continue
                tool_name = str(item.get("tool") or "").strip()
                source_url = str(item.get("source_url") or "").strip()
                excerpt = cls._best_reader_facing_excerpt(
                    item.get("output_excerpt"),
                    tool_name=tool_name,
                    domain_score=cls._resource_domain_score(source_url),
                    limit=180,
                )
                error = cls._clean_excerpt(str(item.get("error") or "").strip(), limit=100)
                if not excerpt and not error:
                    continue
                compact_rows.append(
                    {
                        "tool": tool_name,
                        "success": bool(item.get("success")),
                        "output_excerpt": excerpt,
                        "error": error,
                        "request_origin": str(item.get("request_origin") or "planner").strip() or "planner",
                    }
                )
                if len(compact_rows) >= limit:
                    break
            return compact_rows

        compact["tool_findings"] = [
            {
                "beat_id": str(item.get("beat_id") or "").strip(),
                **row,
            }
            for item, row in (
                (item, row)
                for item in list(packet.get("tool_findings") or [])
                if isinstance(item, Mapping)
                for row in _compact_findings([item], limit=1)
            )
        ][:8]
        compact["beat_packets"] = []
        for item in list(packet.get("beat_packets") or []):
            if not isinstance(item, Mapping):
                continue
            if not (
                list(item.get("requested_tools") or [])
                or list(item.get("tool_findings") or [])
                or list(item.get("public_links") or [])
            ):
                continue
            reader_copy = cls._extract_beat_packet_reader_copy(item)
            compact_item: Dict[str, Any] = {
                "beat_id": str(item.get("beat_id") or "").strip(),
                "title": cls._clean_excerpt(str(item.get("title") or "").strip(), limit=80),
                "reader_goal": cls._sanitize_experience_meta_text(item.get("reader_goal"), limit=140),
                "tool_objectives": cls._dedupe_strings(
                    [str(term).strip() for term in list(item.get("tool_objectives") or []) if str(term).strip()],
                    limit=4,
                ),
                "requested_tools": [
                    {
                        "tool": str(tool_row.get("tool") or "").strip(),
                        "request_origin": str(tool_row.get("request_origin") or "planner").strip() or "planner",
                        "reason": cls._clean_excerpt(str(tool_row.get("reason") or "").strip(), limit=100),
                        "priority": str(tool_row.get("priority") or "").strip(),
                    }
                    for tool_row in list(item.get("requested_tools") or [])
                    if isinstance(tool_row, Mapping)
                ][:3],
                "tool_findings": _compact_findings(
                    [tool_row for tool_row in list(item.get("tool_findings") or []) if isinstance(tool_row, Mapping)],
                    limit=3,
                ),
                "public_links": cls._normalize_public_links(
                    [dict(link) for link in list(item.get("public_links") or []) if isinstance(link, Mapping)],
                    limit=3,
                ),
            }
            if reader_copy["summary"]:
                compact_item["summary"] = reader_copy["summary"]
            if reader_copy["supporting_points"]:
                compact_item["supporting_points"] = reader_copy["supporting_points"]
            if reader_copy["reader_notes"]:
                compact_item["reader_facing_notes"] = reader_copy["reader_notes"]
            compact["beat_packets"].append(compact_item)
            if len(compact["beat_packets"]) >= 8:
                break
        return compact

    @classmethod
    def _build_public_web_backfill_request(
        cls,
        *,
        planner_output: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        resource_objectives = [
            str(item or "").strip()
            for item in list(planner_output.get("resource_objectives") or [])
            if str(item or "").strip()
        ]
        guided_beats = [
            dict(row)
            for row in list(planner_output.get("guided_beats") or [])
            if isinstance(row, Mapping)
        ]
        widget_focus = str(planner_output.get("widget_focus") or "").strip()
        beat_id = ""
        for beat in guided_beats:
            objectives = {str(item).strip() for item in list(beat.get("tool_objectives") or []) if str(item).strip()}
            if objectives.intersection({"why_it_matters", "external_comparison", "method_background"}):
                beat_id = str(beat.get("beat_id") or "").strip()
                break
        lead_topic = resource_objectives[0] if resource_objectives else ""
        query = " ".join(token for token in [lead_topic, widget_focus] if token).strip()
        if not query:
            return None
        return {
            "tool": "web_search",
            "arguments": {"query": query, "max_results": 5},
            "reason": "Backfill one small set of authoritative public links when reader-native grounding leaves context gaps.",
            "priority": "medium",
            "beat_id": beat_id,
        }

    @classmethod
    def _first_successful_tool_excerpt(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        preferred_tools: Optional[Sequence[str]] = None,
        limit: int = 220,
    ) -> str:
        preferred = [str(item).strip() for item in list(preferred_tools or []) if str(item).strip()]
        for tool_name in preferred:
            for row in list(rows or []):
                if not isinstance(row, Mapping):
                    continue
                if str(row.get("tool") or "").strip() != tool_name or not bool(row.get("success")):
                    continue
                excerpt = cls._clean_excerpt(str(row.get("output_excerpt") or "").strip(), limit=limit)
                if excerpt:
                    return excerpt
        for row in list(rows or []):
            if not isinstance(row, Mapping) or not bool(row.get("success")):
                continue
            excerpt = cls._clean_excerpt(str(row.get("output_excerpt") or "").strip(), limit=limit)
            if excerpt:
                return excerpt
        return ""

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

    @classmethod
    def _derive_body_flow_target_ids(cls, enrichment_bundle: Mapping[str, Any]) -> List[str]:
        target_map = cls._index_targets(enrichment_bundle)
        figure_target_ids = cls._select_preferred_current_page_target_ids(
            target_map=target_map,
            role="figure",
            limit=1,
        )
        body_target_ids = cls._select_preferred_current_page_target_ids(
            target_map=target_map,
            role="body",
            limit=6,
        )
        ordered = cls._dedupe_strings([*figure_target_ids, *body_target_ids], limit=8)
        if ordered:
            return ordered
        fallback: List[str] = []
        for row in list(enrichment_bundle.get("targets") or []):
            if not isinstance(row, Mapping):
                continue
            target_id = str(row.get("target_id") or "").strip()
            if not target_id or target_id in fallback:
                continue
            fallback.append(target_id)
        return fallback

    def _build_fallback_plan(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        targets = [row for row in list(enrichment_bundle.get("targets") or []) if isinstance(row, Mapping)]
        body_flow_target_ids = self._derive_body_flow_target_ids(enrichment_bundle)
        figure_target = next((row for row in targets if str(row.get("target_kind") or "") == "figure"), None)
        paragraph_target = next((row for row in targets if str(row.get("target_kind") or "") == "paragraph"), None)
        section_target = next((row for row in targets if str(row.get("target_kind") or "") == "section"), None)

        resource_modules: List[Dict[str, Any]] = []
        interaction_modules: List[Dict[str, Any]] = []
        js_widgets: List[Dict[str, Any]] = []
        rationale: List[str] = [
            "本页会沿着正文主线展开解释，突出最值得理解的内容。",
            "围绕关键证据补充少量必要背景和交互，让页面解释更完整，而不是替代原文。",
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
        target_lookup = {
            str(item.get("target_id") or "").strip(): dict(item)
            for item in targets
            if str(item.get("target_id") or "").strip()
        }
        preferred_body_ids = self._select_preferred_current_page_target_ids(
            target_map=target_lookup,
            role="body",
            limit=4,
        )
        preferred_figure_ids = self._select_preferred_current_page_target_ids(
            target_map=target_lookup,
            role="figure",
            limit=3,
        )
        paragraph_targets = [
            dict(target_lookup.get(target_id) or {})
            for target_id in preferred_body_ids
            if str(dict(target_lookup.get(target_id) or {}).get("target_kind") or "") == "paragraph"
        ] or [row for row in targets if str(row.get("target_kind") or "") == "paragraph"]
        section_targets = [row for row in targets if str(row.get("target_kind") or "") == "section"]
        figure_targets = [
            dict(target_lookup.get(target_id) or {})
            for target_id in preferred_figure_ids
            if str(dict(target_lookup.get(target_id) or {}).get("target_kind") or "") == "figure"
        ] or [row for row in targets if str(row.get("target_kind") or "") == "figure"]
        table_targets = [row for row in targets if str(row.get("target_kind") or "") == "table"]
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
            return "解释这一页的方法设置、展开过程和它为什么重要。"
        if archetype == "concept_decoder":
            return "解释这一页的指标和领域术语，并说明它们如何影响结果判断。"
        if lead_claim and not GenerativeReaderAgentRuntime._is_english_heavy_text(lead_claim):
            return f"帮助读者理解这一页的主结论：{lead_claim}"
        return "解释这一页最关键的结果、比较对象和它们背后的含义。"

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
                return f"{focus_label} 承载了这一页最关键的比较，正文随后解释这些差异为什么足以支撑“{lead_claim}”。"
            if focus_label:
                return f"{focus_label} 承载了这一页最关键的比较，正文随后解释这些差异为什么重要。"
        if archetype == "methods_decoder":
            return "这一页把方法设置、过程和含义连成一条完整解释链，帮助读者理解它为什么这样设计。"
        if archetype == "concept_decoder":
            return "这一页会把技术词汇和结果放在同一条解释线上，帮助读者理解概念如何影响结论。"
        if archetype == "finding_digest" and lead_claim and not GenerativeReaderAgentRuntime._is_english_heavy_text(lead_claim):
            return f"这一页围绕“{lead_claim}”展开，并逐步展示支撑这个判断的证据。"
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
            return "这一页的重要性，在于关键比较先被摆出来，后面的正文再解释它为什么重要。"
        if section_token == "focus_stage":
            if archetype == "figure_explainer" and focus_token:
                return f"{focus_token} 最值得注意的是，它把关键比较压缩进同一张图，能先看出后文到底在解释什么。"
            return "这部分先把当前页最关键的证据摆出来，方便看清后文到底要解释哪些差异。"
        if section_token == "reading_flow":
            if focus_token:
                return f"正文不会重复 {focus_token} 里的高低，而是把这些对照串成作者真正关心的解释链。"
            return "正文不是重复前面的结果，而是把那些对照一步步解释成作者真正关心的判断。"
        if section_token == "explainer_cluster":
            return "这里把容易混在一起的术语、指标和机制拆开，帮你读懂前面到底在比较什么。"
        if section_token == "supporting_resources":
            if background_topics:
                return f"这里补 {', '.join(background_topics[:2])} 这层背景，是为了认出前面各组比较各自对应什么对象或现实场景。"
            if resource_strategy:
                return "这里补的是理解当前页所需的少量权威背景，帮助判断前面那些比较为什么这样分层。"
            return "这里补的是理解当前页所需的少量权威背景，帮助判断前面那些比较为什么这样分层。"
        if section_token == "question_lab":
            return "这些追问把当前页的结果转成可继续验证的理解线索。"
        if section_token == "story_map":
            return "在不打扰主阅读面的前提下，补充这页的叙事意图、阅读钩子和工具决策。"
        return "围绕当前页面的核心内容组织一个更易读的阅读入口。"

    @classmethod
    def _normalize_adjacent_bridge_seed(cls, value: Any, *, limit: int = 140) -> str:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(value, limit=limit), limit=limit)
        if not clean:
            return ""
        if (
            cls._is_english_heavy_text(clean)
            or
            cls._needs_display_localization(clean)
            or cls._looks_like_primary_evidence_dump(clean, section_type="reading_flow")
            or cls._looks_like_internal_planner_copy(clean)
            or cls._looks_like_reader_metadata(clean)
            or cls._is_low_signal_reader_excerpt(clean)
            or not cls._is_reader_ready_summary(clean)
        ):
            return ""
        return clean

    @classmethod
    def _extract_adjacent_bridge_topics(cls, value: Any, *, limit: int = 2) -> List[str]:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(value, limit=180), limit=180)
        if not clean:
            return []
        topics: List[str] = []
        quoted_topics = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{3,80})[\"'“”‘’]", clean)
        topics.extend(
            cls._clean_excerpt(topic, limit=48)
            for topic in quoted_topics
            if cls._clean_excerpt(topic, limit=48)
        )
        labeled_topics = re.findall(r"\b(?:Fig(?:ure)?|Table|Equation)\s*\d+[A-Za-z]?\b", clean, flags=re.IGNORECASE)
        topics.extend(
            cls._clean_excerpt(topic, limit=32)
            for topic in labeled_topics
            if cls._clean_excerpt(topic, limit=32)
        )
        phrase_patterns = [
            r"(?:focus(?:es)? on|continue(?:s)? on|continue(?:s)? with|evaluation of|discussion on|implications for)\s+([A-Za-z][A-Za-z0-9' -]{5,72})",
            r"(?:with|about)\s+([A-Za-z][A-Za-z0-9' -]{5,72})",
        ]
        for pattern in phrase_patterns:
            for match in re.findall(pattern, clean, flags=re.IGNORECASE):
                topic = cls._clean_excerpt(cls._apply_light_repair_text(match), limit=56)
                if topic:
                    topics.append(topic)
        normalized: List[str] = []
        for topic in topics:
            stripped_topic = topic.strip(" .,:;")
            token = cls._localize_adjacent_bridge_topic(stripped_topic)
            if not token:
                fallback_topic = cls._clean_excerpt(stripped_topic, limit=56)
                if (
                    not fallback_topic
                    or cls._is_english_heavy_text(fallback_topic)
                    or cls._contains_long_raw_english_span(fallback_topic)
                    or re.search(r"(?:\.\.\.|…)", fallback_topic)
                    or re.search(r"\b(?:and|or|with|for|about|into|from)\b", fallback_topic, flags=re.IGNORECASE)
                ):
                    continue
                token = fallback_topic
            if (
                not token
                or token.lower().startswith(("this page", "next section", "discussion section", "continuation of"))
                or any(
                    marker in token.lower()
                    for marker in ("continues on", "continue on", "continue with", "focuses on")
                )
                or cls._looks_like_reader_metadata(token)
                or cls._looks_like_internal_planner_copy(token)
                or token.lower() in {item.lower() for item in normalized}
            ):
                continue
            normalized.append(token)
            if len(normalized) >= limit:
                break
        return normalized[:limit]

    @classmethod
    def _localize_adjacent_bridge_topic(cls, topic: str) -> str:
        clean = cls._clean_excerpt(str(topic or "").strip(), limit=72)
        if not clean:
            return ""
        lower = clean.lower()
        if re.search(r"(?:\.\.\.|…)", clean):
            return ""
        replacements = [
            ("evaluation of explanation quality", "对解释质量的评估"),
            ("explanation quality", "解释质量"),
            ("nonobvious insights", "非显而易见的洞见"),
            ("ai-generated explanations", "AI 生成解释"),
            ("generated explanations", "生成解释"),
            ("medical education", "医学教育"),
            ("ai limitations", "AI 的局限"),
            ("discussion section", "讨论部分"),
            ("implications for medical education", "对医学教育的意义"),
            ("accuracy of chat gpt on usmle", "ChatGPT 在 USMLE 上的准确率"),
            ("chat gpt on usmle", "ChatGPT 在 USMLE 上的表现"),
            ("explanation quality", "解释质量"),
            ("insight prevalence", "洞见出现情况"),
        ]
        localized = lower
        for source, target in replacements:
            localized = localized.replace(source, target)
        localized = localized.replace("figure", "Figure ").replace("fig ", "Fig ")
        localized = re.sub(r"\bnext section\b", "", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bthis page\b", "", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bwith\b", " ", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bon\b", " ", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\bof\b", " ", localized, flags=re.IGNORECASE)
        localized = re.sub(r"\s+", " ", localized).strip(" ,.;:，。；：")
        if not localized:
            return ""
        english_residue = localized
        for pattern in (
            r"\b(?:Fig|Figure|Table|Equation)\s*\d+[A-Za-z]?\b",
            r"\bUSMLE\b",
            r"\bChatGPT\b",
            r"\bAI\b",
            r"\bStep\s*1\b",
            r"\bStep\s*2(?:CK)?\b",
            r"\bStep\s*3\b",
        ):
            english_residue = re.sub(pattern, " ", english_residue, flags=re.IGNORECASE)
        english_residue = re.sub(r"[^A-Za-z]+", " ", english_residue).strip().lower()
        if english_residue and (
            cls._contains_long_raw_english_span(localized)
            or re.search(r"\b(?:and|or|with|for|about|into|from)\b", english_residue, flags=re.IGNORECASE)
            or len(english_residue.split()) >= 2
        ):
            return ""
        if re.search(r"[A-Za-z]", localized) and cls._is_english_heavy_text(localized):
            if "Fig " in localized or "Figure " in localized:
                figure_match = re.search(r"\b(?:Fig|Figure)\s*\d+[A-Za-z]?\b", clean, flags=re.IGNORECASE)
                if figure_match:
                    return cls._clean_excerpt(figure_match.group(0).replace("Figure", "Figure"), limit=40)
            return ""
        return cls._clean_excerpt(localized, limit=56)

    @classmethod
    def _compose_adjacent_bridge_from_context_row(cls, row: Mapping[str, Any]) -> str:
        relation = str(row.get("relation") or "").strip()
        topic_candidates: List[str] = []
        for value in (
            *list(row.get("continuation_hints") or []),
            row.get("summary"),
            row.get("body_text"),
            *list(row.get("figure_hints") or []),
            *[
                (
                    f"{str(item.get('label') or '').strip()} {str(item.get('description') or '').strip()}".strip()
                    if isinstance(item, Mapping)
                    else str(item or "").strip()
                )
                for item in list(row.get("figures") or [])
            ],
        ):
            topic_candidates.extend(cls._extract_adjacent_bridge_topics(value))
        topic_candidates = cls._dedupe_strings(topic_candidates, limit=2)
        if topic_candidates:
            topic_phrase = "、".join(topic_candidates)
            if relation == "next_page":
                return cls._clean_excerpt(
                    f"后文会继续展开 {topic_phrase} 这条线索，这里的说明已经把当前页的判断落了下来。",
                    limit=160,
                )
            return cls._clean_excerpt(
                f"前文铺开了 {topic_phrase} 这条线索，这一页继续把它发展成当前页的判断。",
                limit=160,
            )
        fallback_seed = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(
                next(
                    (
                        value
                        for value in (
                            *list(row.get("continuation_hints") or []),
                            row.get("summary"),
                            row.get("body_text"),
                        )
                        if str(value or "").strip()
                    ),
                    "",
                ),
                limit=120,
            ),
            limit=120,
        )
        if not fallback_seed:
            if relation == "next_page":
                return "后文会继续沿着这条线索展开。"
            return "前文先铺开了这条线索，这一页继续沿着它往下展开。"
        if cls._needs_display_localization(fallback_seed) or cls._is_english_heavy_text(fallback_seed):
            if relation == "next_page":
                return "后文会继续沿着这条线索展开。"
            return "前文先铺开了这条线索，这一页继续沿着它往下展开。"
        if relation == "next_page":
            return cls._clean_excerpt(f"后文会继续沿着这条线索展开：{fallback_seed}", limit=160)
        return cls._clean_excerpt(f"前文先铺开了这条线索：{fallback_seed}", limit=160)

    @classmethod
    def _adjacent_provenance_label(cls, *, relation: str, source: str) -> str:
        source_label = ""
        normalized_source = str(source or "").strip().lower()
        if normalized_source.endswith("ocr") or "ocr" in normalized_source:
            source_label = "OCR"
        elif normalized_source:
            source_label = "邻页解析"
        relation_label = "相邻页"
        if relation == "previous_page":
            relation_label = "上一页"
        elif relation == "next_page":
            relation_label = "下一页"
        return f"（承接{relation_label}{source_label and f' {source_label}'}）"

    @classmethod
    def _rewrite_adjacent_bridge_seed(cls, text: str, *, relation: str, source: str = "") -> str:
        clean = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean:
            return ""
        replacements = [
            (r"^上一页", "前文"),
            (r"^前一页", "前文"),
            (r"^下一页", "后文"),
            (r"^当前页延续(?:了)?上一页", "这里延续了前文"),
            (r"^当前页延续(?:了)?前一页", "这里延续了前文"),
            (r"^当前页承接", "这里承接"),
        ]
        for pattern, replacement in replacements:
            clean = re.sub(pattern, replacement, clean)
        if relation == "previous_page":
            if not re.match(r"^(?:前文|这里延续了前文|这里承接)", clean):
                clean = f"前文先铺开了这条线索：{clean}"
        elif relation == "next_page":
            if not re.match(r"^(?:后文|接下来)", clean):
                clean = f"后文会顺着这里继续：{clean}"
        elif not re.match(r"^(?:前文|后文|这里)", clean):
            clean = f"把它放回前后文里看：{clean}"
        provenance = cls._adjacent_provenance_label(relation=relation, source=source)
        if provenance and provenance not in clean:
            clean = f"{clean.rstrip('。')} {provenance}"
        if clean[-1] not in {"。", "！", "？"}:
            clean = f"{clean.rstrip('，,;；:：')}。"
        return cls._clean_excerpt(clean, limit=160)

    @classmethod
    def _strip_adjacent_bridge_provenance(cls, text: str) -> str:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=160)
        if not clean:
            return ""
        clean = re.sub(r"\s*（承接[^）]+）(?=[。！？]?$)\s*", "", clean).strip()
        return cls._clean_excerpt(clean, limit=140)

    @classmethod
    def _derive_adjacent_bridge_cues(
        cls,
        adjacent_rows: Sequence[Mapping[str, Any]],
        *,
        limit: int = 2,
    ) -> List[Dict[str, Any]]:
        cues: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in list(adjacent_rows or []):
            if not isinstance(row, Mapping):
                continue
            relation = str(row.get("relation") or "").strip()
            candidates = [
                *list(row.get("continuation_hints") or []),
                row.get("summary"),
                *list(row.get("figure_hints") or []),
                *list(row.get("table_hints") or []),
                *list(row.get("equation_hints") or []),
            ]
            for candidate in candidates:
                seed = cls._normalize_adjacent_bridge_seed(candidate)
                if not seed:
                    continue
                cue_text = cls._rewrite_adjacent_bridge_seed(
                    seed,
                    relation=relation,
                    source=str(row.get("source") or "").strip(),
                )
                if not cue_text:
                    continue
                key = cue_text.lower()
                if key in seen:
                    continue
                seen.add(key)
                cues.append(
                    {
                        "text": cue_text,
                        "provenance": {
                            "page": int(row.get("page") or 0),
                            "relation": relation,
                            "source": str(row.get("source") or "").strip(),
                            "reference_only": bool(row.get("reference_only")),
                        },
                    }
                )
                break
            if len(cues) < limit and not any(
                str(item.get("provenance", {}).get("page") or 0) == str(int(row.get("page") or 0))
                for item in cues
            ):
                fallback_seed = cls._clean_excerpt(cls._sanitize_reader_facing_text(candidates[0] if candidates else "", limit=140), limit=140)
                if (
                    fallback_seed
                    and not cls._needs_display_localization(fallback_seed)
                    and not cls._is_english_heavy_text(fallback_seed)
                ):
                    cue_text = cls._rewrite_adjacent_bridge_seed(
                        fallback_seed,
                        relation=relation,
                        source=str(row.get("source") or "").strip(),
                    )
                    if cue_text and cue_text.lower() not in seen:
                        seen.add(cue_text.lower())
                        cues.append(
                            {
                                "text": cue_text,
                                "provenance": {
                                    "page": int(row.get("page") or 0),
                                    "relation": relation,
                                    "source": str(row.get("source") or "").strip(),
                                    "reference_only": bool(row.get("reference_only")),
                                },
                            }
                        )
            if len(cues) < limit and not any(
                str(item.get("provenance", {}).get("page") or 0) == str(int(row.get("page") or 0))
                for item in cues
            ):
                fallback_from_context = cls._compose_adjacent_bridge_from_context_row(row)
                if fallback_from_context and fallback_from_context.lower() not in seen:
                    cue_text = cls._rewrite_adjacent_bridge_seed(
                        fallback_from_context,
                        relation=relation,
                        source=str(row.get("source") or "").strip(),
                    )
                    if cue_text and cue_text.lower() not in seen:
                        seen.add(cue_text.lower())
                        cues.append(
                            {
                                "text": cue_text,
                                "provenance": {
                                    "page": int(row.get("page") or 0),
                                    "relation": relation,
                                    "source": str(row.get("source") or "").strip(),
                                    "reference_only": bool(row.get("reference_only")),
                                },
                            }
                        )
            if len(cues) >= limit:
                break
        return cues

    @classmethod
    def _build_adjacent_page_continuity_rows(
        cls,
        *,
        adjacent_rows: Sequence[Mapping[str, Any]],
        adjacent_bridge_cues: Sequence[Mapping[str, Any]],
        limit: int = 2,
    ) -> List[Dict[str, Any]]:
        raw_rows = [dict(row) for row in list(adjacent_rows or []) if isinstance(row, Mapping)]
        cue_rows = [dict(item) for item in list(adjacent_bridge_cues or []) if isinstance(item, Mapping)]
        cue_by_page = {
            int(dict(item.get("provenance") or {}).get("page") or 0): item
            for item in cue_rows
            if int(dict(item.get("provenance") or {}).get("page") or 0) > 0
        }
        continuity_rows: List[Dict[str, Any]] = []
        for row in raw_rows[:limit]:
            relation = str(row.get("relation") or "").strip()
            page = int(row.get("page") or 0)
            cue = dict(cue_by_page.get(page) or {})
            summary = cls._strip_adjacent_bridge_provenance(str(cue.get("text") or "").strip())
            if summary and (
                cls._is_english_heavy_text(summary)
                or cls._looks_like_primary_evidence_dump(summary, section_type="reading_flow")
                or cls._looks_like_internal_planner_copy(summary)
                or cls._is_generic_narrative_summary(summary)
            ):
                summary = ""
            if not summary:
                summary = cls._strip_adjacent_bridge_provenance(cls._compose_adjacent_bridge_from_context_row(row))
            if summary and cls._is_english_heavy_text(summary):
                summary = ""
            if not summary:
                subject = cls._extract_adjacent_bridge_subject(cls._compose_adjacent_bridge_from_context_row(row))
                if subject:
                    summary = (
                        f"后文会继续展开{subject}这条线索。"
                        if relation == "next_page"
                        else f"前文铺开了{subject}这条线索。"
                    )
            continuity_rows.append(
                {
                    "page": page,
                    "relation": relation,
                    "summary": summary,
                    "continuation_hints": [],
                }
            )
        if continuity_rows:
            return continuity_rows[:limit]
        for cue in cue_rows[:limit]:
            provenance = dict(cue.get("provenance") or {})
            summary = cls._strip_adjacent_bridge_provenance(str(cue.get("text") or "").strip())
            if not summary or cls._is_english_heavy_text(summary):
                continue
            continuity_rows.append(
                {
                    "page": int(provenance.get("page") or 0),
                    "relation": str(provenance.get("relation") or "").strip(),
                    "summary": summary,
                    "continuation_hints": [],
                }
            )
        return continuity_rows[:limit]

    @classmethod
    def _compose_adjacent_reading_flow_summary(
        cls,
        cue_text: str,
        *,
        focus_label: str = "",
        readable_claim: str = "",
        anchor_terms: Sequence[str] = (),
    ) -> str:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(cue_text, limit=180), limit=180)
        if not clean:
            return ""

        subject = cls._extract_adjacent_bridge_subject(clean)
        focus_token = cls._clean_excerpt(str(focus_label or "").strip(), limit=60)
        claim_token = cls._clean_excerpt(str(readable_claim or "").strip(), limit=120)
        comparison_phrase = cls._comparison_focus_phrase(
            [
                cls._clean_excerpt(str(item or "").strip(), limit=20)
                for item in list(anchor_terms or [])
                if cls._clean_excerpt(str(item or "").strip(), limit=20)
                and cls._clean_excerpt(str(item or "").strip(), limit=20) != focus_token
            ],
            limit=2,
        )
        claim_numbers = cls._extract_grounding_numbers(claim_token, limit=1)
        if focus_token and comparison_phrase and claim_numbers:
            main_clause = (
                f"正文会把 {focus_token} 里{comparison_phrase}继续拆开来讲，交代像 {claim_numbers[0]} "
                "这样的总体结果究竟由哪些差异撑起来。"
            )
        elif focus_token and comparison_phrase:
            main_clause = f"正文会把 {focus_token} 里{comparison_phrase}继续拆开来讲，把图上的对照串成作者真正关心的判断和解释链。"
        elif focus_token and claim_numbers:
            main_clause = f"正文会把 {focus_token} 里的比较继续展开，也交代像 {claim_numbers[0]} 这样的总体结果究竟由哪些差异撑起来。"
        elif focus_token:
            main_clause = f"正文会把 {focus_token} 里的比较继续展开，把图上的差异一步步串成作者真正关心的判断和解释链。"
        elif claim_numbers:
            main_clause = f"正文会把当前页的结果继续展开，也交代像 {claim_numbers[0]} 这样的总体结果究竟由哪些差异撑起来。"
        elif claim_token:
            main_clause = "正文会把当前页的结果继续展开，把前面的对照一步步串成作者真正关心的判断。"
        else:
            main_clause = "正文会把这一页的结果继续展开，把图上看到的差异一步步解释成作者真正要说明的判断。"
        if re.search(r"(?:^后文|后面|接下来)", clean) and "前文" not in clean:
            if subject:
                return cls._clean_excerpt(
                    f"这一页已经把{subject}这条线索接到当前结果上，后文还会继续展开。{main_clause}",
                    limit=220,
                )
            return cls._clean_excerpt(
                f"这一页已经把当前结果接了下来，后文还会沿着这条线索继续展开。{main_clause}",
                limit=220,
            )
        if subject:
            generic_subject = bool(
                any(token in subject for token in ("图示", "图表", "正文", "线索", "背景", "比较基线", "图示说明"))
                and not any(token in subject for token in ("Concordance", "Insight", "USMLE", "解释质量", "非显而易见", "有效性", "新颖性"))
            )
            if generic_subject:
                return cls._clean_excerpt(
                    f"顺着当前页正文往下读时，也带着前文关于{subject}的铺垫；{main_clause}",
                    limit=240,
                )
            return cls._clean_excerpt(
                f"前文铺开了关于{subject}的线索，这一页继续把它接到当前结果上。{main_clause}",
                limit=240,
            )
        if "前文" in clean or "上一页" in clean:
            return cls._clean_excerpt(
                f"这一页接着前文的线索往下讲。{main_clause}",
                limit=240,
            )
        return cls._clean_excerpt(
            f"这一段和前后文是连着的。{main_clause}",
            limit=240,
        )

    @classmethod
    def _compose_adjacent_bridge_note(cls, cue_text: str) -> str:
        clean = cls._strip_adjacent_bridge_provenance(str(cue_text or "").strip())
        if not clean:
            return ""
        subject = cls._extract_adjacent_bridge_subject(clean)
        if re.search(r"(?:^后文|后面|接下来)", clean) and "前文" not in clean:
            if subject:
                return cls._clean_excerpt(
                    f"读到这里时，先把当前页围绕{subject}的解释读稳；后面还会继续展开。",
                    limit=220,
                )
            return "读到这里时，先把当前页的解释读稳，后面还会沿着这条线索继续展开。"
        if subject:
            return cls._clean_excerpt(
                f"读到这里时，先接上前文关于{subject}的铺垫，再看作者怎样把当前页的结果讲清楚。",
                limit=220,
            )
        if "前文" in clean or "上一页" in clean:
            return "读到这里时，先把前文那条线索接上，再看作者怎样把当前页的结果讲清楚。"
        return "读到这里时，先把这条前后文线索接上，再看作者怎样把当前页的结果讲清楚。"

    @classmethod
    def _extract_adjacent_bridge_subject(cls, cue_text: str) -> str:
        clean = cls._strip_adjacent_bridge_provenance(str(cue_text or "").strip())
        if not clean:
            return ""
        direct_topics = cls._extract_adjacent_bridge_topics(clean, limit=2)
        if direct_topics:
            return "、".join(direct_topics)
        subject = re.sub(r"^(?:前文|后文|这里)\s*", "", clean)
        subject = re.sub(
            r"^(?:先铺开了|延续了前文的|承接前文的|会继续展开|会继续沿着|会顺着这里继续|顺着这里继续|继续沿着)\s*",
            "",
            subject,
        )
        subject = re.sub(
            r"(?:这条线索|这一页继续沿着它往下展开|这里先把当前页的解释和判断读稳|这里先把当前页的解释读稳|当前页继续往下讲|再往下展开).*$",
            "",
            subject,
        )
        subject = subject.strip(" ：:，,。；;")
        if (
            not subject
            or subject in {"会继续沿着", "继续沿着", "往下展开", "承上启下"}
            or cls._is_english_heavy_text(subject)
            or cls._needs_display_localization(subject, short_form=len(subject) <= 80)
        ):
            return ""
        return cls._clean_excerpt(subject, limit=56)

    @classmethod
    def _build_teacher_narrative_spine(
        cls,
        *,
        page_brief: Mapping[str, Any],
        story_substrate: Mapping[str, Any],
        focus_label: str,
        adjacent_bridge_cues: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        claims = [dict(item) for item in list(story_substrate.get("main_claims") or []) if isinstance(item, Mapping)]
        readable_claim = next(
            (
                cls._clean_excerpt(str(item.get("display_text") or item.get("text") or "").strip(), limit=120)
                for item in claims
                if str(item.get("display_text") or item.get("text") or "").strip()
                and not cls._is_english_heavy_text(str(item.get("display_text") or item.get("text") or "").strip())
            ),
            "",
        )
        terms = []
        for item in list(story_substrate.get("terms_to_explain") or []):
            if not isinstance(item, Mapping):
                continue
            raw_term = str(item.get("term") or "").strip()
            if not raw_term:
                continue
            normalized_terms = cls._collect_reader_anchor_terms(raw_term, limit=2)
            for candidate in normalized_terms:
                token = cls._clean_excerpt(str(candidate or "").strip(), limit=24)
                if token and token not in terms:
                    terms.append(token)
                if len(terms) >= 3:
                    break
            if len(terms) >= 3:
                break
        resource_gaps = [
            str(item).strip()
            for item in list(page_brief.get("resource_gaps") or [])[:2]
            if str(item).strip()
        ]
        resource_gap_terms = cls._collect_reader_anchor_terms(*resource_gaps, limit=2)
        adjacent_text = str((adjacent_bridge_cues[0] or {}).get("text") or "").strip() if adjacent_bridge_cues else ""
        anchor_terms = cls._collect_reader_anchor_terms(
            focus_label,
            readable_claim,
            *terms,
            *resource_gaps,
        )
        narrative_terms = cls._titleworthy_grounding_terms([*terms, *anchor_terms], limit=2)
        joined_terms = cls._join_display_terms(narrative_terms)
        comparison_phrase = cls._comparison_focus_phrase(narrative_terms, limit=2)
        claim_numbers = cls._extract_grounding_numbers(readable_claim, limit=1)
        resource_context = cls._join_display_terms(resource_gap_terms)

        opening = ""
        if focus_label and comparison_phrase and claim_numbers:
            opening = f"这一页重要，不只因为 {focus_label} 摆出了结果，更因为它让你能判断像 {claim_numbers[0]} 这样的总体结果究竟由哪些{comparison_phrase}撑起来"
            if resource_context:
                opening += f"，也能看出 {resource_context} 这层背景怎样进入同一次比较"
            opening += "。"
        elif focus_label and comparison_phrase:
            opening = f"这一页重要，不只因为 {focus_label} 摆出了结果，更因为它把{comparison_phrase}放进同一套比较框架"
            if resource_context:
                opening += f"，也把 {resource_context} 这层背景纳入同一次对照"
            opening += "，让你看见作者究竟据什么来判断差异。"
        elif focus_label and claim_numbers:
            opening = f"这一页重要，在于 {focus_label} 不只是给出结果，也让像 {claim_numbers[0]} 这样的总体判断有了可以回看的比较框架。"
        elif focus_label and readable_claim:
            opening = f"这一页重要，在于 {focus_label} 先摆出关键差异，正文再把这些差异解释成作者真正关心的判断。"
        elif focus_label:
            opening = f"这一页重要，在于 {focus_label} 先把关键比较摆到台面上，让后面的正文有了明确要解释的对象。"
        else:
            opening = "这一页的重要性在于，关键比较先被摆到台面上，后面的正文再解释这些结果为什么成立。"

        if focus_label and comparison_phrase and claim_numbers:
            body_guidance = (
                f"正文会顺着 {focus_label} 里{comparison_phrase}往下讲，交代像 {claim_numbers[0]} "
                "这样的总体结果究竟由哪些差异撑起来。"
            )
        elif focus_label and comparison_phrase:
            body_guidance = f"正文会顺着 {focus_label} 里{comparison_phrase}往下讲，把图上的对照串成作者真正关心的判断和解释链。"
        elif focus_label and claim_numbers:
            body_guidance = f"正文会顺着 {focus_label} 里的比较往下讲，也交代像 {claim_numbers[0]} 这样的总体结果究竟由哪些差异撑起来。"
        elif focus_label and readable_claim:
            body_guidance = f"正文会顺着 {focus_label} 里的比较往下讲，把图上的差异一步步串成作者真正关心的判断。"
        elif focus_label:
            body_guidance = f"正文会顺着 {focus_label} 里的比较往下讲，把前面的对照一步步串成作者真正关心的判断和解释链。"
        else:
            body_guidance = "正文不是在重述结果，而是在把前面的对照一步步解释成作者真正关心的判断。"
        if adjacent_text:
            body_guidance = cls._compose_adjacent_reading_flow_summary(
                adjacent_text,
                focus_label=focus_label,
                readable_claim=readable_claim,
                anchor_terms=anchor_terms,
            )
        if focus_label and comparison_phrase and claim_numbers:
            focus_guidance = f"{focus_label} 最值得注意的是，{comparison_phrase}被并排摆在一起，所以像 {claim_numbers[0]} 这样的总体结果不再只是孤立数字。"
        elif focus_label and comparison_phrase:
            focus_guidance = f"{focus_label} 最值得注意的是，{comparison_phrase}被并排摆在一起，能分清哪些变化会进入后面的解释，哪些只是局部起伏。"
        elif focus_label and claim_numbers:
            focus_guidance = f"{focus_label} 最值得注意的是，它把关键比较先摆齐，也让像 {claim_numbers[0]} 这样的总体结果有了参照。"
        elif focus_label:
            focus_guidance = f"{focus_label} 最值得注意的是，它把这一页要解释的关键比较先摆在一起，方便分清后面正文各自在回应哪一种差异。"
        else:
            focus_guidance = "当前页最强的图或证据先把关键差异摆出来，方便看清后面的正文到底在解释什么。"

        if len(narrative_terms) == 1:
            term_guidance = (
                f"这里会先把{narrative_terms[0]}这个判断词讲清楚：它决定了 {focus_label or '这一页'} 里的比较到底在衡量什么，"
                "也决定后面正文是在解释哪一类差异。"
            )
        elif narrative_terms:
            term_guidance = (
                f"这里会先把 {joined_terms} 这些判断词拆开：它们在 {focus_label or '这一页'} 里看起来放在一起，"
                "但各自回答的问题不同，读懂后才不会把不同层次的比较混成一句话。"
            )
        else:
            term_guidance = "这里补的不是再说一遍结论，而是把会卡住理解的术语拆开，让你知道每个词在这一页究竟负责解释什么。"
        support_guidance = (
            f"这里补 {cls._join_display_terms(resource_gap_terms)} 的背景，不是另起一条线，而是帮你认出 {focus_label or '当前页'} "
            "里的比较各自对应什么对象或场景，为什么作者要这样分层。"
            if resource_gap_terms else
            f"这里补的是理解 {focus_label or '当前页'} 所需的背景，不是重复结论，而是帮你认出这些比较各自落在哪些对象或现实场景上。"
        )
        return {
            "opening": cls._clean_excerpt(opening, limit=220),
            "focus_guidance": cls._clean_excerpt(focus_guidance, limit=240),
            "body_guidance": cls._clean_excerpt(body_guidance, limit=240),
            "term_guidance": cls._clean_excerpt(term_guidance, limit=200),
            "support_guidance": cls._clean_excerpt(support_guidance, limit=220),
            "adjacent_bridge_cues": [dict(item) for item in list(adjacent_bridge_cues or []) if isinstance(item, Mapping)],
            "anchor_terms": anchor_terms,
            "page_anchor": focus_label or "当前页",
        }

    @classmethod
    def _teacher_guidance_for_section_type(
        cls,
        *,
        section_type: str,
        teacher_spine: Mapping[str, Any],
    ) -> str:
        return str(
            {
                "hero": teacher_spine.get("opening"),
                "focus_stage": teacher_spine.get("focus_guidance"),
                "reading_flow": teacher_spine.get("body_guidance"),
                "explainer_cluster": teacher_spine.get("term_guidance"),
                "supporting_resources": teacher_spine.get("support_guidance"),
            }.get(str(section_type or "").strip(), "")
            or ""
        ).strip()

    @staticmethod
    def _manuscript_title_for_segment_type(segment_type: str, focus_label: str = "") -> str:
        segment_token = str(segment_type or "").strip()
        focus_token = str(focus_label or "").strip()
        if focus_token.lower() in {"当前焦点", "current focus", "focus", "page focus"}:
            focus_token = ""
        if segment_token == "opening":
            return f"{focus_token} 与这一页的主结论" if focus_token else "这一页的核心结论"
        if segment_token == "figure":
            return f"{focus_token} 的关键比较" if focus_token else "这张图的关键比较"
        if segment_token == "body":
            return "正文如何解释这些结果"
        if segment_token == "wrapup":
            return "理解结果需要的背景"
        return "相关解释"

    @classmethod
    def _manuscript_title_needs_repair(cls, *, segment_type: str, title: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(title, limit=80), limit=80)
        if not clean:
            return True
        if cls._looks_like_internal_planner_copy(clean) or cls._is_reader_surface_noise(clean):
            return True
        generic_titles = {
            "先把这页最重要的判断和证据对上",
            "这张图里最重要的差异",
            "正文怎样把前面的比较讲成判断",
            "先抓住这一页在讲什么",
            "先看最关键的图示",
            "继续往下读",
            "读到卡住时再补的背景",
        }
        if clean in generic_titles:
            return True
        if segment_type in {"opening", "figure", "focus", "body"} and bool(
            re.search(r"(?:核心问题|真正说明什么|把结果讲具体|先抓住这一页|先看最关键的图示)", clean)
        ):
            return True
        if re.search(r"^先在 .+ 里找到 .+ 这个结果$", clean):
            return True
        if re.search(r"^再看 .+ 怎样(?:被解释|解释)$", clean):
            return True
        return False

    @classmethod
    def _target_ids_overlap(cls, left: Sequence[Any], right: Sequence[Any]) -> bool:
        left_tokens = {str(item or "").strip() for item in list(left or []) if str(item or "").strip()}
        right_tokens = {str(item or "").strip() for item in list(right or []) if str(item or "").strip()}
        if not left_tokens or not right_tokens:
            return False
        for left_token in left_tokens:
            left_suffix = left_token.split(":")[-1]
            for right_token in right_tokens:
                right_suffix = right_token.split(":")[-1]
                if left_token == right_token or left_suffix == right_suffix:
                    return True
        return False

    @classmethod
    def _resolve_teaching_manuscript_anchor_excerpt(
        cls,
        *,
        target_ids: Sequence[str],
        target_map: Mapping[str, Any],
        limit: int = 180,
        prefer_body_excerpt: bool = False,
        suppress_english_heavy: bool = False,
    ) -> str:
        best_excerpt = ""
        best_score = -999
        primary_target: Dict[str, Any] = {}
        for target_id in [str(item).strip() for item in list(target_ids or []) if str(item).strip()]:
            target = dict(target_map.get(target_id) or {})
            if not primary_target:
                primary_target = dict(target)
            target_kind = str(target.get("target_kind") or "").strip().lower()
            body_like_target = target_kind not in {"figure", "table", "equation"}
            excerpt_candidate = cls._best_reader_facing_excerpt(
                target.get("excerpt"),
                tool_name="paper_read",
                limit=limit,
            )
            candidate_rows = [
                (excerpt_candidate, 42 if body_like_target else 24),
                (target.get("excerpt"), 20 if body_like_target else 8),
                (target.get("summary"), 18 if body_like_target else 10),
                (target.get("title"), 14 if body_like_target else 6),
                (target.get("section_label"), 12 if body_like_target else 4),
                (target.get("figure_label"), 4),
            ]
            for candidate, bonus in candidate_rows:
                raw = cls._clean_excerpt(
                    cls._sanitize_reader_facing_text(candidate, limit=limit),
                    limit=limit,
                )
                if (
                    not raw
                    or cls._is_reader_surface_noise(raw)
                    or cls._looks_like_reader_metadata(raw)
                    or cls._looks_like_heading_only(raw)
                ):
                    continue
                score = cls._score_reader_facing_text(raw, tool_name="paper_read") + bonus
                if prefer_body_excerpt and body_like_target:
                    score += 18
                if cls._looks_like_primary_evidence_dump(raw, section_type="reading_flow"):
                    score -= 40
                if suppress_english_heavy and cls._needs_display_localization(raw, short_form=len(raw) <= 120):
                    score -= 60
                if score > best_score:
                    best_excerpt = raw
                    best_score = score
        if best_excerpt:
            if suppress_english_heavy and cls._needs_display_localization(best_excerpt, short_form=len(best_excerpt) <= 120):
                return ""
            if cls._looks_like_heading_only(best_excerpt):
                fallback_excerpt = cls._compose_teaching_manuscript_anchor_fallback(
                    target=primary_target,
                    prefer_body_excerpt=prefer_body_excerpt,
                )
                if fallback_excerpt:
                    return fallback_excerpt
        return best_excerpt

    @classmethod
    def _compose_teaching_manuscript_anchor_fallback(
        cls,
        *,
        target: Mapping[str, Any],
        prefer_body_excerpt: bool = False,
    ) -> str:
        current = dict(target or {})
        for candidate in (
            current.get("excerpt"),
            current.get("summary"),
            current.get("title"),
            current.get("section_label"),
            current.get("figure_label"),
        ):
            raw = cls._clean_excerpt(cls._sanitize_reader_facing_text(candidate, limit=180), limit=180)
            if (
                raw
                and not cls._is_reader_surface_noise(raw)
                and not cls._looks_like_reader_metadata(raw)
                and not cls._looks_like_heading_only(raw)
            ):
                return raw
        return ""

    @classmethod
    def _looks_like_synthetic_anchor_excerpt(cls, text: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=180), limit=180)
        if not clean:
            return False
        return bool(
            re.search(r"(?:这一页最关键的比较图|这一页最关键的图示)", clean)
            or re.search(r"(?:这一段在继续解释当前页的关键结果|这一段承接了当前页的核心解释)", clean)
        )

    @classmethod
    def _compose_teaching_manuscript_body_emphasis(
        cls,
        *,
        target_ids: Sequence[str],
        target_map: Mapping[str, Any],
        focus_label: str,
    ) -> str:
        focus_token = cls._clean_excerpt(str(focus_label or "").strip(), limit=80)
        for target_id in [str(item).strip() for item in list(target_ids or []) if str(item).strip()]:
            target = dict(target_map.get(target_id) or {})
            if not cls._is_body_reading_target(target):
                continue
            section_label = cls._clean_excerpt(str(target.get("section_label") or "").strip(), limit=60)
            title = cls._clean_excerpt(str(target.get("title") or "").strip(), limit=60)
            excerpt = cls._best_reader_facing_excerpt(
                target.get("excerpt"),
                tool_name="paper_read",
                limit=140,
            )
            excerpt = cls._clean_excerpt(excerpt, limit=140) if excerpt else ""
            if excerpt and not cls._needs_display_localization(excerpt, short_form=len(excerpt) <= 120):
                return f"这段正文先交代：{excerpt}"
            if section_label and not cls._needs_display_localization(section_label, short_form=True):
                if focus_token:
                    return f"{section_label} 这一段把 {focus_token} 里的结果继续展开成正文解释。"
                return f"{section_label} 这一段把当前页的结果继续展开成正文解释。"
            if title and not cls._needs_display_localization(title, short_form=True):
                if focus_token:
                    return f"{title} 这一段解释了 {focus_token} 背后的含义和判断。"
                return f"{title} 这一段解释了当前页结果背后的含义和判断。"
            if focus_token:
                return f"正文里的关键结果段落，会把 {focus_token} 里的比较结果落到作者的判断里。"
            return "正文里的关键结果段落，会把这些比较结果落到作者的判断里。"
        if focus_token:
            return f"直接解释 {focus_token} 的正文段落，会把这些比较结果落到作者的判断里。"
        return ""

    @classmethod
    def _collect_teaching_manuscript_terms(
        cls,
        *,
        story_substrate: Mapping[str, Any],
        target_ids: Sequence[str],
        limit: int = 2,
    ) -> List[str]:
        matched: List[str] = []
        fallback: List[str] = []
        for row in list(story_substrate.get("terms_to_explain") or []):
            if not isinstance(row, Mapping):
                continue
            term = cls._clean_excerpt(str(row.get("term") or "").strip(), limit=60)
            if not term:
                continue
            source_target_ids = [
                str(item).strip()
                for item in list(row.get("source_target_ids") or row.get("target_ids") or [])
                if str(item).strip()
            ]
            if target_ids and source_target_ids and cls._target_ids_overlap(source_target_ids, target_ids):
                matched.append(term)
            else:
                fallback.append(term)
        return cls._dedupe_strings(matched or fallback, limit=limit)

    @classmethod
    def _compose_teaching_manuscript_context_emphasis(
        cls,
        *,
        segment_type: str,
        story_substrate: Mapping[str, Any],
        target_ids: Sequence[str],
        focus_label: str,
        primary_text: str = "",
    ) -> str:
        focus_token = cls._clean_excerpt(str(focus_label or "").strip(), limit=80)
        useful_primary = cls._clean_excerpt(str(primary_text or "").strip(), limit=180)
        if useful_primary and (
            cls._looks_like_generic_helper_summary(useful_primary)
            or cls._is_generic_narrative_summary(useful_primary)
            or "建议结合原文逐句核对" in useful_primary
        ):
            useful_primary = ""
        terms = cls._collect_teaching_manuscript_terms(
            story_substrate=story_substrate,
            target_ids=target_ids,
            limit=2,
        )
        term_text = "、".join(terms[:2])
        background_topics = [
            cls._clean_excerpt(str(item.get("topic") or "").strip(), limit=40)
            for item in list(story_substrate.get("background_gaps") or [])
            if isinstance(item, Mapping) and str(item.get("topic") or "").strip()
        ]
        background_text = "、".join(cls._dedupe_strings(background_topics, limit=2))
        if segment_type == "opening":
            if useful_primary:
                return f"这一页最重要的结论是：{useful_primary}"
            if focus_token and term_text:
                return f"这一页围绕 {focus_token} 展开，{term_text} 这些关键词会把主要解释线索串起来。"
            if term_text:
                return f"{term_text} 这些关键词会把这一页的主要结论串起来。"
            return ""
        if segment_type == "figure":
            if focus_token and term_text:
                return f"{focus_token} 里和 {term_text} 有关的比较承载了这一页最关键的结果，正文随后解释这些差异的含义。"
            if focus_token and useful_primary:
                return f"{focus_token} 承载了这一页的核心比较，并支撑“{useful_primary}”这个判断。"
            return ""
        if segment_type == "body":
            if focus_token and term_text:
                return f"正文会解释 {term_text} 这些词怎样把 {focus_token} 里的比较结果落到作者的判断里。"
            if term_text:
                return f"正文会解释 {term_text} 这些词怎样串起作者的判断。"
            if background_text:
                return f"正文会把当前结果进一步连到 {background_text} 这层背景上。"
        if segment_type == "wrapup" and background_text:
            return f"{background_text} 这层背景能帮助解释当前页的结果为什么重要。"
        return ""

    @classmethod
    def _compose_glossary_note_from_reason(
        cls,
        *,
        term: str,
        reason: str,
        focus_label: str,
    ) -> str:
        reason_token = str(reason or "").strip().lower()
        focus_token = str(focus_label or "").strip() or "这一页"
        if "metric" in reason_token:
            return f"{term} 是这里用来判断 {focus_token} 中比较结果是否一致的指标。"
        if "context" in reason_token:
            return f"{term} 是读懂 {focus_token} 时需要先补上的背景概念。"
        if "concept" in reason_token or "term" in reason_token:
            return f"{term} 是这一页继续往下读时会反复遇到的关键概念。"
        return f"{term} 是继续理解 {focus_token} 时需要先补上的概念。"

    def _collect_teaching_manuscript_glossary(
        self,
        *,
        story_substrate: Mapping[str, Any],
        interaction_modules: Sequence[Mapping[str, Any]],
        target_ids: Sequence[str],
        focus_label: str,
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        glossary_terms: List[Dict[str, Any]] = []
        glossary_definitions: Dict[str, Dict[str, Any]] = {}
        for module in list(interaction_modules or []):
            if str(module.get("module_type") or "").strip() != "GlossaryPanel":
                continue
            module_target_ids = [str(item).strip() for item in list(module.get("target_ids") or []) if str(item).strip()]
            if target_ids and module_target_ids and not self._target_ids_overlap(module_target_ids, target_ids):
                continue
            for row in list(dict(module.get("props") or {}).get("terms") or []):
                if not isinstance(row, Mapping):
                    continue
                term = self._clean_excerpt(str(row.get("term") or "").strip(), limit=80)
                note = self._clean_excerpt(
                    self._sanitize_reader_facing_text(
                        row.get("definition") or row.get("note") or row.get("summary"),
                        limit=180,
                    ),
                    limit=180,
                )
                if not term or not note:
                    continue
                glossary_definitions.setdefault(
                    term.lower(),
                    {
                        "term": term,
                        "note": note,
                        "target_ids": module_target_ids,
                    },
                )

        for row in list(story_substrate.get("terms_to_explain") or []):
            if not isinstance(row, Mapping):
                continue
            source_target_ids = [
                str(item).strip()
                for item in list(row.get("source_target_ids") or row.get("target_ids") or [])
                if str(item).strip()
            ]
            if target_ids and source_target_ids and not self._target_ids_overlap(source_target_ids, target_ids):
                continue
            term = self._clean_excerpt(str(row.get("term") or "").strip(), limit=80)
            if not term:
                continue
            definition = glossary_definitions.get(term.lower())
            note = ""
            if definition:
                note = str(definition.get("note") or "").strip()
            if not note:
                note = self._compose_glossary_note_from_reason(
                    term=term,
                    reason=str(row.get("reason") or "").strip(),
                    focus_label=focus_label,
                )
            note = self._clean_excerpt(self._sanitize_reader_facing_text(note, limit=180), limit=180)
            if (
                not note
                or self._looks_like_internal_planner_copy(note)
                or self._is_reader_surface_noise(note)
            ):
                continue
            glossary_terms.append(
                {
                    "term": term,
                    "note": note,
                    "target_ids": source_target_ids or list(definition.get("target_ids") or []) if definition else [],
                }
            )

        deduped_terms: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in glossary_terms:
            key = str(row.get("term") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped_terms.append(row)
        return deduped_terms[:limit]

    def _collect_teaching_manuscript_reference_links(
        self,
        *,
        resource_modules: Sequence[Mapping[str, Any]],
        packet: Optional[Mapping[str, Any]],
        target_ids: Sequence[str],
        limit: int = 2,
    ) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        fallback_normalized: List[Dict[str, str]] = []
        packet_objective = next(
            (
                str(item).strip()
                for item in list((packet or {}).get("tool_objectives") or [])
                if str(item).strip()
            ),
            "why_it_matters",
        )
        packet_links = self._normalize_public_links(
            [dict(item) for item in list((packet or {}).get("public_links") or []) if isinstance(item, Mapping)],
            limit=limit,
        )
        for row in packet_links:
            label = str(row.get("label") or row.get("href") or "").strip()
            note = self._clean_excerpt(
                self._sanitize_reader_facing_text(row.get("justification") or row.get("snippet"), limit=140),
                limit=140,
            )
            if not note or self._is_generic_reference_summary(note):
                note = self._rewrite_reader_facing_reference(
                    label=label,
                    snippet=str(row.get("snippet") or "").strip(),
                    objective=packet_objective,
                )
            normalized.append(
                {
                    "label": label,
                    "href": str(row.get("href") or "").strip(),
                    "note": note,
                }
            )
            fallback_normalized.append(
                {
                    "label": label,
                    "href": str(row.get("href") or "").strip(),
                    "note": note,
                }
            )
        for module in list(resource_modules or []):
            module_target_ids = [str(item).strip() for item in list(module.get("target_ids") or []) if str(item).strip()]
            target_overlap = not target_ids or not module_target_ids or self._target_ids_overlap(module_target_ids, target_ids)
            module_summary = self._clean_excerpt(
                self._sanitize_reader_facing_text(
                    module.get("display_summary") or module.get("summary"),
                    limit=140,
                ),
                limit=140,
            )
            module_links = self._normalize_public_links(
                [dict(item) for item in list(module.get("links") or []) if isinstance(item, Mapping)],
                limit=limit,
            )
            module_objective = (
                "figure_context"
                if str(module.get("module_type") or "").strip() == "FigureExplainPanel"
                else "why_it_matters"
            )
            for row in module_links:
                label = str(row.get("label") or row.get("href") or "").strip()
                note = self._clean_excerpt(
                    self._sanitize_reader_facing_text(
                        row.get("justification") or row.get("snippet") or module_summary,
                        limit=140,
                    ),
                    limit=140,
                )
                if not note or self._is_generic_reference_summary(note):
                    note = self._rewrite_reader_facing_reference(
                        label=label,
                        snippet=str(row.get("snippet") or module_summary or "").strip(),
                        objective=module_objective,
                    )
                normalized_row = {
                    "label": label,
                    "href": str(row.get("href") or "").strip(),
                    "note": note,
                }
                fallback_normalized.append(normalized_row)
                if target_overlap:
                    normalized.append(normalized_row)
        deduped: List[Dict[str, str]] = []
        href_index: Dict[str, int] = {}
        seed_rows = normalized or fallback_normalized
        for row in seed_rows:
            href = str(row.get("href") or "").strip()
            if not href or not self._is_reader_worthy_resource_link(href):
                continue
            existing_index = href_index.get(href)
            if existing_index is None:
                href_index[href] = len(deduped)
                deduped.append(row)
                continue
            existing = deduped[existing_index]
            existing_note = str(existing.get("note") or "").strip()
            current_note = str(row.get("note") or "").strip()
            existing_score = (
                len(existing_note)
                + (0 if self._is_generic_reference_summary(existing_note) else 40)
                + (35 if self._has_public_web_relevance_anchor(existing_note) else 0)
            )
            current_score = (
                len(current_note)
                + (0 if self._is_generic_reference_summary(current_note) else 40)
                + (35 if self._has_public_web_relevance_anchor(current_note) else 0)
            )
            if current_score > existing_score:
                deduped[existing_index] = row
        return deduped[:limit]

    @classmethod
    def _compose_teaching_manuscript_segment_text(
        cls,
        *,
        base_guidance: str,
        section_type: str,
        packet_copy: Optional[Mapping[str, Any]] = None,
        emphasis: str = "",
        limit: int = 320,
    ) -> str:
        def _tokenize_reader_copy(text: str) -> set[str]:
            return {
                token.lower()
                for token in re.findall(r"[A-Za-z0-9']+|[\u3400-\u9fff]", str(text or ""))
                if len(token.strip()) >= 1
            }

        def _is_too_similar(candidate: str, existing: str) -> bool:
            candidate_tokens = _tokenize_reader_copy(candidate)
            existing_tokens = _tokenize_reader_copy(existing)
            if not candidate_tokens or not existing_tokens:
                return False
            candidate_markers = {
                marker.lower()
                for marker in re.findall(r"\b(?:Fig(?:ure)?|Table|Equation)\s*\d+[A-Za-z]?\b", candidate, flags=re.IGNORECASE)
            }
            existing_markers = {
                marker.lower()
                for marker in re.findall(r"\b(?:Fig(?:ure)?|Table|Equation)\s*\d+[A-Za-z]?\b", existing, flags=re.IGNORECASE)
            }
            overlap = len(candidate_tokens & existing_tokens) / max(1, min(len(candidate_tokens), len(existing_tokens)))
            if candidate_markers and existing_markers and candidate_markers & existing_markers and overlap >= 0.42:
                return True
            return overlap >= 0.58

        candidates: List[str] = []
        for value in (
            base_guidance,
            emphasis,
            (packet_copy or {}).get("summary"),
            *((packet_copy or {}).get("reader_notes") or []),
            *(((packet_copy or {}).get("supporting_points") or [])[:1]),
        ):
            clean = cls._sanitize_reader_facing_text(value, limit=limit)
            clean = cls._clean_excerpt(clean, limit=limit) if clean else ""
            if (
                not clean
                or cls._looks_like_internal_planner_copy(clean)
                or cls._is_reader_surface_noise(clean)
                or cls._looks_like_generic_helper_summary(clean)
                or cls._is_generic_narrative_summary(clean)
                or cls._needs_display_localization(clean)
                or cls._looks_like_hype_marketing_copy(clean)
                or cls._looks_like_primary_evidence_dump(clean, section_type=section_type)
            ):
                continue
            if "建议结合原文逐句核对" in clean:
                clean = clean.replace("“这一段正文包含本页的重要结论，建议结合原文逐句核对。”", "这段正文里的关键结论")
                clean = clean.replace("这一段正文包含本页的重要结论，建议结合原文逐句核对。", "这段正文里的关键结论")
                clean = cls._clean_excerpt(clean, limit=limit)
                if not clean or clean == "这段正文里的关键结论":
                    continue
            if any(
                clean in existing
                or existing in clean
                or _is_too_similar(clean, existing)
                for existing in candidates
            ):
                continue
            candidates.append(clean)
        return cls._clean_excerpt(" ".join(candidates), limit=limit) if candidates else ""

    @staticmethod
    def _is_body_reading_target(target: Mapping[str, Any]) -> bool:
        target_kind = str(target.get("target_kind") or "").strip().lower()
        component_type = str(target.get("component_type") or "").strip()
        if target_kind in {"figure", "table", "equation"}:
            return False
        if component_type in {"FigurePanel", "TablePanel", "EquationPanel"}:
            return False
        return True

    def _resolve_teaching_manuscript_body_targets(
        self,
        *,
        primary_focus_target_id: str,
        body_flow_target_ids: Sequence[str],
        secondary_support_ids: Sequence[str],
        story_substrate: Mapping[str, Any],
        target_map: Mapping[str, Any],
    ) -> tuple[List[str], List[str]]:
        ordered_candidates = self._dedupe_strings(
            [
                *[str(item).strip() for item in list(body_flow_target_ids or []) if str(item).strip()],
                *[str(item).strip() for item in list(secondary_support_ids or []) if str(item).strip()],
                *[
                    str(item).strip()
                    for row in list(story_substrate.get("narrative_turns") or [])
                    if isinstance(row, Mapping)
                    for item in list(row.get("target_ids") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for row in list(story_substrate.get("terms_to_explain") or [])
                    if isinstance(row, Mapping)
                    for item in list(row.get("source_target_ids") or row.get("target_ids") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for row in list(story_substrate.get("evidence_units") or [])
                    if isinstance(row, Mapping)
                    for item in list(row.get("source_target_ids") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in list(target_map.keys())
                    if str(item).strip()
                ],
            ],
            limit=12,
        )
        body_candidates = [
            target_id
            for target_id in ordered_candidates
            if target_id != primary_focus_target_id
            and self._is_body_reading_target(dict(target_map.get(target_id) or {}))
        ]
        substantive_body_candidates: List[str] = []
        fragmentary_body_candidates: List[str] = []
        for target_id in body_candidates:
            target = dict(target_map.get(target_id) or {})
            excerpt = self._clean_excerpt(
                str(
                    target.get("excerpt")
                    or target.get("title")
                    or target.get("section_label")
                    or target.get("figure_label")
                    or ""
                ).strip(),
                limit=240,
            )
            if excerpt and not self._is_fragment_like_excerpt(excerpt):
                substantive_body_candidates.append(target_id)
            else:
                fragmentary_body_candidates.append(target_id)
        preferred_body_target_ids = self._select_preferred_current_page_target_ids(
            target_map=target_map,
            role="body",
            seed_target_ids=[*substantive_body_candidates, *body_candidates, *fragmentary_body_candidates],
            limit=3,
        )
        if substantive_body_candidates:
            inline_target_ids = self._dedupe_strings(
                [
                    target_id
                    for target_id in list(preferred_body_target_ids or substantive_body_candidates)
                    if target_id in substantive_body_candidates
                ]
                or substantive_body_candidates,
                limit=2,
            )
        else:
            inline_target_ids = self._dedupe_strings(
                preferred_body_target_ids or body_candidates or fragmentary_body_candidates,
                limit=3,
            )
        if not inline_target_ids:
            inline_target_ids = self._dedupe_strings(
                [
                    target_id
                    for target_id in ordered_candidates
                    if target_id and target_id != primary_focus_target_id
                ],
                limit=3,
            )
        full_target_ids = self._dedupe_strings(
            [
                *[str(item).strip() for item in list(body_flow_target_ids or []) if str(item).strip()],
                *inline_target_ids,
                *[str(item).strip() for item in list(secondary_support_ids or []) if str(item).strip()],
                *([primary_focus_target_id] if primary_focus_target_id else []),
            ],
            limit=6,
        )
        return inline_target_ids, full_target_ids

    @staticmethod
    def _teaching_manuscript_slot_kind(target: Mapping[str, Any]) -> str:
        target_kind = str(target.get("kind") or target.get("target_kind") or "").strip().lower()
        component_type = str(target.get("component_type") or "").strip()
        if target_kind in {"figure", "table", "equation"} or component_type in {"FigurePanel", "TablePanel", "EquationPanel"}:
            return "figure_slot"
        return "body_slot"

    @classmethod
    def _teaching_manuscript_slot_label(
        cls,
        *,
        slot_kind: str,
        segment_type: str,
        target_ids: Sequence[str],
        target_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
        fallback_title: str = "",
    ) -> str:
        for target_id in [str(item).strip() for item in list(target_ids or []) if str(item).strip()]:
            target = dict((target_map or {}).get(target_id) or {})
            label = cls._resolve_target_display_label(target)
            if label:
                return cls._clean_excerpt(label, limit=80)
            section_label = cls._clean_excerpt(str(target.get("section_label") or "").strip(), limit=80)
            if section_label:
                return section_label
        clean_fallback = cls._clean_excerpt(str(fallback_title or "").strip(), limit=80)
        if clean_fallback and (
            (
                slot_kind == "figure_slot"
                and re.search(r"\b(?:fig(?:ure)?|table|equation)\b|图", clean_fallback, flags=re.IGNORECASE)
            )
            or (
                slot_kind != "figure_slot"
                and not re.search(r"\b(?:fig(?:ure)?|table|equation)\b|图", clean_fallback, flags=re.IGNORECASE)
            )
        ):
            return clean_fallback
        if slot_kind == "figure_slot":
            return "配图原文"
        if segment_type == "opening":
            return "本页正文"
        return "正文摘录"

    @classmethod
    def _build_teaching_manuscript_display_flow(
        cls,
        *,
        segment_id: str,
        segment_type: str,
        teaching_text: str,
        title: str,
        target_ids: Sequence[str],
        full_evidence_target_ids: Sequence[str],
        target_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
        adjacent_bridge: str = "",
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        display_flow: List[Dict[str, Any]] = []
        slot_bindings: List[Dict[str, Any]] = []

        prose_text = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(teaching_text, limit=420),
            limit=420,
        )
        if prose_text:
            display_flow.append({"kind": "prose", "text": prose_text})

        primary_target_ids = cls._dedupe_strings(
            [str(item).strip() for item in list(target_ids or []) if str(item).strip()],
            limit=6,
        )
        all_target_ids = cls._dedupe_strings(
            [
                *primary_target_ids,
                *[str(item).strip() for item in list(full_evidence_target_ids or []) if str(item).strip()],
            ],
            limit=8,
        )
        grouped_slots: List[Dict[str, Any]] = []
        slot_index_by_kind: Dict[str, int] = {}
        slot_target_ids_by_kind: Dict[str, List[str]] = {}
        slot_full_target_ids_by_kind: Dict[str, List[str]] = {}

        def _fallback_slot_kind(target_id: str, target: Mapping[str, Any]) -> str:
            if dict(target or {}):
                return cls._teaching_manuscript_slot_kind(target)
            lowered = str(target_id or "").strip().lower()
            if segment_type in {"figure", "focus"}:
                return "figure_slot"
            if segment_type == "body":
                return "body_slot" if target_id in primary_target_ids else "figure_slot"
            if segment_type == "opening":
                if primary_target_ids:
                    return "figure_slot" if target_id == primary_target_ids[0] else "body_slot"
                if any(token in lowered for token in (":fig", "figure", "table", "equation", ":eq", ":tbl")):
                    return "figure_slot"
            if any(token in lowered for token in (":fig", "figure", "table", "equation", ":eq", ":tbl")):
                return "figure_slot"
            return "body_slot"

        figure_target_ids = [
            target_id
            for target_id in all_target_ids
            if _fallback_slot_kind(target_id, dict((target_map or {}).get(target_id) or {})) == "figure_slot"
        ]
        body_target_ids = [
            target_id
            for target_id in all_target_ids
            if _fallback_slot_kind(target_id, dict((target_map or {}).get(target_id) or {})) == "body_slot"
        ]
        if segment_type in {"figure", "focus"}:
            ordered_target_ids = figure_target_ids[:1] or primary_target_ids[:1] or all_target_ids[:1]
        elif segment_type == "opening":
            ordered_target_ids = figure_target_ids[:1] or body_target_ids[:1] or primary_target_ids[:1] or all_target_ids[:1]
        elif segment_type == "body":
            ordered_target_ids = body_target_ids[:1] or primary_target_ids[:1] or all_target_ids[:1]
        elif segment_type == "wrapup":
            ordered_target_ids = body_target_ids[:1]
        else:
            ordered_target_ids = primary_target_ids[:1] or all_target_ids[:1]

        for target_id in ordered_target_ids:
            target = dict((target_map or {}).get(target_id) or {})
            slot_kind = _fallback_slot_kind(target_id, target)
            slot_target_ids_by_kind.setdefault(slot_kind, [])
            slot_full_target_ids_by_kind.setdefault(slot_kind, [])
            if slot_kind not in slot_index_by_kind:
                slot_prefix = "fig" if slot_kind == "figure_slot" else "body"
                slot_id = f"{slot_prefix}:{segment_id}"
                slot_label = cls._teaching_manuscript_slot_label(
                    slot_kind=slot_kind,
                    segment_type=segment_type,
                    target_ids=[target_id],
                    target_map=target_map,
                    fallback_title=title,
                )
                slot_index_by_kind[slot_kind] = len(grouped_slots)
                grouped_slots.append(
                    {
                        "kind": slot_kind,
                        "slot_id": slot_id,
                        "label": slot_label,
                        "target_ids": [],
                    }
                )
            grouped_slots[slot_index_by_kind[slot_kind]]["target_ids"].append(target_id)
            slot_target_ids_by_kind[slot_kind].append(target_id)

        for target_id in [str(item).strip() for item in list(full_evidence_target_ids or []) if str(item).strip()]:
            target = dict((target_map or {}).get(target_id) or {})
            slot_kind = _fallback_slot_kind(target_id, target)
            slot_full_target_ids_by_kind.setdefault(slot_kind, [])
            slot_full_target_ids_by_kind[slot_kind].append(target_id)

        if grouped_slots:
            display_flow.extend(grouped_slots)
            for block in grouped_slots:
                slot_kind = str(block.get("kind") or "").strip()
                slot_bindings.append(
                    {
                        "slot_id": str(block.get("slot_id") or "").strip(),
                        "kind": slot_kind,
                        "label": str(block.get("label") or "").strip(),
                        "target_ids": cls._dedupe_strings(
                            [str(item).strip() for item in list(slot_target_ids_by_kind.get(slot_kind) or []) if str(item).strip()],
                            limit=6,
                        ),
                        "full_evidence_target_ids": cls._dedupe_strings(
                            [str(item).strip() for item in list(slot_full_target_ids_by_kind.get(slot_kind) or []) if str(item).strip()],
                            limit=8,
                        ),
                    }
                )
        elif prose_text and adjacent_bridge:
            bridge_text = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(adjacent_bridge, limit=180),
                limit=180,
            )
            if bridge_text:
                display_flow.append({"kind": "note", "text": bridge_text})

        return display_flow, slot_bindings

    def _build_teaching_manuscript(
        self,
        *,
        status: str,
        target_map: Mapping[str, Any],
        story_substrate: Mapping[str, Any],
        page_brief: Mapping[str, Any],
        teacher_spine: Mapping[str, Any],
        adjacent_bridge_cues: Sequence[Mapping[str, Any]],
        tool_enrichment_packet: Mapping[str, Any],
        resource_modules: Sequence[Mapping[str, Any]],
        interaction_modules: Sequence[Mapping[str, Any]],
        focus_label: str,
    ) -> Dict[str, Any]:
        storyboard = [dict(row) for row in list(page_brief.get("storyboard") or []) if isinstance(row, Mapping)]
        packet_rows = [dict(row) for row in list(tool_enrichment_packet.get("beat_packets") or []) if isinstance(row, Mapping)]
        packet_by_beat_id = {
            str(row.get("beat_id") or "").strip(): row
            for row in packet_rows
            if str(row.get("beat_id") or "").strip()
        }
        beat_id_by_section = {
            str(row.get("section_type") or "").strip(): str(row.get("beat_id") or "").strip()
            for row in storyboard
            if str(row.get("section_type") or "").strip()
        }

        def packet_for_section(section_type: str) -> Optional[Dict[str, Any]]:
            beat_id = beat_id_by_section.get(section_type) or ""
            if beat_id and beat_id in packet_by_beat_id:
                return dict(packet_by_beat_id[beat_id])
            for row in packet_rows:
                inferred = self._infer_section_type_from_tool_objectives(
                    [str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()]
                )
                if inferred == section_type:
                    return dict(row)
            return None

        primary_focus_target_id = str(page_brief.get("primary_focus_target_id") or "").strip()
        body_flow_target_ids = [
            str(item).strip()
            for item in list(page_brief.get("body_flow_target_ids") or [])
            if str(item).strip()
        ]
        secondary_support_ids = [
            str(item).strip()
            for item in list(page_brief.get("secondary_support_target_ids") or [])
            if str(item).strip()
        ]
        focus_target_ids = self._select_preferred_current_page_target_ids(
            target_map=target_map,
            role="figure",
            seed_target_ids=[primary_focus_target_id, *secondary_support_ids],
            limit=1,
        ) or self._dedupe_strings([primary_focus_target_id], limit=1)
        if focus_target_ids:
            primary_focus_target_id = focus_target_ids[0]
        body_target_ids, body_full_evidence_target_ids = self._resolve_teaching_manuscript_body_targets(
            primary_focus_target_id=primary_focus_target_id,
            body_flow_target_ids=body_flow_target_ids,
            secondary_support_ids=secondary_support_ids,
            story_substrate=story_substrate,
            target_map=target_map,
        )
        opening_target_ids = self._dedupe_strings(
            [primary_focus_target_id, *body_target_ids[:1]],
            limit=2,
        )

        readable_claim = next(
            (
                self._clean_excerpt(str(item.get("display_text") or item.get("text") or "").strip(), limit=160)
                for item in list(story_substrate.get("main_claims") or [])
                if isinstance(item, Mapping)
                and str(item.get("display_text") or item.get("text") or "").strip()
                and not self._is_english_heavy_text(str(item.get("display_text") or item.get("text") or "").strip())
            ),
            "",
        )

        focus_packet = packet_for_section("focus_stage")
        body_packet = packet_for_section("reading_flow")
        support_packet = packet_for_section("supporting_resources")
        focus_packet_copy = self._extract_beat_packet_reader_copy(focus_packet)
        body_packet_copy = self._extract_beat_packet_reader_copy(body_packet)
        support_packet_copy = self._extract_beat_packet_reader_copy(support_packet)
        adjacent_bridge = str((adjacent_bridge_cues[0] or {}).get("text") or "").strip() if adjacent_bridge_cues else ""
        focus_adjacent_bridge = self._compose_adjacent_bridge_note(adjacent_bridge) if adjacent_bridge else ""
        opening_emphasis = self._compose_teaching_manuscript_context_emphasis(
            segment_type="opening",
            story_substrate=story_substrate,
            target_ids=focus_target_ids or body_target_ids,
            focus_label=focus_label,
            primary_text=readable_claim,
        )
        focus_emphasis = self._compose_teaching_manuscript_context_emphasis(
            segment_type="figure",
            story_substrate=story_substrate,
            target_ids=focus_target_ids,
            focus_label=focus_label,
            primary_text=readable_claim,
        )
        opening_grounding = self._compose_segment_grounding_copy(
            segment_type="opening",
            target_ids=opening_target_ids,
            target_map=target_map,
            focus_label=focus_label,
            limit=320,
        )
        focus_grounding = self._compose_segment_grounding_copy(
            segment_type="figure",
            target_ids=focus_target_ids,
            target_map=target_map,
            focus_label=focus_label,
            limit=320,
        )
        body_grounding = self._compose_segment_grounding_copy(
            segment_type="body",
            target_ids=body_target_ids,
            target_map=target_map,
            focus_label=focus_label,
            limit=340,
        )

        segments: List[Dict[str, Any]] = []
        opening_target_label = next(
            (
                self._clean_excerpt(str(dict(target_map.get(target_id) or {}).get("title") or "").strip(), limit=60)
                for target_id in opening_target_ids
                if str(dict(target_map.get(target_id) or {}).get("title") or "").strip()
            ),
            "",
        )
        opening_fallback_text = self._compose_teaching_manuscript_segment_text(
            base_guidance=str(teacher_spine.get("opening") or "").strip(),
            section_type="hero",
            emphasis=opening_emphasis,
            limit=320,
        )
        opening_title = self._compose_segment_grounding_title(
            segment_type="opening",
            target_ids=opening_target_ids,
            target_map=target_map,
            focus_label=focus_label,
        ) or self._manuscript_title_for_segment_type("opening", focus_label)
        opening_teaching_text = self._prefer_grounded_manuscript_text(
            segment_type="opening",
            focus_label=focus_label,
            target_label=opening_target_label,
            grounding_copy=opening_grounding,
            fallback_text=opening_fallback_text,
        )
        opening_display_flow, opening_slot_bindings = self._build_teaching_manuscript_display_flow(
            segment_id="ms-opening",
            segment_type="opening",
            teaching_text=opening_teaching_text,
            title=opening_title,
            target_ids=opening_target_ids,
            full_evidence_target_ids=opening_target_ids,
            target_map=target_map,
        )
        segments.append(
            {
                "segment_id": "ms-opening",
                "segment_type": "opening",
                "title": opening_title,
                "teaching_text": opening_teaching_text,
                "anchor_excerpt": "",
                "target_ids": opening_target_ids,
                "full_evidence_target_ids": opening_target_ids,
                "display_flow": opening_display_flow,
                "slot_bindings": opening_slot_bindings,
                "glossary": [],
                "adjacent_bridge": "",
                "reference_links": [],
                "meta": {"role": "opening", "section_type": "hero"},
            }
        )
        if focus_target_ids:
            focus_reference_links = self._collect_teaching_manuscript_reference_links(
                resource_modules=resource_modules,
                packet=focus_packet or support_packet,
                target_ids=focus_target_ids,
                limit=1,
            )
            focus_target_label = next(
                (
                    self._clean_excerpt(str(dict(target_map.get(target_id) or {}).get("title") or "").strip(), limit=60)
                    for target_id in focus_target_ids
                    if str(dict(target_map.get(target_id) or {}).get("title") or "").strip()
                ),
                "",
            )
            focus_fallback_text = self._compose_teaching_manuscript_segment_text(
                base_guidance=str(teacher_spine.get("focus_guidance") or "").strip(),
                section_type="focus_stage",
                packet_copy=focus_packet_copy,
                emphasis=focus_emphasis or readable_claim,
                limit=340,
            )
            focus_title = self._compose_segment_grounding_title(
                segment_type="figure",
                target_ids=focus_target_ids,
                target_map=target_map,
                focus_label=focus_label,
            ) or self._manuscript_title_for_segment_type("figure", focus_label)
            focus_teaching_text = self._prefer_grounded_manuscript_text(
                segment_type="figure" if primary_focus_target_id else "focus",
                focus_label=focus_label,
                target_label=focus_target_label,
                grounding_copy=focus_grounding,
                has_reference_links=bool(focus_reference_links),
                fallback_text=focus_fallback_text,
            )
            focus_display_flow, focus_slot_bindings = self._build_teaching_manuscript_display_flow(
                segment_id="ms-focus",
                segment_type="figure" if primary_focus_target_id else "focus",
                teaching_text=focus_teaching_text,
                title=focus_title,
                target_ids=focus_target_ids,
                full_evidence_target_ids=focus_target_ids,
                target_map=target_map,
                adjacent_bridge=focus_adjacent_bridge,
            )
            segments.append(
                {
                    "segment_id": "ms-focus",
                    "segment_type": "figure" if primary_focus_target_id else "focus",
                    "title": focus_title,
                    "teaching_text": focus_teaching_text,
                    "anchor_excerpt": self._resolve_teaching_manuscript_anchor_excerpt(
                        target_ids=focus_target_ids,
                        target_map=target_map,
                    ),
                    "target_ids": focus_target_ids,
                    "full_evidence_target_ids": focus_target_ids,
                    "display_flow": focus_display_flow,
                    "slot_bindings": focus_slot_bindings,
                    "glossary": [],
                    "adjacent_bridge": focus_adjacent_bridge,
                    "reference_links": focus_reference_links,
                    "meta": {"role": "focus", "section_type": "focus_stage"},
                }
            )
        if body_target_ids:
            body_reference_links = self._collect_teaching_manuscript_reference_links(
                resource_modules=resource_modules,
                packet=support_packet or body_packet,
                target_ids=body_full_evidence_target_ids,
                limit=2,
            )
            body_emphasis = self._compose_teaching_manuscript_body_emphasis(
                target_ids=body_target_ids,
                target_map=target_map,
                focus_label=focus_label,
            )
            body_context_emphasis = self._compose_teaching_manuscript_context_emphasis(
                segment_type="body",
                story_substrate=story_substrate,
                target_ids=body_full_evidence_target_ids,
                focus_label=focus_label,
                primary_text=readable_claim,
            )
            body_guidance = str(teacher_spine.get("body_guidance") or "").strip()
            body_target_label = next(
                (
                    self._clean_excerpt(
                        str(
                            dict(target_map.get(target_id) or {}).get("section_label")
                            or dict(target_map.get(target_id) or {}).get("title")
                            or ""
                        ).strip(),
                        limit=60,
                    )
                    for target_id in body_target_ids
                    if str(
                        dict(target_map.get(target_id) or {}).get("section_label")
                        or dict(target_map.get(target_id) or {}).get("title")
                        or ""
                    ).strip()
                ),
                "",
            )
            body_fallback_text = self._compose_teaching_manuscript_segment_text(
                base_guidance=body_context_emphasis or body_emphasis or body_guidance,
                section_type="reading_flow",
                packet_copy=body_packet_copy,
                emphasis=body_emphasis if body_context_emphasis else ("" if body_emphasis else body_guidance),
                limit=360,
            )
            body_title = self._compose_segment_grounding_title(
                segment_type="body",
                target_ids=body_target_ids,
                target_map=target_map,
                focus_label=focus_label,
            ) or self._manuscript_title_for_segment_type("body", focus_label)
            body_teaching_text = self._prefer_grounded_manuscript_text(
                segment_type="body",
                focus_label=focus_label,
                target_label=body_target_label,
                grounding_copy=body_grounding,
                has_reference_links=bool(body_reference_links),
                fallback_text=body_fallback_text,
            )
            body_adjacent_bridge = self._compose_adjacent_bridge_note(adjacent_bridge) if adjacent_bridge else ""
            body_display_flow, body_slot_bindings = self._build_teaching_manuscript_display_flow(
                segment_id="ms-body",
                segment_type="body",
                teaching_text=body_teaching_text,
                title=body_title,
                target_ids=body_target_ids,
                full_evidence_target_ids=body_full_evidence_target_ids,
                target_map=target_map,
                adjacent_bridge=body_adjacent_bridge,
            )
            segments.append(
                {
                    "segment_id": "ms-body",
                    "segment_type": "body",
                    "title": body_title,
                    "teaching_text": body_teaching_text,
                    "anchor_excerpt": self._resolve_teaching_manuscript_anchor_excerpt(
                        target_ids=body_target_ids,
                        target_map=target_map,
                        prefer_body_excerpt=True,
                    ),
                    "target_ids": body_target_ids,
                    "full_evidence_target_ids": body_full_evidence_target_ids,
                    "display_flow": body_display_flow,
                    "slot_bindings": body_slot_bindings,
                    "glossary": self._collect_teaching_manuscript_glossary(
                        story_substrate=story_substrate,
                        interaction_modules=interaction_modules,
                        target_ids=body_full_evidence_target_ids,
                        focus_label=focus_label,
                        limit=3,
                    ),
                    "adjacent_bridge": body_adjacent_bridge,
                    "reference_links": body_reference_links,
                    "meta": {"role": "body", "section_type": "reading_flow"},
                }
            )
        used_reference_hrefs = {
            str(item.get("href") or "").strip()
            for segment in segments
            for item in list(segment.get("reference_links") or [])
            if str(item.get("href") or "").strip()
        }
        wrapup_links = self._collect_teaching_manuscript_reference_links(
            resource_modules=resource_modules,
            packet=support_packet,
            target_ids=secondary_support_ids or body_target_ids,
            limit=2,
        )
        wrapup_links = [
            row for row in wrapup_links
            if str(row.get("href") or "").strip() not in used_reference_hrefs
        ]
        wrapup_text = self._compose_teaching_manuscript_segment_text(
            base_guidance=str(teacher_spine.get("support_guidance") or "").strip(),
            section_type="supporting_resources",
            packet_copy=support_packet_copy,
            emphasis=self._compose_teaching_manuscript_context_emphasis(
                segment_type="wrapup",
                story_substrate=story_substrate,
                target_ids=secondary_support_ids or body_target_ids,
                focus_label=focus_label,
            ),
            limit=260,
        )
        if wrapup_text and wrapup_links:
            wrapup_title = self._manuscript_title_for_segment_type("wrapup", focus_label)
            wrapup_display_flow, wrapup_slot_bindings = self._build_teaching_manuscript_display_flow(
                segment_id="ms-wrapup",
                segment_type="wrapup",
                teaching_text=wrapup_text,
                title=wrapup_title,
                target_ids=secondary_support_ids or body_target_ids[:1],
                full_evidence_target_ids=secondary_support_ids or body_target_ids[:1],
                target_map=target_map,
            )
            segments.append(
                {
                    "segment_id": "ms-wrapup",
                    "segment_type": "wrapup",
                    "title": wrapup_title,
                    "teaching_text": wrapup_text,
                    "anchor_excerpt": "",
                    "target_ids": secondary_support_ids or body_target_ids[:1],
                    "full_evidence_target_ids": secondary_support_ids or body_target_ids[:1],
                    "display_flow": wrapup_display_flow,
                    "slot_bindings": wrapup_slot_bindings,
                    "glossary": [],
                    "adjacent_bridge": "",
                    "reference_links": wrapup_links,
                    "meta": {"role": "wrapup", "section_type": "supporting_resources"},
                }
            )
        return {
            "version": "v2",
            "status": str(status or "done").strip() or "done",
            "segments": segments,
        }

    @classmethod
    def _leading_clause_for_manuscript_text(cls, text: str) -> str:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=120)
        if not clean:
            return ""
        lead = re.split(r"[，。；;：:！？!?]", clean, maxsplit=1)[0]
        return cls._clean_excerpt(lead, limit=32)

    @classmethod
    def _contains_long_raw_english_span(cls, text: str) -> bool:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=260)
        if not clean:
            return False
        normalized = re.sub(
            r"\b(?:Fig(?:ure)?|USMLE|DOI|AI|ChatGPT|Step\s*[123](?:CK)?|MC-[A-Z]+|ACI|S\d+\s+Data)\b",
            " ",
            clean,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\b\d+(?:\.\d+)?%?\b", " ", normalized)
        normalized = re.sub(r"\b[A-Z]{1,4}\b", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if re.search(r"[A-Za-z]{4,}(?:[-\s]+[A-Za-z]{4,}){2,}", normalized):
            return True
        return bool(
            re.search(r"[A-Za-z][A-Za-z\s,'()/-]{24,}", normalized)
            and len(re.findall(r"[A-Za-z]{4,}", normalized)) >= 3
        )

    @classmethod
    def _has_broken_reader_transition_copy(cls, text: str) -> bool:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=260)
        if not clean:
            return False
        return bool(
            re.search(r"关于\s*(?:会继续沿着|继续沿着|往下展开|承上启下|这条线索)", clean)
            or re.search(r"(?:前文|后文).{0,10}(?:会继续沿着|继续沿着)", clean)
            or "把前文先铺开了这条线索" in clean
            or "承上启下" in clean
        )

    @classmethod
    def _looks_quote_heavy_manuscript_copy(cls, text: str) -> bool:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=320)
        if not clean:
            return False
        quoted_spans = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{18,140})[\"'“”‘’]", clean)
        return any(
            cls._contains_long_raw_english_span(span)
            or len(span) >= 48
            for span in quoted_spans
        )

    @classmethod
    def _looks_like_generic_manuscript_placeholder(cls, *, segment_type: str, text: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=360), limit=360)
        if not clean:
            return False
        pattern_map = {
            "opening": [
                r"^这页真正重要的不是读完几个段落",
                r"^这页的主线围绕 .+ 展开，正文后面都在解释这里出现的关键差异",
                r"^这页真正要解释的核心问题是[:：]?",
                r"^这一页围绕 .+ 展开，先留意 .+ 怎样串起主线",
            ],
            "figure": [
                r"^.+把当前页的关键结果展开成更具体的解释",
                r"^.+集中了这一页最关键的比较，正文后面都在解释这些差异为什么重要",
                r"^先看 .+ 里和 .+ 有关的比较，再回正文核对作者怎样解释这些结果",
            ],
            "focus": [
                r"^.+把当前页的关键结果展开成更具体的解释",
                r"^.+集中了这一页最关键的比较，正文后面都在解释这些差异为什么重要",
                r"^先看 .+ 里和 .+ 有关的比较，再回正文核对作者怎样解释这些结果",
            ],
            "body": [
                r"^正文(?:这一段|这部分)把图里的结果展开成作者真正要表达的判断",
                r"^.+把 .+ 里的比较结果展开成作者真正要表达的判断",
                r"^.+把当前页的关键结果展开成更具体的解释",
                r"^.+先看.+出现得有多频繁.+先顺着正文里的关键结果段落往下读",
            ],
        }
        return any(re.search(pattern, clean) for pattern in pattern_map.get(segment_type, []))

    @classmethod
    def _looks_like_template_teacher_copy(cls, *, segment_type: str, text: str) -> bool:
        clean = cls._clean_excerpt(str(text or "").strip(), limit=320)
        if not clean:
            return False
        pattern_map = {
            "opening": [r"^这页(?:可以先把|先把|先别急着)"],
            "figure": [r"^(?:先看|先盯住).+?(?:比较了什么|变化幅度|变化落在哪)"],
            "focus": [r"^(?:先看|先盯住).+?(?:比较了什么|变化幅度|变化落在哪)"],
            "body": [r"^(?:正文(?:这部分|这一段)负责把|接着读|先顺着|顺着正文)"],
        }
        return any(
            re.search(pattern, clean)
            for pattern in pattern_map.get(segment_type, [])
        )

    @classmethod
    def _manuscript_surface_needs_repair(cls, *, segment_type: str, text: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=360), limit=360)
        if not clean:
            return True
        if cls._has_broken_reader_transition_copy(clean) or cls._contains_long_raw_english_span(clean):
            return True
        if segment_type in {"opening", "figure", "focus", "body"} and (
            cls._looks_quote_heavy_manuscript_copy(clean)
            or cls._looks_like_template_teacher_copy(segment_type=segment_type, text=clean)
            or cls._looks_like_generic_manuscript_placeholder(segment_type=segment_type, text=clean)
        ):
            return True
        return False

    @classmethod
    def _adjacent_bridge_surface_needs_repair(cls, text: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=220), limit=220)
        if not clean:
            return False
        if (
            cls._looks_like_internal_planner_copy(clean)
            or cls._looks_like_primary_evidence_dump(clean, section_type="reading_flow")
            or cls._is_reader_surface_noise(clean)
            or cls._contains_long_raw_english_span(clean)
            or cls._has_broken_reader_transition_copy(clean)
        ):
            return True
        return False

    @classmethod
    def _manuscript_text_needs_polish(
        cls,
        *,
        segment_type: str,
        text: str,
        previous_texts: Sequence[str],
    ) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=340), limit=340)
        if not clean:
            return True
        if (
            cls._looks_like_internal_planner_copy(clean)
            or cls._is_reader_surface_noise(clean)
            or cls._looks_like_generic_helper_summary(clean)
            or cls._is_generic_narrative_summary(clean)
            or cls._needs_display_localization(clean)
            or cls._looks_like_primary_evidence_dump(clean, section_type="reading_flow" if segment_type == "body" else "focus_stage")
            or cls._manuscript_surface_needs_repair(segment_type=segment_type, text=clean)
        ):
            return True
        lead = cls._leading_clause_for_manuscript_text(clean)
        if not lead:
            return True
        previous_leads = {
            cls._leading_clause_for_manuscript_text(item)
            for item in list(previous_texts or [])
            if cls._leading_clause_for_manuscript_text(item)
        }
        if lead in previous_leads:
            return True
        repeated_clauses = [
            "再顺着正文看作者怎样",
            "再回到正文",
            "重点看作者怎样",
            "先抓住",
            "先顺着正文里的关键结果段落往下读",
        ]
        return any(
            clause in clean and any(clause in str(previous or "") for previous in list(previous_texts or []))
            for clause in repeated_clauses
        ) or (
            segment_type == "body"
            and clean.startswith("先顺着正文里的关键结果段落往下读")
        ) or cls._looks_like_generic_manuscript_placeholder(segment_type=segment_type, text=clean)

    @classmethod
    def _manuscript_lacks_grounded_substance(cls, *, segment_type: str, title: str, text: str) -> bool:
        clean_text = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=360), limit=360)
        clean_title = cls._clean_excerpt(cls._sanitize_reader_facing_text(title, limit=80), limit=80)
        if not clean_text:
            return True
        if cls._looks_like_generic_manuscript_placeholder(segment_type=segment_type, text=clean_text):
            return True
        if cls._manuscript_title_needs_repair(segment_type=segment_type, title=clean_title):
            return True
        grounding_tokens = cls._extract_grounding_terms(clean_text, limit=4)
        grounding_numbers = cls._extract_grounding_numbers(clean_text, limit=2)
        figure_markers = re.findall(r"\b(?:Fig(?:ure)?|Table|Equation)\s*\d+[A-Za-z]?\b", clean_text, flags=re.IGNORECASE)
        return len(grounding_tokens) + len(grounding_numbers) + len(figure_markers) < 2

    @classmethod
    def _compose_polished_teaching_segment_text(
        cls,
        *,
        segment_type: str,
        focus_label: str,
        target_label: str,
        grounding_copy: str,
        adjacent_subject: str,
        has_reference_links: bool,
    ) -> str:
        focus_token = cls._clean_excerpt(str(focus_label or "").strip(), limit=60)
        target_token = cls._clean_excerpt(str(target_label or "").strip(), limit=60)
        target_summary = cls._trim_terminal_punctuation(grounding_copy)
        if segment_type == "opening":
            if target_summary:
                if focus_token:
                    return cls._clean_excerpt(
                        f"先把阅读顺序定下来：先看 {focus_token}，抓住这一页最关键的比较框架。{target_summary}",
                        limit=340,
                    )
                return cls._clean_excerpt(f"先把阅读顺序定下来：先抓住这一页最关键的比较框架。{target_summary}", limit=340)
            if focus_token:
                return f"{focus_token} 呈现了这一页最关键的差异，正文会解释这些比较为什么重要。"
            return "这一页呈现了关键差异，正文会解释这些比较为什么重要。"
        if segment_type in {"figure", "focus"}:
            if target_summary:
                return cls._clean_excerpt(f"这一步先只看图里的比较：{target_summary}", limit=340)
            if focus_token:
                return f"{focus_token} 先把这一页最重要的差异摆出来，后面的正文会继续解释这些差异为什么重要。"
            return "这张图先把当前页最重要的差异摆出来，后面的正文会继续解释这些差异意味着什么。"
        if segment_type == "body":
            if target_summary:
                if target_token:
                    return cls._clean_excerpt(f"接下来读 {target_token}，重点看作者怎样解释前面的比较：{target_summary}", limit=340)
                if focus_token:
                    return cls._clean_excerpt(f"接下来读正文，重点看作者怎样解释 {focus_token} 里的比较：{target_summary}", limit=340)
                return cls._clean_excerpt(f"接下来读正文，重点看作者怎样解释前面的比较：{target_summary}", limit=340)
            if focus_token and target_token:
                return f"{target_token} 这一段会解释 {focus_token} 里的差异为什么出现，以及作者想据此说明什么。"
            if focus_token:
                return f"正文这一段会解释 {focus_token} 里的差异为什么出现，以及作者想据此说明什么。"
            return "正文这一段会解释前面的差异为什么出现，以及作者想据此说明什么。"
        if segment_type == "wrapup":
            if has_reference_links:
                return "如果这里还有一层背景没接上，再用参考资料补齐比较框架；它的作用是帮助理解正文，不是替代正文。"
            return "如果这里还有一层背景没接上，再补这层上下文；它的作用是帮助理解正文，不是替代正文。"
        return ""

    @classmethod
    def _prefer_grounded_manuscript_text(
        cls,
        *,
        segment_type: str,
        focus_label: str,
        target_label: str,
        grounding_copy: str,
        adjacent_subject: str = "",
        has_reference_links: bool = False,
        fallback_text: str,
    ) -> str:
        grounded = cls._compose_polished_teaching_segment_text(
            segment_type=segment_type,
            focus_label=focus_label,
            target_label=target_label,
            grounding_copy=grounding_copy,
            adjacent_subject=adjacent_subject,
            has_reference_links=has_reference_links,
        )
        if grounded and not cls._manuscript_lacks_grounded_substance(
            segment_type=segment_type,
            title=target_label or focus_label or segment_type,
            text=grounded,
        ):
            return grounded
        return cls._clean_excerpt(
            cls._sanitize_reader_facing_text(fallback_text, limit=360),
            limit=360,
        )

    @classmethod
    def _polish_teaching_manuscript_with_dossier(
        cls,
        *,
        manuscript: Mapping[str, Any],
        page_dossier: Mapping[str, Any],
        adjacent_page_context: Sequence[Mapping[str, Any]],
        focus_label: str,
        story_substrate: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        current = dict(manuscript or {})
        segments = [dict(row) for row in list(current.get("segments") or []) if isinstance(row, Mapping)]
        if not segments:
            return current
        compact_dossier = cls._compact_page_dossier_for_generation(page_dossier)
        current_page = dict(compact_dossier.get("current_page") or {})
        dossier_targets = {
            str(item.get("target_id") or "").strip(): dict(item)
            for item in list(current_page.get("targets") or [])
            if isinstance(item, Mapping) and str(item.get("target_id") or "").strip()
        }
        adjacent_subject = next(
            (
                subject
                for subject in (
                    cls._extract_adjacent_bridge_subject(
                        cls._compose_adjacent_bridge_from_context_row(row)
                    )
                    for row in list(adjacent_page_context or [])
                    if isinstance(row, Mapping)
                )
                if subject
            ),
            "",
        )
        adjacent_bridge_rows = cls._derive_adjacent_bridge_cues(
            [dict(item) for item in list(adjacent_page_context or []) if isinstance(item, Mapping)],
            limit=1,
        )
        fallback_adjacent_bridge = next(
            (
                cls._compose_adjacent_bridge_note(
                    cls._strip_adjacent_bridge_provenance(str(row.get("text") or "").strip())
                )
                for row in adjacent_bridge_rows
                if cls._compose_adjacent_bridge_note(
                    cls._strip_adjacent_bridge_provenance(str(row.get("text") or "").strip())
                )
            ),
            "",
        )
        if not cls._adjacent_bridge_has_specific_continuity(fallback_adjacent_bridge):
            fallback_adjacent_bridge = ""
        polished_segments: List[Dict[str, Any]] = []
        previous_texts: List[str] = []
        for row in segments:
            segment = dict(row)
            segment_type = str(segment.get("segment_type") or "").strip() or "body"
            target_ids = [
                str(item).strip()
                for item in list(segment.get("target_ids") or segment.get("full_evidence_target_ids") or [])
                if str(item).strip()
            ]
            target_label = next(
                (
                    cls._clean_excerpt(str(target.get("title") or "").strip(), limit=60)
                    for target_id in target_ids
                    for target in [dict(dossier_targets.get(target_id) or {})]
                    if str(target.get("title") or "").strip()
                    and not cls._needs_display_localization(str(target.get("title") or "").strip(), short_form=True)
                ),
                "",
            )
            primary_target = dict(dossier_targets.get(target_ids[0]) or {}) if target_ids else {}
            grounding_copy = cls._compose_segment_grounding_copy(
                segment_type=segment_type,
                target_ids=target_ids,
                target_map=dossier_targets,
                focus_label=focus_label,
            )
            polished_text = (
                cls._compose_polished_teaching_segment_text(
                    segment_type=segment_type,
                    focus_label=focus_label,
                    target_label=target_label,
                    grounding_copy=grounding_copy,
                    adjacent_subject=adjacent_subject,
                    has_reference_links=bool(list(segment.get("reference_links") or [])),
                )
                if grounding_copy
                else ""
            )
            current_text = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(segment.get("teaching_text"), limit=360),
                limit=360,
            )
            if polished_text and cls._manuscript_text_needs_polish(
                segment_type=segment_type,
                text=current_text,
                previous_texts=previous_texts,
            ):
                segment["teaching_text"] = polished_text
                current_text = polished_text

            current_title = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(segment.get("title"), limit=80),
                limit=80,
            )
            grounded_title = cls._compose_segment_grounding_title(
                segment_type=segment_type,
                target_ids=target_ids,
                target_map=dossier_targets,
                focus_label=focus_label,
            )
            if grounded_title and cls._manuscript_title_needs_repair(segment_type=segment_type, title=current_title):
                segment["title"] = grounded_title

            anchor_excerpt = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(segment.get("anchor_excerpt"), limit=180),
                limit=180,
            )
            if cls._looks_like_synthetic_anchor_excerpt(anchor_excerpt):
                anchor_excerpt = ""
            if not anchor_excerpt:
                fallback_target_ids = target_ids or [
                    str(item).strip()
                    for item in list(segment.get("full_evidence_target_ids") or [])
                    if str(item).strip()
                ]
                if fallback_target_ids:
                    anchor_excerpt = cls._resolve_teaching_manuscript_anchor_excerpt(
                        target_ids=fallback_target_ids,
                        target_map=dossier_targets,
                        prefer_body_excerpt=segment_type == "body",
                    )
            segment["anchor_excerpt"] = anchor_excerpt

            adjacent_bridge = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(segment.get("adjacent_bridge"), limit=220),
                limit=220,
            )
            if segment_type in {"figure", "focus", "body"}:
                if not cls._adjacent_bridge_has_specific_continuity(adjacent_bridge):
                    adjacent_bridge = ""
                elif cls._adjacent_bridge_surface_needs_repair(adjacent_bridge) or not adjacent_bridge.startswith("读到这里时，"):
                    adjacent_bridge = fallback_adjacent_bridge or cls._compose_adjacent_bridge_note(adjacent_bridge)
                segment["adjacent_bridge"] = adjacent_bridge if cls._adjacent_bridge_has_specific_continuity(adjacent_bridge) else fallback_adjacent_bridge

            display_flow, slot_bindings = cls._build_teaching_manuscript_display_flow(
                segment_id=str(segment.get("segment_id") or f"ms-{segment_type}").strip() or f"ms-{segment_type}",
                segment_type=segment_type,
                teaching_text=str(segment.get("teaching_text") or "").strip(),
                title=str(segment.get("title") or "").strip(),
                target_ids=target_ids,
                full_evidence_target_ids=[
                    str(item).strip()
                    for item in list(segment.get("full_evidence_target_ids") or target_ids)
                    if str(item).strip()
                ],
                target_map=dossier_targets,
                adjacent_bridge=str(segment.get("adjacent_bridge") or "").strip(),
            )
            segment["display_flow"] = display_flow
            segment["slot_bindings"] = slot_bindings

            polished_segments.append(segment)
            previous_texts.append(str(segment.get("teaching_text") or "").strip())
        current["segments"] = polished_segments
        return current

    def _normalize_teaching_manuscript(
        self,
        *,
        manuscript: Mapping[str, Any],
        teacher_spine: Mapping[str, Any],
        focus_label: str,
        anchor_terms: Sequence[str],
        adjacent_bridge_cues: Sequence[Mapping[str, Any]],
        status: str,
    ) -> Optional[Dict[str, Any]]:
        current = dict(manuscript or {})
        normalized_segments: List[Dict[str, Any]] = []
        first_adjacent_text = str((adjacent_bridge_cues[0] or {}).get("text") or "").strip() if adjacent_bridge_cues else ""
        for row in list(current.get("segments") or []):
            if not isinstance(row, Mapping):
                continue
            segment = dict(row)
            segment_type = str(segment.get("segment_type") or "").strip() or "body"
            section_type = (
                "hero" if segment_type == "opening"
                else "focus_stage" if segment_type in {"figure", "focus"}
                else "supporting_resources" if segment_type == "wrapup"
                else "reading_flow"
            )
            fallback_text = self._teacher_guidance_for_section_type(
                section_type=section_type,
                teacher_spine=teacher_spine,
            )
            raw_title = self._clean_excerpt(
                self._sanitize_reader_facing_text(segment.get("title"), limit=80),
                limit=80,
            )
            if (
                not raw_title
                or self._looks_like_internal_planner_copy(raw_title)
                or self._is_reader_surface_noise(raw_title)
            ):
                raw_title = self._manuscript_title_for_segment_type(segment_type, focus_label)
            segment["title"] = raw_title
            raw_text = self._clean_excerpt(
                self._sanitize_reader_facing_text(segment.get("teaching_text"), limit=340),
                limit=340,
            )
            if (
                raw_text
                and not self._manuscript_surface_needs_repair(segment_type=segment_type, text=raw_text)
                and not self._manuscript_lacks_grounded_substance(
                    segment_type=segment_type,
                    title=raw_title,
                    text=raw_text,
                )
            ):
                repaired_text = raw_text
            else:
                repaired_text, _ = self._repair_reader_visible_summary(
                    raw_value=segment.get("teaching_text"),
                    fallback=fallback_text,
                    section_type=section_type,
                    anchor_terms=anchor_terms,
                    require_anchor_alignment=section_type == "supporting_resources",
                    limit=340,
                )
            segment["teaching_text"] = repaired_text or fallback_text
            segment["target_ids"] = self._dedupe_strings(
                [str(item).strip() for item in list(segment.get("target_ids") or []) if str(item).strip()],
                limit=6,
            )
            segment["full_evidence_target_ids"] = self._dedupe_strings(
                [
                    str(item).strip()
                    for item in list(segment.get("full_evidence_target_ids") or segment.get("target_ids") or [])
                    if str(item).strip()
                ],
                limit=8,
            )
            anchor_excerpt = self._clean_excerpt(
                self._sanitize_reader_facing_text(segment.get("anchor_excerpt"), limit=180),
                limit=180,
            )
            anchor_has_claim_signal = bool(
                self._extract_grounding_numbers(anchor_excerpt, limit=2)
                or self._has_reader_facing_predicate(anchor_excerpt)
            )
            if (
                anchor_excerpt
                and section_type == "reading_flow"
                and (
                    (
                        self._looks_like_primary_evidence_dump(anchor_excerpt, section_type="reading_flow")
                        and len(anchor_excerpt) > 140
                        and not anchor_has_claim_signal
                    )
                    or self._looks_like_synthetic_anchor_excerpt(anchor_excerpt)
                )
            ):
                anchor_excerpt = ""
            if self._looks_like_synthetic_anchor_excerpt(anchor_excerpt):
                anchor_excerpt = ""
            segment["anchor_excerpt"] = anchor_excerpt
            adjacent_bridge = self._clean_excerpt(
                self._sanitize_reader_facing_text(segment.get("adjacent_bridge"), limit=180),
                limit=180,
            )
            if (
                not adjacent_bridge
                or self._looks_like_internal_planner_copy(adjacent_bridge)
                or self._looks_like_primary_evidence_dump(adjacent_bridge, section_type="reading_flow")
                or self._adjacent_bridge_surface_needs_repair(adjacent_bridge)
            ):
                adjacent_bridge = ""
            if segment_type == "body" and first_adjacent_text and self._adjacent_bridge_has_specific_continuity(first_adjacent_text):
                if not adjacent_bridge:
                    adjacent_bridge = self._compose_adjacent_bridge_note(first_adjacent_text)
                elif not adjacent_bridge.startswith("读到这里时，"):
                    adjacent_bridge = self._compose_adjacent_bridge_note(adjacent_bridge)
            elif segment_type in {"figure", "focus", "body"} and adjacent_bridge and not self._adjacent_bridge_has_specific_continuity(adjacent_bridge):
                adjacent_bridge = ""
            segment["adjacent_bridge"] = adjacent_bridge
            display_flow, slot_bindings = self._build_teaching_manuscript_display_flow(
                segment_id=str(segment.get("segment_id") or f"ms-{segment_type}").strip() or f"ms-{segment_type}",
                segment_type=segment_type,
                teaching_text=segment["teaching_text"],
                title=segment["title"],
                target_ids=segment["target_ids"],
                full_evidence_target_ids=segment["full_evidence_target_ids"],
                adjacent_bridge=segment["adjacent_bridge"],
            )
            segment["display_flow"] = display_flow
            segment["slot_bindings"] = slot_bindings
            glossary_rows: List[Dict[str, Any]] = []
            for item in list(segment.get("glossary") or []):
                if not isinstance(item, Mapping):
                    continue
                term = self._clean_excerpt(str(item.get("term") or "").strip(), limit=80)
                note = self._clean_excerpt(
                    self._sanitize_reader_facing_text(item.get("note"), limit=180),
                    limit=180,
                )
                if (
                    not term
                    or not note
                    or self._looks_like_internal_planner_copy(note)
                    or self._is_reader_surface_noise(note)
                ):
                    continue
                glossary_rows.append(
                    {
                        "term": term,
                        "note": note,
                        "target_ids": self._dedupe_strings(
                            [str(value).strip() for value in list(item.get("target_ids") or []) if str(value).strip()],
                            limit=4,
                        ),
                    }
                )
            segment["glossary"] = glossary_rows[:3]
            original_reference_links = [dict(item) for item in list(segment.get("reference_links") or []) if isinstance(item, Mapping)]
            reference_seed_rows = []
            for item in original_reference_links:
                row = dict(item)
                if not row.get("snippet") and row.get("note"):
                    row["snippet"] = row.get("note")
                reference_seed_rows.append(row)
            reference_links = self._normalize_public_links(
                reference_seed_rows,
                limit=2,
            )
            segment["reference_links"] = [
                {
                    "label": str(item.get("label") or item.get("href") or "").strip(),
                    "href": str(item.get("href") or "").strip(),
                    "note": (
                        self._clean_excerpt(
                            self._sanitize_reader_facing_text(item.get("justification") or item.get("snippet") or item.get("note"), limit=140),
                            limit=140,
                        )
                        or self._rewrite_reader_facing_reference(
                            label=str(item.get("label") or item.get("href") or "").strip(),
                            snippet=str(item.get("snippet") or item.get("note") or "").strip(),
                            objective=(
                                "why_it_matters"
                                if segment_type == "wrapup"
                                else "continuation_bridge"
                                if segment_type == "body"
                                else "external_comparison"
                            ),
                        )
                    ),
                }
                for item in reference_links
                if str(item.get("href") or "").strip()
            ]
            if (
                segment["teaching_text"]
                or segment["anchor_excerpt"]
                or segment["glossary"]
                or segment["reference_links"]
            ):
                normalized_segments.append(segment)
        if not normalized_segments:
            return None
        return {
            "version": "v2",
            "status": str(status or current.get("status") or "done").strip() or "done",
            "segments": normalized_segments,
        }

    @classmethod
    def _collect_authoritative_resource_candidates(
        cls,
        *,
        resource_modules: Sequence[Mapping[str, Any]],
        tool_enrichment_packet: Mapping[str, Any],
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        seed_rows: List[Dict[str, Any]] = []
        seed_meta_by_href: Dict[str, Dict[str, Any]] = {}

        def _append_seed(
            row: Mapping[str, Any],
            *,
            source: str,
            target_ids: Sequence[str],
            module_id: str = "",
            beat_id: str = "",
        ) -> None:
            href = str(row.get("href") or "").strip()
            if not href:
                return
            seed_rows.append(
                {
                    "href": href,
                    "label": str(row.get("label") or "").strip(),
                    "snippet": str(row.get("snippet") or row.get("note") or row.get("summary") or "").strip(),
                }
            )
            current_meta = seed_meta_by_href.setdefault(
                href,
                {
                    "source": source,
                    "module_id": module_id,
                    "beat_id": beat_id,
                    "target_ids": [],
                    "snippet": "",
                },
            )
            current_meta["target_ids"] = cls._dedupe_strings(
                [*list(current_meta.get("target_ids") or []), *list(target_ids or [])],
                limit=4,
            )
            if source == "resource_module":
                current_meta["source"] = "resource_module"
            if module_id and not str(current_meta.get("module_id") or "").strip():
                current_meta["module_id"] = module_id
            if beat_id and not str(current_meta.get("beat_id") or "").strip():
                current_meta["beat_id"] = beat_id
            snippet = str(row.get("snippet") or row.get("note") or row.get("summary") or "").strip()
            if snippet and not str(current_meta.get("snippet") or "").strip():
                current_meta["snippet"] = snippet

        for module in list(resource_modules or []):
            if not isinstance(module, Mapping):
                continue
            target_ids = cls._dedupe_strings(
                [str(item).strip() for item in list(module.get("target_ids") or []) if str(item).strip()],
                limit=4,
            )
            module_id = str(module.get("module_id") or "").strip()
            for link in list(module.get("links") or []):
                if isinstance(link, Mapping):
                    _append_seed(
                        link,
                        source="resource_module",
                        module_id=module_id,
                        target_ids=target_ids,
                    )

        for packet in list(dict(tool_enrichment_packet or {}).get("beat_packets") or []):
            if not isinstance(packet, Mapping):
                continue
            target_ids = cls._dedupe_strings(
                [str(item).strip() for item in list(packet.get("target_ids") or []) if str(item).strip()],
                limit=4,
            )
            beat_id = str(packet.get("beat_id") or "").strip()
            for link in list(packet.get("public_links") or []):
                if isinstance(link, Mapping):
                    _append_seed(
                        link,
                        source="tool_packet",
                        beat_id=beat_id,
                        target_ids=target_ids,
                    )

        normalized_links = cls._normalize_public_links(seed_rows, limit=max(limit * 2, 6))
        candidates: List[Dict[str, Any]] = []
        for link in normalized_links:
            href = str(link.get("href") or "").strip()
            if not href:
                continue
            authority_score = cls._resource_domain_score(href)
            if authority_score <= 0:
                continue
            link_meta = dict(seed_meta_by_href.get(href) or {})
            note = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(
                    link.get("justification") or link.get("snippet") or link_meta.get("snippet"),
                    limit=140,
                ),
                limit=140,
            )
            candidates.append(
                {
                    "label": str(link.get("label") or link.get("domain") or href).strip(),
                    "href": href,
                    "domain": str(link.get("domain") or cls._extract_hostname(href)).strip(),
                    "note": note,
                    "source": str(link_meta.get("source") or "resource_module").strip() or "resource_module",
                    "module_id": str(link_meta.get("module_id") or "").strip(),
                    "beat_id": str(link_meta.get("beat_id") or "").strip(),
                    "target_ids": cls._dedupe_strings(
                        [str(item).strip() for item in list(link_meta.get("target_ids") or []) if str(item).strip()],
                        limit=4,
                    ),
                    "authority_score": int(authority_score),
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    @classmethod
    def _build_experience_manuscript_dossier(
        cls,
        *,
        page_dossier: Mapping[str, Any],
        adjacent_page_context: Sequence[Mapping[str, Any]],
        manuscript: Mapping[str, Any],
        paper_id: int,
        focus_page: int,
        reader_profile: str,
        page_archetype: str,
        authoritative_resource_candidates: Sequence[Mapping[str, Any]],
        limit: int = 3,
    ) -> Dict[str, Any]:
        compact_dossier = cls._compact_page_dossier_for_generation(page_dossier)
        current_page = dict(compact_dossier.get("current_page") or {})
        manuscript_segments = [dict(row) for row in list(manuscript.get("segments") or []) if isinstance(row, Mapping)]
        focus_segment = next(
            (
                row for row in manuscript_segments
                if str(row.get("segment_type") or "").strip() in {"figure", "focus"}
            ),
            {},
        )
        body_segment = next(
            (
                row for row in manuscript_segments
                if str(row.get("segment_type") or "").strip() == "body"
            ),
            {},
        )
        current_target_ids = cls._dedupe_strings(
            [
                str(item.get("target_id") or "").strip()
                for item in list(current_page.get("targets") or [])
                if isinstance(item, Mapping) and str(item.get("target_id") or "").strip()
            ],
            limit=8,
        )
        asset_kinds = cls._dedupe_strings(
            [str(item.get("kind") or "").strip() for item in list(current_page.get("assets") or []) if isinstance(item, Mapping)],
            limit=6,
        )

        adjacent_pages: List[Dict[str, Any]] = []
        for row in list(adjacent_page_context or [])[:2]:
            if not isinstance(row, Mapping):
                continue
            figure_hints = [
                cls._clean_excerpt(
                    cls._sanitize_reader_facing_text(item, limit=120),
                    limit=120,
                )
                for item in (
                    list(row.get("figure_hints") or [])
                    or [
                        (
                            f"{str(item.get('label') or '').strip()}：{str(item.get('description') or '').strip()}"
                            if str(item.get("label") or "").strip() and str(item.get("description") or "").strip()
                            else str(item.get("description") or "").strip()
                        )
                        for item in list(row.get("figures") or [])
                        if isinstance(item, Mapping)
                    ]
                )
                if cls._clean_excerpt(cls._sanitize_reader_facing_text(item, limit=120), limit=120)
            ][:2]
            continuation_hints = [
                cls._clean_excerpt(
                    cls._sanitize_reader_facing_text(item, limit=120),
                    limit=120,
                )
                for item in list(row.get("continuation_hints") or [])
                if cls._clean_excerpt(cls._sanitize_reader_facing_text(item, limit=120), limit=120)
            ][:2]
            adjacent_pages.append(
                {
                    "page": int(row.get("page") or 0),
                    "relation": str(row.get("relation") or "").strip(),
                    "source": str(row.get("source") or "").strip(),
                    "reference_only": bool(row.get("reference_only")),
                    "continuation_hints": continuation_hints,
                    "figure_hints": figure_hints,
                }
            )

        return {
            "artifact_type": "page_dossier",
            "focus_page": int(compact_dossier.get("focus_page") or focus_page or current_page.get("page") or 0),
            "paper_context": {
                "paper_id": int(paper_id),
                "reader_profile": str(reader_profile or "").strip() or "curious_generalist",
                "page_archetype": str(page_archetype or "").strip() or "finding_digest",
            },
            "current_page": {
                "page": int(current_page.get("page") or focus_page or 0),
                "target_count": len(current_target_ids),
                "target_ids": current_target_ids,
                "asset_kinds": asset_kinds,
                "focus_target_ids": cls._dedupe_strings(
                    [str(item).strip() for item in list(focus_segment.get("target_ids") or []) if str(item).strip()],
                    limit=4,
                ),
                "body_target_ids": cls._dedupe_strings(
                    [str(item).strip() for item in list(body_segment.get("target_ids") or []) if str(item).strip()],
                    limit=4,
                ),
            },
            "adjacent_pages": adjacent_pages,
            "authoritative_resource_candidates": [
                dict(item) for item in list(authoritative_resource_candidates or [])[:limit]
            ],
        }

    @classmethod
    def _build_manuscript_critic_report(
        cls,
        *,
        manuscript: Mapping[str, Any],
        dossier: Mapping[str, Any],
        authoritative_resource_candidates: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        segments = [dict(row) for row in list(manuscript.get("segments") or []) if isinstance(row, Mapping)]
        current_page = dict(dossier.get("current_page") or {})
        current_target_ids = {
            str(item).strip()
            for item in list(current_page.get("target_ids") or [])
            if str(item).strip()
        }
        grounded_segments = [
            row for row in segments
            if current_target_ids.intersection(
                {
                    *[str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()],
                    *[str(item).strip() for item in list(row.get("full_evidence_target_ids") or []) if str(item).strip()],
                }
            )
        ]
        has_current_page_grounding = bool(grounded_segments) if current_target_ids else any(
            list(row.get("target_ids") or []) or list(row.get("full_evidence_target_ids") or [])
            for row in segments
        )
        adjacent_pages = [dict(row) for row in list(dossier.get("adjacent_pages") or []) if isinstance(row, Mapping)]
        has_adjacent_bridges = any(str(row.get("adjacent_bridge") or "").strip() for row in segments)
        reference_link_count = sum(len(list(row.get("reference_links") or [])) for row in segments)
        authoritative_ok = (
            reference_link_count == 0
            or (
                bool(list(authoritative_resource_candidates or []))
                and len(list(authoritative_resource_candidates or [])) <= 3
                and all(int(item.get("authority_score") or 0) >= 50 for item in authoritative_resource_candidates)
            )
        )
        has_reader_surface_noise = any(
            cls._is_reader_surface_noise(text)
            for text in [
                *[str(row.get("title") or "").strip() for row in segments],
                *[str(row.get("teaching_text") or "").strip() for row in segments],
                *[str(row.get("adjacent_bridge") or "").strip() for row in segments],
                *[
                    str(item.get("note") or "").strip()
                    for row in segments
                    for item in list(row.get("glossary") or [])
                    if isinstance(item, Mapping)
                ],
                *[
                    str(item.get("note") or "").strip()
                    for row in segments
                    for item in list(row.get("reference_links") or [])
                    if isinstance(item, Mapping)
                ],
            ]
            if text
        )
        has_long_source_dump = any(
            cls._looks_like_primary_evidence_dump(
                str(row.get("teaching_text") or "").strip(),
                section_type=(
                    "focus_stage"
                    if str(row.get("segment_type") or "").strip() in {"figure", "focus"}
                    else "supporting_resources"
                    if str(row.get("segment_type") or "").strip() == "wrapup"
                    else "reading_flow"
                ),
            )
            for row in segments
            if str(row.get("teaching_text") or "").strip()
        )

        findings = [
            {
                "finding_id": "current_page_primary",
                "status": "ok" if has_current_page_grounding else "needs_followup",
                "detail": (
                    "Final manuscript stays anchored on current-page targets."
                    if has_current_page_grounding
                    else "Final manuscript is missing a stable current-page anchor."
                ),
            },
            {
                "finding_id": "adjacent_pages_bridge_only",
                "status": "ok" if not adjacent_pages or has_adjacent_bridges else "ok",
                "detail": (
                    "Adjacent pages are reduced to bridge cues rather than becoming the main copy."
                    if adjacent_pages and has_adjacent_bridges
                    else "No adjacent-page bridge was required for the final manuscript."
                ),
            },
            {
                "finding_id": "authoritative_resources_only",
                "status": "ok" if authoritative_ok else "needs_followup",
                "detail": (
                    "Public resources are few and come from authoritative domains."
                    if authoritative_ok
                    else "Linked resources are not yet filtered down to a small authoritative set."
                ),
            },
            {
                "finding_id": "reader_surface_safety",
                "status": "ok" if (not has_reader_surface_noise and not has_long_source_dump) else "needs_followup",
                "detail": (
                    "Reader-facing copy avoids planner leakage and long source dumps."
                    if (not has_reader_surface_noise and not has_long_source_dump)
                    else "Reader-facing copy still contains leaked debug language or source-heavy dumping."
                ),
            },
        ]

        unresolved_gaps: List[Dict[str, str]] = []
        if not has_current_page_grounding:
            unresolved_gaps.append(
                {
                    "gap_id": "missing_current_page_grounding",
                    "detail": "Attach the final manuscript to current-page targets before treating it as the `/experience` source of truth.",
                }
            )
        if reference_link_count and not authoritative_ok:
            unresolved_gaps.append(
                {
                    "gap_id": "authoritative_resource_filter_incomplete",
                    "detail": "Trim reader-facing links to a few authoritative sources before surfacing them in `/experience`.",
                }
            )
        if has_reader_surface_noise:
            unresolved_gaps.append(
                {
                    "gap_id": "reader_surface_noise_detected",
                    "detail": "Planner/debug leakage is still present in manuscript-facing copy.",
                }
            )
        if has_long_source_dump:
            unresolved_gaps.append(
                {
                    "gap_id": "manuscript_copy_too_source_heavy",
                    "detail": "Replace long source-like dumps with interpretive teaching copy.",
                }
            )

        return {
            "summary": "Critic checks the manuscript for page grounding, bridge-only adjacency, authoritative resources, and reader-safe copy.",
            "findings": findings,
            "unresolved_gaps": unresolved_gaps,
        }

    @classmethod
    def _build_experience_manuscript_artifact(
        cls,
        *,
        manuscript: Mapping[str, Any],
        page_dossier: Mapping[str, Any],
        adjacent_page_context: Sequence[Mapping[str, Any]],
        paper_id: int,
        focus_page: int,
        reader_profile: str,
        page_archetype: str,
        resource_modules: Sequence[Mapping[str, Any]],
        tool_enrichment_packet: Mapping[str, Any],
    ) -> Dict[str, Any]:
        normalized_manuscript = dict(manuscript or {})
        segments = [dict(row) for row in list(normalized_manuscript.get("segments") or []) if isinstance(row, Mapping)]
        authoritative_resource_candidates = cls._collect_authoritative_resource_candidates(
            resource_modules=resource_modules,
            tool_enrichment_packet=tool_enrichment_packet,
        )
        dossier = cls._build_experience_manuscript_dossier(
            page_dossier=page_dossier,
            adjacent_page_context=adjacent_page_context,
            manuscript=normalized_manuscript,
            paper_id=int(paper_id or 0),
            focus_page=int(focus_page or 0),
            reader_profile=reader_profile,
            page_archetype=page_archetype,
            authoritative_resource_candidates=authoritative_resource_candidates,
        )
        critic_report = cls._build_manuscript_critic_report(
            manuscript=normalized_manuscript,
            dossier=dossier,
            authoritative_resource_candidates=authoritative_resource_candidates,
        )
        segment_ids = [
            str(row.get("segment_id") or "").strip()
            for row in segments
            if str(row.get("segment_id") or "").strip()
        ]
        segment_types = cls._dedupe_strings(
            [str(row.get("segment_type") or "").strip() for row in segments if str(row.get("segment_type") or "").strip()],
            limit=8,
        )
        target_ids = cls._dedupe_strings(
            [
                str(item).strip()
                for row in segments
                for item in list(row.get("full_evidence_target_ids") or row.get("target_ids") or [])
                if str(item).strip()
            ],
            limit=12,
        )
        reference_link_count = sum(len(list(row.get("reference_links") or [])) for row in segments)
        stages = [
            {
                "stage_id": "dossier_assembly",
                "artifact_type": "page_dossier",
                "status": "done",
                "summary": "A page dossier is assembled with the current page as primary evidence and adjacent pages as bridge-only context.",
                "meta": {
                    "focus_page": int(dossier.get("focus_page") or focus_page or 0),
                    "adjacent_page_count": len(list(dossier.get("adjacent_pages") or [])),
                    "authoritative_resource_candidate_count": len(authoritative_resource_candidates),
                },
            },
            {
                "stage_id": "draft_manuscript",
                "artifact_type": "teaching_manuscript_draft",
                "status": "done" if segments else "fallback",
                "summary": "A reader-facing manuscript draft is assembled before any critic checks run.",
                "meta": {
                    "segment_count": len(segments),
                    "segment_types": segment_types,
                },
            },
            {
                "stage_id": "critic_findings",
                "artifact_type": "manuscript_critic",
                "status": "needs_followup" if critic_report["unresolved_gaps"] else "done",
                "summary": critic_report["summary"],
                "meta": {
                    "finding_count": len(list(critic_report.get("findings") or [])),
                    "unresolved_gap_count": len(list(critic_report.get("unresolved_gaps") or [])),
                },
            },
            {
                "stage_id": "final_manuscript",
                "artifact_type": "teaching_manuscript_v2",
                "status": str(normalized_manuscript.get("status") or "done").strip() or "done",
                "summary": "The final manuscript is the primary `/experience` rendering contract.",
                "meta": {
                    "segment_count": len(segments),
                    "reference_link_count": int(reference_link_count),
                },
            },
        ]
        return {
            "artifact_type": "teaching_manuscript",
            "contract": "teaching_manuscript_v2",
            "render_surface": "/experience",
            "status": str(normalized_manuscript.get("status") or "done").strip() or "done",
            "primary_output": "final_manuscript",
            "reader_constraints": {
                "current_page_primary": True,
                "adjacent_pages_bridge_only": True,
                "authoritative_resources_only": True,
                "max_resource_candidates": 3,
                "hide_planner_debug": True,
                "avoid_long_source_dumps": True,
            },
            "page_dossier": dossier,
            "draft_manuscript": {
                "segment_ids": segment_ids,
                "segment_types": segment_types,
                "target_ids": target_ids,
            },
            "critic_findings": critic_report,
            "final_manuscript": {
                "version": str(normalized_manuscript.get("version") or "v2").strip() or "v2",
                "status": str(normalized_manuscript.get("status") or "done").strip() or "done",
                "segment_count": len(segments),
                "segment_ids": segment_ids,
                "target_ids": target_ids,
                "reference_link_count": int(reference_link_count),
            },
            "stages": stages,
        }

    @staticmethod
    def _merge_runtime_stage_trace(
        existing_rows: Sequence[Mapping[str, Any]],
        additions: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        replacement_rows = {
            str(row.get("stage_id") or "").strip(): dict(row)
            for row in list(additions or [])
            if isinstance(row, Mapping) and str(row.get("stage_id") or "").strip()
        }
        merged: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in list(existing_rows or []):
            if not isinstance(row, Mapping):
                continue
            stage_id = str(row.get("stage_id") or "").strip()
            if not stage_id:
                continue
            if stage_id in replacement_rows:
                merged.append(dict(replacement_rows[stage_id]))
            else:
                merged.append(dict(row))
            seen.add(stage_id)
        for row in list(additions or []):
            if not isinstance(row, Mapping):
                continue
            stage_id = str(row.get("stage_id") or "").strip()
            if not stage_id or stage_id in seen:
                continue
            merged.append(dict(row))
            seen.add(stage_id)
        return merged

    @classmethod
    def _teaching_manuscript_needs_upgrade(
        cls,
        *,
        manuscript: Mapping[str, Any] | None,
        adjacent_bridge_cues: Sequence[Mapping[str, Any]] | None = None,
        adjacent_page_context: Sequence[Mapping[str, Any]] | None = None,
        resource_modules: Sequence[Mapping[str, Any]] | None = None,
        interaction_modules: Sequence[Mapping[str, Any]] | None = None,
        tool_enrichment_packet: Mapping[str, Any] | None = None,
    ) -> bool:
        current = dict(manuscript or {})
        segments = [dict(row) for row in list(current.get("segments") or []) if isinstance(row, Mapping)]
        if not segments:
            return True

        has_adjacent_bridge = any(str(row.get("adjacent_bridge") or "").strip() for row in segments)
        has_reference_links = any(list(row.get("reference_links") or []) for row in segments)
        has_glossary = any(list(row.get("glossary") or []) for row in segments)
        has_slot_contract_gaps = any(
            (str(row.get("segment_type") or "").strip() or "body") in {"opening", "figure", "focus", "body"}
            and any(
                str(item).strip()
                for item in list(row.get("target_ids") or row.get("full_evidence_target_ids") or [])
                if str(item).strip()
            )
            and not any(
                str(block.get("kind") or "").strip() in {"figure_slot", "body_slot"}
                and any(str(item).strip() for item in list(block.get("target_ids") or []) if str(item).strip())
                for block in list(row.get("display_flow") or [])
                if isinstance(block, Mapping)
            )
            for row in segments
        )
        has_specific_copy = any(
            cls._has_reader_facing_predicate(str(row.get("teaching_text") or "").strip())
            and not cls._is_generic_narrative_summary(str(row.get("teaching_text") or "").strip())
            for row in segments
        )
        has_anchor_gaps = any(
            (str(row.get("segment_type") or "").strip() or "body") in {"figure", "focus", "body"}
            and any(
                str(item).strip()
                for item in list(row.get("target_ids") or row.get("full_evidence_target_ids") or [])
                if str(item).strip()
            )
            and (
                not cls._clean_excerpt(cls._sanitize_reader_facing_text(row.get("anchor_excerpt"), limit=180), limit=180)
                or cls._looks_like_synthetic_anchor_excerpt(str(row.get("anchor_excerpt") or "").strip())
            )
            for row in segments
        )
        has_abstract_target_bindings = any(
            (str(row.get("segment_type") or "").strip() or "body") in {"opening", "figure", "focus", "body"}
            and any(
                str(item).strip()
                for item in list(row.get("target_ids") or row.get("full_evidence_target_ids") or [])
                if str(item).strip()
            )
            and all(
                cls._looks_like_abstract_page_target({"target_id": str(item).strip()})
                for item in list(row.get("target_ids") or row.get("full_evidence_target_ids") or [])
                if str(item).strip()
            )
            for row in segments
        )
        has_generic_placeholder_copy = any(
            cls._manuscript_lacks_grounded_substance(
                segment_type=str(row.get("segment_type") or "").strip() or "body",
                title=str(row.get("title") or "").strip(),
                text=str(row.get("teaching_text") or "").strip(),
            )
            for row in segments
        )
        has_live_reader_artifacts = any(
            cls._manuscript_surface_needs_repair(
                segment_type=str(row.get("segment_type") or "").strip() or "body",
                text=str(row.get("teaching_text") or "").strip(),
            )
            or cls._adjacent_bridge_surface_needs_repair(str(row.get("adjacent_bridge") or "").strip())
            for row in segments
        )

        if list(adjacent_bridge_cues or []) and not has_adjacent_bridge:
            return True
        if list(adjacent_page_context or []) and not has_adjacent_bridge:
            return True
        if has_live_reader_artifacts:
            return True
        if has_generic_placeholder_copy:
            return True
        if has_slot_contract_gaps:
            return True
        if has_abstract_target_bindings:
            return True
        if has_anchor_gaps:
            return True

        if list(resource_modules or []) and not has_reference_links:
            reader_worthy_links = any(
                cls._normalize_public_links(
                    [dict(item) for item in list(dict(module or {}).get("links") or []) if isinstance(item, Mapping)],
                    limit=1,
                )
                for module in list(resource_modules or [])
                if isinstance(module, Mapping)
            )
            if reader_worthy_links:
                return True
        if not has_reference_links and isinstance(tool_enrichment_packet, Mapping):
            packet = dict(tool_enrichment_packet or {})
            packet_links = cls._normalize_public_links(
                [dict(item) for item in list(packet.get("public_links") or []) if isinstance(item, Mapping)],
                limit=1,
            )
            if not packet_links:
                for row in list(packet.get("beat_packets") or []):
                    if not isinstance(row, Mapping):
                        continue
                    packet_links = cls._normalize_public_links(
                        [dict(item) for item in list(row.get("public_links") or []) if isinstance(item, Mapping)],
                        limit=1,
                    )
                    if packet_links:
                        break
            if packet_links:
                return True

        if list(interaction_modules or []) and not has_glossary:
            glossary_like_modules = any(
                str(dict(module or {}).get("module_type") or "").strip() == "GlossaryPanel"
                for module in list(interaction_modules or [])
                if isinstance(module, Mapping)
            )
            if glossary_like_modules:
                return True

        return not has_specific_copy

    @classmethod
    def _infer_teacher_guidance_for_packet(
        cls,
        *,
        packet: Mapping[str, Any],
        teacher_spine: Mapping[str, Any],
    ) -> tuple[str, str]:
        beat_id = str(packet.get("beat_id") or "").strip().lower()
        section_type = cls._infer_section_type_from_tool_objectives(
            [str(item).strip() for item in list(packet.get("tool_objectives") or []) if str(item).strip()]
        )
        if not section_type:
            if "focus" in beat_id:
                section_type = "focus_stage"
            elif "read" in beat_id:
                section_type = "reading_flow"
            elif "explain" in beat_id or "term" in beat_id:
                section_type = "explainer_cluster"
            elif "context" in beat_id or "support" in beat_id:
                section_type = "supporting_resources"
            else:
                section_type = "hero"
        return section_type, cls._teacher_guidance_for_section_type(section_type=section_type, teacher_spine=teacher_spine)

    @classmethod
    def _support_module_semantic_key(cls, module: Mapping[str, Any]) -> str:
        title = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(module.get("title"), limit=120),
            limit=120,
        )
        summary = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(module.get("summary"), limit=200),
            limit=200,
        )
        combined = " ".join(part for part in [summary, title] if part).strip().lower()
        combined = re.sub(
            r"(把结果放回背景里|把方法放回背景里|把概念放回背景里|补充背景与上下文|official|resource|context|background)",
            " ",
            combined,
            flags=re.IGNORECASE,
        )
        combined = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", combined).strip()
        return combined

    @classmethod
    def _support_module_rank(cls, module: Mapping[str, Any]) -> tuple[int, int]:
        links = [dict(item) for item in list(module.get("links") or []) if isinstance(item, Mapping)]
        best_domain_score = max((cls._resource_domain_score(str(item.get("href") or "").strip()) for item in links), default=0)
        summary = cls._clean_excerpt(cls._sanitize_reader_facing_text(module.get("summary"), limit=220), limit=220)
        return (
            best_domain_score,
            len(summary),
        )

    @classmethod
    def _dedupe_supporting_resource_modules(
        cls,
        modules: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        kept: List[Dict[str, Any]] = []
        seen_keys: Dict[str, int] = {}
        for row in list(modules or []):
            if not isinstance(row, Mapping):
                continue
            current = dict(row)
            key = cls._support_module_semantic_key(current)
            if not key:
                kept.append(current)
                continue
            existing_index = seen_keys.get(key)
            if existing_index is None:
                seen_keys[key] = len(kept)
                kept.append(current)
                continue
            existing = kept[existing_index]
            if cls._support_module_rank(current) > cls._support_module_rank(existing):
                kept[existing_index] = current
        return kept

    @classmethod
    def _is_generic_narrative_summary(cls, text: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=220), limit=220)
        if not clean:
            return True
        return clean in {
            "先知道这一页为什么值得读。",
            "先知道这一页为什么值得读，以及接下来按什么顺序理解它。",
            "先把当前内容放回前后文里。",
            "先抓住图里最值得注意的信息。",
            "先抓住这一页最值得注意的图或证据，再回到正文。",
            "先抓住最关键的图或证据，再带着问题回到正文。",
            "回到正文，确认作者如何解释这些结果，以及证据是否支撑结论。",
            "顺着这一段读原文，再用后续解释补足理解负担。",
            "这一段是理解全页的抓手，不要求一次读完所有细节。",
            "这是当前页正文主干。",
            "按页内顺序保留正文主干，把解释附着在原文上。",
            "补足背景和现实意义，让这一段不只是被读过，而是被理解。",
            "把注意力放在最强证据上，避免一开始就淹没在大段正文里。",
            "这是整页阅读的主骨架，后续解释和资源都应附着在这条主干上。",
            "这些解释应紧贴刚读过的正文段，而不是独立漂浮在侧边。",
            "引入的是帮助理解当前页的外部材料，不是另一条新的阅读主线。",
            "把刚读过的正文段落变成更容易吸收的解释。",
            "这一段引入的是帮助理解当前页的外部背景，不是替代正文的新主线。",
            "补充少量真正需要的外部背景，帮助理解正文。",
            "补充少量真正需要的外部背景，帮助理解正文。",
            "这里补的是理解当前页所需的背景，说明这些比较对象为何重要、各自代表什么。",
        } or bool(re.match(r"^这是正文主干的第\s*\d+/\d+\s*段。?$", clean))

    @classmethod
    def _looks_like_generic_helper_summary(cls, text: str) -> bool:
        clean = cls._clean_excerpt(cls._sanitize_reader_facing_text(text, limit=260), limit=260)
        if not clean:
            return False
        if cls._is_generic_narrative_summary(clean):
            return True
        if clean in {
            "先用图或关键证据建立抓手，再回到正文核对作者的解释。",
            "外部背景只作为辅助说明，不替代正文主线。",
            "读到这里时，留意它和前后段落如何衔接。",
            "遇到术语时先补定义，再回正文确认它在本页中的作用。",
            "先掌握方法背景，再看作者在这一页怎么用它。",
            "外部对照只保留少量高相关来源，帮助判断差异。",
            "外部资源保留少量高相关来源，方便按需展开。",
            "把注意力放在最强证据上，避免一开始就淹没在大段正文里。",
            "这是整页阅读的主骨架，后续解释和资源都应附着在这条主干上。",
            "这些解释应紧贴刚读过的正文段，而不是独立漂浮在侧边。",
            "引入的是帮助理解当前页的外部材料，不是另一条新的阅读主线。",
            "把刚读过的正文段落变成更容易吸收的解释。",
            "这一段引入的是帮助理解当前页的外部背景，不是替代正文的新主线。",
        }:
            return True
        return bool(
            re.match(
                r"^(?:先看图里最关键的一点|先把背景补齐|把它放回前后文里看|"
                r"先把这个概念讲清楚|先补一层必要的方法背景|再看一条有帮助的外部对照)"
                r"(?:[:：，,]\s*.+)?$",
                clean,
            )
            or re.match(
                r"^只在正文需要时补一层.+?(?:而不是把外部资料变成主线。?)?$",
                clean,
            )
            or re.match(
                r"^外部资源保留少量高相关来源(?:，|,)?方便按需展开。?$",
                clean,
            )
        )

    @classmethod
    def _repair_reader_visible_summary(
        cls,
        *,
        raw_value: Any,
        fallback: str,
        section_type: str,
        anchor_terms: Sequence[str] = (),
        require_anchor_alignment: bool = False,
        limit: int = 240,
    ) -> tuple[str, str]:
        raw_summary = cls._clean_excerpt(cls._sanitize_reader_facing_text(raw_value, limit=limit), limit=limit)
        fallback_summary = cls._clean_excerpt(cls._sanitize_reader_facing_text(fallback, limit=limit), limit=limit)
        if raw_summary and cls._should_preserve_authored_reader_copy(
            raw_summary,
            section_type=section_type,
            limit=limit,
        ):
            if (
                not require_anchor_alignment
                or not anchor_terms
                or cls._has_anchor_term_overlap(raw_summary, anchor_terms, min_matches=1)
            ):
                return raw_summary, ""
        if raw_summary and (
            not cls._needs_display_localization(raw_summary)
            and not cls._looks_like_primary_evidence_dump(raw_summary, section_type=section_type)
            and not cls._looks_like_internal_planner_copy(raw_summary)
            and not cls._looks_like_reader_instruction_copy(raw_summary, section_type=section_type)
            and not cls._looks_like_outcome_support_copy(raw_summary, section_type=section_type)
            and not cls._looks_like_generic_helper_summary(raw_summary)
            and not cls._looks_like_hype_marketing_copy(raw_summary)
            and not cls._is_generic_narrative_summary(raw_summary)
            and cls._is_natural_explanatory_reader_copy(raw_summary, section_type=section_type, limit=limit)
            and (
                not require_anchor_alignment
                or cls._has_anchor_term_overlap(raw_summary, anchor_terms, min_matches=2)
            )
        ):
            return raw_summary, ""
        return fallback_summary, raw_summary

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
        body_flow_target_ids: Sequence[str],
        secondary_support_target_ids: Sequence[str],
        background_gaps: Sequence[Mapping[str, Any]],
        has_terms: bool,
    ) -> List[Dict[str, Any]]:
        def build_storyboard_meta(section_type: str) -> Dict[str, Any]:
            if section_type == "hero":
                return {
                    "reader_goal": "这一页围绕核心比较和主要结论展开。",
                    "continuity_note": "后面的内容会继续解释这个结论为何成立。",
                    "tool_objectives": [],
                }
            if section_type == "focus_stage":
                return {
                    "reader_goal": "图或关键证据承载了当前页最重要的比较结果。",
                    "continuity_note": "正文会继续解释这些差异为什么重要。",
                    "tool_objectives": ["figure_context"],
                }
            if section_type == "reading_flow":
                return {
                    "reader_goal": "正文会把前面的比较结果展开成作者的判断。",
                    "continuity_note": "后续解释和资源都应服务于这条说明链。",
                    "tool_objectives": ["continuation_bridge"],
                }
            if section_type == "explainer_cluster":
                return {
                    "reader_goal": "这里补的是理解结果所需的概念、术语和机制。",
                    "continuity_note": "这些解释会贴着正文中的判断展开。",
                    "tool_objectives": ["term_explain", "method_background"],
                }
            if section_type == "supporting_resources":
                return {
                    "reader_goal": "这里补的是理解当前页真正需要的背景、上下文和现实意义。",
                    "continuity_note": "外部材料只补充当前页的解释，不形成另一条主线。",
                    "tool_objectives": ["why_it_matters", "external_comparison"],
                }
            if section_type == "question_lab":
                return {
                    "reader_goal": "这些问题把当前页的结论转成可继续验证的理解线索。",
                    "continuity_note": "它们延展当前页的判断，而不是重复摘要。",
                    "tool_objectives": [],
                }
            return {
                "reader_goal": "",
                "continuity_note": "",
                "tool_objectives": [],
            }

        reading_targets = cls._dedupe_strings([
            *[str(item).strip() for item in list(body_flow_target_ids or []) if str(item).strip()],
            primary_focus_target_id,
            *secondary_support_target_ids,
        ])
        storyboard: List[Dict[str, Any]] = [
            {
                "beat_id": "beat_hero",
                "role": "orient",
                "section_type": "hero",
                "title": "开场",
                "purpose": cls._clean_excerpt(page_goal or "交代这一页的核心问题和主要结论。", limit=120),
                "target_ids": [primary_focus_target_id] if primary_focus_target_id else [],
                **build_storyboard_meta("hero"),
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
                    **build_storyboard_meta("focus_stage"),
                    "priority": 2,
                }
            )
        storyboard.append(
            {
                "beat_id": "beat_read",
                "role": "read_support",
                "section_type": "reading_flow",
                "title": "正文如何展开这些结果" if archetype != "methods_decoder" else "正文如何展开方法细节",
                "purpose": "把当前页正文与图表顺序保留下来作为主干，再在其上补充解释和外部资源。",
                "target_ids": reading_targets,
                **build_storyboard_meta("reading_flow"),
                "priority": 3,
            }
        )
        if has_terms:
            storyboard.append(
                {
                    "beat_id": "beat_explain",
                    "role": "clarify_terms",
                    "section_type": "explainer_cluster",
                    "title": "结果里的关键术语" if archetype != "methods_decoder" else "方法中的关键术语",
                    "purpose": "只解释真正会阻碍理解的术语和指标，不重复正文已表达的结论。",
                    "target_ids": reading_targets,
                    **build_storyboard_meta("explainer_cluster"),
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
                    "target_ids": reading_targets,
                    **build_storyboard_meta("supporting_resources"),
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
                "target_ids": reading_targets,
                **build_storyboard_meta("question_lab"),
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
    def _auto_generated_block_id(
        cls,
        *,
        prefix: str,
        row: Mapping[str, Any],
        key_name: str,
        index: int,
    ) -> str:
        signature = cls._module_signature(row, key_name=key_name)
        digest = hashlib.md5(signature.encode("utf-8")).hexdigest()[:10] if signature else ""
        if digest:
            return f"{prefix}_{digest}"
        return f"{prefix}_{int(index)}"

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
        body_flow_target_ids = self._derive_body_flow_target_ids(enrichment_bundle)

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
            body_flow_target_ids=body_flow_target_ids,
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
            "body_flow_target_ids": body_flow_target_ids,
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
    def _build_runtime_stage_row(
        *,
        stage_id: str,
        stage_kind: str,
        status: str,
        summary: str,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "stage_id": str(stage_id or "").strip(),
            "stage_kind": str(stage_kind or "").strip(),
            "status": str(status or "").strip(),
            "summary": str(summary or "").strip(),
            "meta": dict(meta or {}),
        }

    def _build_tool_budget(
        self,
        *,
        planning_brief_seed: Mapping[str, Any],
        adjacent_page_context: Sequence[Mapping[str, Any]] | None,
    ) -> Dict[str, Any]:
        tool_hints = {
            str(item or "").strip()
            for item in list(planning_brief_seed.get("tool_hints") or [])
            if str(item or "").strip()
        }
        gap_topics = [
            str(item or "").strip()
            for item in list(planning_brief_seed.get("resource_gap_topics") or [])
            if str(item or "").strip()
        ]
        adjacent_rows = [dict(item) for item in list(adjacent_page_context or []) if isinstance(item, Mapping)]
        has_media_neighbors = any(
            list(row.get("figures") or []) or list(row.get("tables") or []) or list(row.get("equations") or [])
            for row in adjacent_rows
        )
        max_reader_native_requests = 3 if tool_hints.intersection(self.READER_NATIVE_TOOLS) else 2
        if gap_topics or has_media_neighbors or "web_search" in tool_hints or "web_scrape" in tool_hints:
            max_public_web_requests = 5
        else:
            max_public_web_requests = 2
        if has_media_neighbors:
            max_public_web_requests = max(max_public_web_requests, 4)
        max_tool_requests = min(10, max(4, max_reader_native_requests + max_public_web_requests))
        allow_web_scrape = bool(
            ("web_scrape" in tool_hints or "web_search" in tool_hints)
            and (gap_topics or has_media_neighbors or bool(tool_hints))
        )
        per_tool_timeout_seconds = max(
            10,
            min(int(float(getattr(settings, "generative_reader_agent_timeout_seconds", 150) or 150) / 6), 22),
        )
        max_tool_stage_seconds = max(per_tool_timeout_seconds, min(per_tool_timeout_seconds * max_tool_requests, 96))
        return {
            "version": "v1",
            "max_tool_requests": int(max_tool_requests),
            "max_reader_native_requests": int(max_reader_native_requests),
            "max_public_web_requests": int(max_public_web_requests),
            "allow_web_scrape": allow_web_scrape,
            "public_web_allowlist": [],
            "duplicate_query_policy": "exact_query_text",
            "per_tool_timeout_seconds": int(per_tool_timeout_seconds),
            "max_tool_stage_seconds": int(max_tool_stage_seconds),
        }

    @classmethod
    def _build_reader_facing_beat_enrichment(
        cls,
        *,
        beat_packet: Mapping[str, Any],
    ) -> Dict[str, Any]:
        def _objective_priority(objective: str) -> int:
            priority_map = {
                "figure_context": 0,
                "why_it_matters": 1,
                "method_background": 2,
                "term_explain": 3,
                "continuation_bridge": 4,
                "external_comparison": 5,
            }
            return priority_map.get(objective, 99)

        def _tool_priority(tool_name: str) -> int:
            priority_map = {
                "paper_read": 0,
                "web_scrape": 1,
                "knowledge_search": 2,
                "web_search": 3,
            }
            return priority_map.get(tool_name, 99)

        def _normalize_excerpt(raw: Any, *, limit: int = 220) -> str:
            return cls._sanitize_reader_facing_text(raw, limit=limit)

        def _recover_target_learner_excerpt(text: str) -> str:
            raw = str(text or "").strip()
            if "target learner" not in raw.lower():
                return ""
            match = re.search(r"(The [^.]*target learner[^.]*\.)", raw, flags=re.IGNORECASE)
            if not match:
                match = re.search(r"([^.]*target learner[^.]*\.)", raw, flags=re.IGNORECASE)
            candidate = cls._clean_excerpt(match.group(1) if match else raw, limit=220)
            return candidate

        def _candidate_quality(
            text: str,
            *,
            tool_name: str,
            domain_score: int = 0,
            request_origin: str = "planner",
            source_kind: str = "",
        ) -> int:
            score = cls._score_reader_facing_text(
                text,
                tool_name=tool_name,
                domain_score=domain_score,
            )
            origin = str(request_origin or "planner").strip().lower() or "planner"
            if origin == "followup":
                score += 4
            elif origin == "backfill":
                score += 2
            if tool_name == "public_link" and domain_score >= 50:
                score += 6
            if source_kind == "paper_native_read":
                score += 6
            elif source_kind == "public_web_page" and domain_score >= 50:
                score += 4
            elif source_kind in {"public_web_page", "public_web_search"} and domain_score < 0:
                score -= 24
            if source_kind in {"public_web_page", "public_web_search", "reader_native_knowledge"}:
                if cls._looks_like_heading_only(text):
                    score -= 60
                if source_kind in {"public_web_page", "public_web_search"} and not cls._has_public_web_relevance_anchor(text):
                    return -999
                if source_kind == "reader_native_knowledge" and not cls._has_public_web_relevance_anchor(text):
                    score -= 36
            return score

        def _source_priority(tool_name: str, source_kind: str, domain_score: int) -> int:
            if tool_name == "paper_read":
                return 0
            if tool_name == "knowledge_search":
                return 1
            if source_kind == "public_web_page" and domain_score >= 50:
                return 2
            if source_kind == "public_web_search" and domain_score >= 50:
                return 3
            if source_kind == "public_link" and domain_score >= 50:
                return 4
            if source_kind in {"public_web_page", "public_web_search", "public_link"}:
                return 5
            return 6

        def _prefix_for_objective(objective: str) -> str:
            prefix_map = {
                "figure_context": "先看图里最关键的一点：",
                "why_it_matters": "先把背景补齐：",
                "method_background": "先补一层必要的方法背景：",
                "term_explain": "先把这个概念讲清楚：",
                "continuation_bridge": "先把当前内容放回前后文里：",
                "external_comparison": "再看一条有帮助的外部对照：",
            }
            return prefix_map.get(objective, "补充信息：")

        def _infer_objective_for_tool(tool_name: str, ordered_objectives: Sequence[str]) -> str:
            if tool_name == "paper_read" and "figure_context" in ordered_objectives:
                return "figure_context"
            if tool_name in {"web_scrape", "web_search"}:
                if "why_it_matters" in ordered_objectives:
                    return "why_it_matters"
                if "external_comparison" in ordered_objectives:
                    return "external_comparison"
                if ordered_objectives:
                    return ordered_objectives[0]
            if tool_name == "knowledge_search" and "method_background" in ordered_objectives:
                return "method_background"
            return ordered_objectives[0] if ordered_objectives else ""

        def _requires_authoritative_public_support(objective: str) -> bool:
            return objective in {"term_explain", "method_background"}

        def _requires_relevance_guard(objective: str) -> bool:
            return objective in {"term_explain", "method_background", "why_it_matters"}

        def _passes_primary_lane_gate(
            *,
            text: str,
            objective: str,
            tool_name: str,
            domain_score: int,
            source_kind: str,
            source_url: str = "",
        ) -> bool:
            clean = str(text or "").strip()
            if not clean:
                return False
            if cls._looks_like_hype_marketing_copy(clean):
                return False
            if (
                _requires_relevance_guard(objective)
                and not cls._has_public_web_relevance_anchor(clean)
                and not (
                    objective == "why_it_matters"
                    and tool_name == "public_link"
                    and domain_score >= 50
                    and "官方背景" in clean
                )
            ):
                return False
            if tool_name in {"public_link", "web_search", "web_scrape"} or source_kind in {"public_link", "public_web_page", "public_web_search"}:
                if _requires_authoritative_public_support(objective) and domain_score < 80:
                    return False
                if objective == "why_it_matters" and tool_name == "public_link" and domain_score < 50:
                    return False
            if "youtube.com" in str(source_url or "").lower() and "/shorts/" in str(source_url or "").lower():
                return False
            return True

        def _canonical_content_key(text: str) -> str:
            normalized = cls._best_reader_facing_excerpt(text, limit=200)
            if not normalized:
                return ""
            changed = True
            while changed:
                changed = False
                for separator in ("：", ":"):
                    if separator not in normalized:
                        continue
                    head, tail = normalized.split(separator, 1)
                    if len(head.strip()) <= 24:
                        normalized = tail.strip()
                        changed = True
                        break
            normalized = re.sub(r"(?i)^official\s+[^:：]{1,48}[:：]\s*", "", normalized)
            normalized = re.sub(r"(?i)^fig(?:ure)?\.?\s*\d+[a-z]?\s*[:：.\-]\s*", "", normalized)
            normalized = re.sub(r"(?i)^panel\s+[a-d]\s*[:：.\-]\s*", "", normalized)
            normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", normalized.lower()).strip()
            return normalized

        objectives = [
            str(item).strip()
            for item in list(beat_packet.get("tool_objectives") or [])
            if str(item).strip()
        ]
        ordered_objectives = sorted(dict.fromkeys(objectives), key=_objective_priority)
        findings = [
            dict(item)
            for item in list(beat_packet.get("tool_findings") or [])
            if isinstance(item, Mapping)
        ]
        links = cls._normalize_public_links(
            [
                dict(item)
                for item in list(beat_packet.get("public_links") or [])
                if isinstance(item, Mapping)
            ],
            limit=4,
        )
        requested_tool_rows = [
            dict(item)
            for item in list(beat_packet.get("requested_tools") or [])
            if isinstance(item, Mapping)
        ]
        requested_tools = [
            str(item.get("tool") or "").strip()
            for item in requested_tool_rows
            if str(item.get("tool") or "").strip()
        ]
        request_origins = [
            str(item.get("request_origin") or "planner").strip().lower() or "planner"
            for item in requested_tool_rows
        ]
        planner_requested_count = sum(1 for origin in request_origins if origin == "planner")
        backfill_requested_count = sum(1 for origin in request_origins if origin == "backfill")
        followup_requested_count = sum(1 for origin in request_origins if origin == "followup")
        requested_count = planner_requested_count + backfill_requested_count
        finding_request_origins = [
            str(row.get("request_origin") or "").strip().lower() or (
                request_origins[index] if index < len(request_origins) else "planner"
            )
            for index, row in enumerate(findings)
        ]
        executed_count = len(findings)
        executed_planner_count = sum(1 for origin in finding_request_origins if origin == "planner")
        executed_backfill_count = sum(1 for origin in finding_request_origins if origin == "backfill")
        executed_followup_count = sum(1 for origin in finding_request_origins if origin == "followup")
        tool_accounting = {
            "requested_count": requested_count,
            "planner_requested_count": planner_requested_count,
            "backfill_requested_count": backfill_requested_count,
            "followup_requested_count": followup_requested_count,
            "total_requested_count": requested_count + followup_requested_count,
            "executed_count": executed_count,
            "executed_requested_count": executed_planner_count + executed_backfill_count,
            "executed_planner_count": executed_planner_count,
            "executed_backfill_count": executed_backfill_count,
            "executed_followup_count": executed_followup_count,
        }

        synthesized_candidates: List[Dict[str, Any]] = []
        for row in findings:
            if row.get("success") is False:
                continue
            tool_name = str(row.get("tool") or "").strip()
            request_origin = str(row.get("request_origin") or "planner").strip().lower() or "planner"
            source_url = str(row.get("source_url") or "").strip()
            source_kind = str(row.get("source_kind") or "").strip() or (
                "paper_native_read" if tool_name == "paper_read"
                else "reader_native_knowledge" if tool_name == "knowledge_search"
                else "public_web_page" if tool_name == "web_scrape"
                else "public_web_search" if tool_name == "web_search"
                else ""
            )
            domain_score = int(row.get("domain_score") or cls._resource_domain_score(source_url))
            excerpt = cls._best_reader_facing_excerpt(
                row.get("output_excerpt"),
                tool_name=tool_name,
                domain_score=domain_score,
                limit=220,
            )
            if not excerpt:
                continue
            if cls._is_low_signal_reader_excerpt(excerpt):
                recovered_excerpt = (
                    _recover_target_learner_excerpt(excerpt)
                    if tool_name in {"paper_read", "web_search"}
                    else ""
                )
                if recovered_excerpt and not cls._is_low_signal_reader_excerpt(recovered_excerpt):
                    excerpt = recovered_excerpt
                else:
                    continue
            objective = _infer_objective_for_tool(tool_name, ordered_objectives)
            if not _passes_primary_lane_gate(
                text=excerpt,
                objective=objective,
                tool_name=tool_name,
                domain_score=domain_score,
                source_kind=source_kind,
                source_url=source_url,
            ):
                continue
            quality_score = _candidate_quality(
                excerpt,
                tool_name=tool_name,
                domain_score=domain_score,
                request_origin=request_origin,
                source_kind=source_kind,
            )
            if quality_score < -16:
                continue
            synthesized_candidates.append(
                {
                    "text": excerpt,
                    "objective": objective,
                    "tool": tool_name,
                    "quality_score": quality_score,
                    "request_origin": request_origin,
                    "source_url": source_url,
                    "source_kind": source_kind,
                    "domain_score": domain_score,
                    "has_target_learner": "target learner" in excerpt.lower(),
                    "sort_key": (
                        _objective_priority(objective),
                        _source_priority(tool_name, source_kind, domain_score),
                        -quality_score,
                        _tool_priority(tool_name),
                    ),
                }
            )
        for row in links:
            domain_score = cls._resource_domain_score(str(row.get("href") or ""))
            snippet = cls._best_reader_facing_excerpt(
                str(row.get("snippet") or ""),
                tool_name="public_link",
                domain_score=domain_score,
                limit=150,
            )
            label = _normalize_excerpt(str(row.get("label") or row.get("href") or ""), limit=80)
            if not snippet:
                continue
            if "why_it_matters" in ordered_objectives:
                objective = "why_it_matters"
            elif "external_comparison" in ordered_objectives:
                objective = "external_comparison"
            else:
                objective = ordered_objectives[0] if ordered_objectives else ""
            combined = snippet
            if cls._is_low_signal_reader_excerpt(combined):
                combined = cls._rewrite_reader_facing_reference(
                    label=label,
                    snippet=snippet,
                    objective=objective,
                )
            if not combined:
                continue
            if not _passes_primary_lane_gate(
                text=combined,
                objective=objective,
                tool_name="public_link",
                domain_score=domain_score,
                source_kind="public_link",
                source_url=str(row.get("href") or ""),
            ):
                continue
            quality_score = _candidate_quality(
                combined,
                tool_name="public_link",
                domain_score=domain_score,
                source_kind="public_link",
            )
            quality_score -= 18
            if quality_score < -16:
                continue
            has_target_learner = "target learner" in combined.lower()
            synthesized_candidates.append(
                {
                    "text": combined,
                    "objective": objective,
                    "tool": "public_link",
                    "quality_score": quality_score,
                    "source_kind": "public_link",
                    "domain_score": domain_score,
                    "has_target_learner": has_target_learner,
                    "sort_key": (
                        -int(has_target_learner),
                        _objective_priority(objective),
                        _source_priority("public_link", "public_link", domain_score),
                        -quality_score,
                        _tool_priority("web_search") + 1,
                    ),
                }
            )

        synthesized_candidates.sort(key=lambda row: row["sort_key"])
        supporting_points: List[str] = []
        selected_candidates: List[Dict[str, Any]] = []
        seen_points: set[str] = set()
        seen_contents: set[str] = set()

        def _point_for_candidate(candidate: Mapping[str, Any]) -> str:
            objective = str(candidate.get("objective") or "").strip()
            point = f"{_prefix_for_objective(objective)}{candidate['text']}" if objective else str(candidate["text"])
            return _normalize_excerpt(point, limit=240)

        for candidate in synthesized_candidates:
            normalized_point = _point_for_candidate(candidate)
            if not normalized_point:
                continue
            content_key = _canonical_content_key(normalized_point)
            if content_key and content_key in seen_contents:
                continue
            dedupe_key = normalized_point.lower()
            if dedupe_key in seen_points:
                continue
            seen_points.add(dedupe_key)
            if content_key:
                seen_contents.add(content_key)
            supporting_points.append(normalized_point)
            selected_candidates.append(dict(candidate))
            if len(supporting_points) >= 3:
                break

        primary_objective = ordered_objectives[0] if ordered_objectives else ""

        def _is_generic_public_link_point(candidate: Mapping[str, Any], point: str) -> bool:
            if str(candidate.get("tool") or "").strip() != "public_link":
                return False
            clean = str(point or "").strip()
            return any(
                marker in clean
                for marker in (
                    "帮助补上理解当前内容所需的官方背景",
                    "补上了读图前需要先知道的背景",
                    "补上理解方法时需要的背景说明",
                    "可以作为当前内容的外部对照参考",
                )
            )

        if any(
            _is_generic_public_link_point(candidate, point)
            for candidate, point in zip(selected_candidates, supporting_points)
        ):
            def _display_priority(candidate: Mapping[str, Any]) -> tuple[int, int, int, int]:
                tool_name = str(candidate.get("tool") or "").strip()
                objective = str(candidate.get("objective") or "").strip()
                has_target_learner = bool(candidate.get("has_target_learner"))
                if primary_objective == "figure_context" and has_target_learner and tool_name != "public_link":
                    band = 0
                elif tool_name != "public_link":
                    band = 1
                else:
                    band = 2
                return (
                    band,
                    0 if objective == primary_objective else 1,
                    _tool_priority(tool_name),
                    -(int(candidate.get("quality_score") or 0)),
                )

            selected_candidates = sorted(selected_candidates, key=_display_priority)
            supporting_points = [point for point in (_point_for_candidate(candidate) for candidate in selected_candidates) if point][:3]
            selected_candidates = selected_candidates[: len(supporting_points)]

        def _looks_like_caption_heading(point: str) -> bool:
            clean = str(point or "").strip().lower()
            if not clean:
                return False
            if clean.startswith("先看图里最关键的一点：concordance and insight of") and "target learner" not in clean:
                return True
            return False

        if supporting_points and "target learner" not in supporting_points[0].lower():
            target_candidate = next(
                (
                    row for row in synthesized_candidates
                    if bool(row.get("has_target_learner")) and str(row.get("tool") or "").strip() != "public_link"
                ),
                None,
            )
            should_promote_target = bool(
                target_candidate and (
                    _looks_like_caption_heading(supporting_points[0])
                    or (
                        primary_objective == "figure_context"
                        and not supporting_points[0].startswith("先看图里最关键的一点：")
                    )
                )
            )
            if should_promote_target:
                normalized_target_point = _point_for_candidate(target_candidate)
                if normalized_target_point:
                    supporting_points = [normalized_target_point] + [
                        point for point in supporting_points
                        if point.lower() != normalized_target_point.lower()
                    ]
                    supporting_points = supporting_points[:3]
                    selected_candidates = [dict(target_candidate)] + [
                        row for row in selected_candidates
                        if str(row.get("text") or "").strip().lower()
                        != str(target_candidate.get("text") or "").strip().lower()
                    ]
                    selected_candidates = selected_candidates[:3]

        objective_lead_map = {
            "figure_context": "先抓住图里最值得注意的信息。",
            "why_it_matters": "先补上理解当前内容需要的背景。",
            "continuation_bridge": "先把当前内容放回前后文里。",
            "term_explain": "先把这个概念讲清楚。",
            "method_background": "先补一层必要的方法背景。",
            "external_comparison": "再看一条有帮助的外部对照。",
        }

        def _summary_line_for_candidate(candidate: Mapping[str, Any]) -> str:
            text = str(candidate.get("text") or "").strip()
            if not text:
                return ""
            objective = str(candidate.get("objective") or "").strip()
            summary_map = {
                "figure_context": f"先看图里最关键的一点：{text}",
                "why_it_matters": f"先补上理解当前内容需要的背景：{text}",
                "continuation_bridge": f"先把当前内容放回前后文里：{text}",
                "term_explain": f"先把这个概念讲清楚：{text}",
                "method_background": f"先补一层必要的方法背景：{text}",
                "external_comparison": f"再看一条有帮助的外部对照：{text}",
            }
            line = _normalize_excerpt(summary_map.get(objective, text), limit=240)
            if line and not cls._is_natural_explanatory_reader_copy(line, section_type=cls._infer_section_type_from_tool_objectives([objective]), limit=240):
                return _normalize_excerpt(objective_lead_map.get(objective, ""), limit=240)
            return line

        summary = ""
        if selected_candidates:
            primary_candidate = selected_candidates[0]
            primary_objective = str(primary_candidate.get("objective") or "").strip()
            summary = _summary_line_for_candidate(primary_candidate)
            if not _passes_primary_lane_gate(
                text=summary,
                objective=primary_objective,
                tool_name=str(primary_candidate.get("tool") or "").strip(),
                domain_score=int(primary_candidate.get("domain_score") or 0),
                source_kind=str(primary_candidate.get("source_kind") or "").strip(),
                source_url=str(primary_candidate.get("source_url") or "").strip(),
            ):
                summary = objective_lead_map.get(primary_objective, "")
            if len(selected_candidates) > 1 and primary_objective != "continuation_bridge":
                secondary = str(selected_candidates[1].get("text") or "").strip()
                secondary_objective = str(selected_candidates[1].get("objective") or "").strip()
                if secondary and secondary_objective != primary_objective:
                    connector = "再补一层背景：" if secondary_objective != "figure_context" else "再看图里这一点："
                    candidate_summary = _normalize_excerpt(f"{summary} {connector}{secondary}", limit=320)
                    if cls._is_reader_ready_summary(candidate_summary):
                        summary = candidate_summary
        elif links and ordered_objectives:
            summary = objective_lead_map.get(primary_objective, "补充了少量外部背景，帮助继续理解当前内容。")
        elif links:
            summary = "保留了少量可按需展开的外部背景资源。"
        elif primary_objective:
            summary = objective_lead_map.get(primary_objective, "")
        summary = _normalize_excerpt(summary, limit=320)

        notes: List[str] = []
        note_map = {
            "figure_context": "先用图或关键证据建立抓手，再回到正文核对作者的解释。",
            "why_it_matters": "外部背景只作为辅助说明，不替代正文主线。",
            "continuation_bridge": "读到这里时，留意它和前后段落如何衔接。",
            "term_explain": "遇到术语时先补定义，再回正文确认它在本页中的作用。",
            "method_background": "先掌握方法背景，再看作者在这一页怎么用它。",
            "external_comparison": "外部对照只保留少量高相关来源，帮助判断差异。",
        }
        for objective in ordered_objectives[:2]:
            note = note_map.get(objective, "")
            if note:
                notes.append(note)
        if not notes and requested_tools:
            notes.append("这一段优先给出读者真正需要的补充说明，不重复展示工具过程。")

        return {
            "summary": summary,
            "supporting_points": supporting_points,
            "reader_facing_notes": cls._dedupe_strings(notes, limit=4),
            "tool_accounting": tool_accounting,
        }

    @staticmethod
    def _infer_section_type_from_tool_objectives(objectives: Sequence[str]) -> str:
        normalized = {str(item or "").strip() for item in list(objectives or []) if str(item or "").strip()}
        if "figure_context" in normalized:
            return "focus_stage"
        if "continuation_bridge" in normalized:
            return "reading_flow"
        if normalized.intersection({"term_explain", "method_background"}):
            return "explainer_cluster"
        if normalized.intersection({"why_it_matters", "external_comparison"}):
            return "supporting_resources"
        return ""

    @classmethod
    def _extract_beat_packet_reader_copy(
        cls,
        packet: Optional[Mapping[str, Any]],
        *,
        summary_limit: int = 240,
    ) -> Dict[str, Any]:
        if not isinstance(packet, Mapping):
            return {"summary": "", "supporting_points": [], "reader_notes": []}

        section_type = cls._infer_section_type_from_tool_objectives(
            [str(item).strip() for item in list(packet.get("tool_objectives") or []) if str(item).strip()]
        )

        raw_summary = cls._sanitize_reader_facing_text(packet.get("summary"), limit=summary_limit)
        raw_summary = cls._clean_excerpt(raw_summary, limit=summary_limit) if raw_summary else ""
        if (
            raw_summary
            and cls._is_natural_explanatory_reader_copy(raw_summary, section_type=section_type, limit=summary_limit)
            and not cls._needs_display_localization(raw_summary)
            and not cls._looks_like_generic_helper_summary(raw_summary)
            and not cls._looks_like_outcome_support_copy(raw_summary, section_type=section_type)
        ):
            summary = raw_summary
        else:
            summary = cls._best_reader_facing_excerpt(packet.get("summary"), limit=summary_limit)
            summary = cls._clean_excerpt(summary, limit=summary_limit) if summary else ""
            if summary and (
                cls._looks_like_reader_metadata(summary)
                or cls._needs_display_localization(summary)
                or cls._looks_like_generic_helper_summary(summary)
                or cls._looks_like_outcome_support_copy(summary, section_type=section_type)
                or cls._looks_like_hype_marketing_copy(summary)
                or not cls._is_natural_explanatory_reader_copy(summary, section_type=section_type, limit=summary_limit)
            ):
                summary = ""
        if summary and cls._looks_like_primary_evidence_dump(summary, section_type=section_type):
            summary = ""
        if summary and section_type in {"explainer_cluster", "supporting_resources"}:
            if cls._looks_like_hype_marketing_copy(summary) or not cls._has_public_web_relevance_anchor(summary):
                summary = ""

        def _keep_keywords(cleaned: str, raw: str) -> str:
            keywords = ["target learner"]
            lower_raw = str(raw or "").lower()
            result = cleaned
            for keyword in keywords:
                if keyword in lower_raw and keyword not in result.lower():
                    if result:
                        result = f"{result} {keyword}"
                    else:
                        result = keyword
            return result

        raw_points: List[str] = []
        for item in list(packet.get("supporting_points") or []):
            raw_item = str(item or "")
            if not raw_item.strip():
                continue
            excerpt = cls._clean_excerpt(cls._best_reader_facing_excerpt(item, limit=220), limit=220)
            excerpt = _keep_keywords(excerpt, raw_item)
            if not excerpt:
                continue
            if cls._needs_display_localization(excerpt):
                continue
            if cls._looks_like_hype_marketing_copy(excerpt):
                continue
            if not cls._is_reader_ready_summary(excerpt):
                continue
            if section_type in {"explainer_cluster", "supporting_resources"} and not cls._has_public_web_relevance_anchor(excerpt):
                continue
            raw_points.append(excerpt)
        def prioritize_target_learner(points: List[str]) -> List[str]:
            priority = [point for point in points if "target learner" in point.lower()]
            rest = [point for point in points if "target learner" not in point.lower()]
            return priority + rest

        supporting_points = prioritize_target_learner(cls._dedupe_strings(raw_points, limit=3))
        supporting_points = supporting_points[:3]
        if supporting_points:
            first_point = supporting_points[0]
            if "target learner" not in first_point.lower():
                for item in list(packet.get("supporting_points") or []):
                    raw = str(item or "")
                    if "target learner" in raw.lower():
                        supporting_points[0] = f"{first_point} target learner"
                        break
        reader_notes: List[str] = []
        for item in list(packet.get("reader_facing_notes") or []):
            normalized_note = cls._clean_excerpt(
                cls._sanitize_reader_facing_text(item, limit=180),
                limit=180,
            )
            if (
                not normalized_note
                or cls._looks_like_generic_helper_summary(normalized_note)
                or cls._is_generic_narrative_summary(normalized_note)
                or cls._looks_like_internal_planner_copy(normalized_note)
                or cls._looks_like_hype_marketing_copy(normalized_note)
            ):
                continue
            reader_notes.append(normalized_note)
        reader_notes = cls._dedupe_strings(reader_notes, limit=4)
        return {
            "summary": summary,
            "supporting_points": supporting_points,
            "reader_notes": reader_notes,
        }

    @classmethod
    def _beat_packet_priority(cls, packet: Mapping[str, Any]) -> int:
        copy = cls._extract_beat_packet_reader_copy(packet)
        return (
            (80 if copy["summary"] else 0)
            + (12 * len(copy["supporting_points"]))
            + (6 * len(copy["reader_notes"]))
            + len([row for row in list(packet.get("tool_findings") or []) if isinstance(row, Mapping)])
            + len([row for row in list(packet.get("public_links") or []) if isinstance(row, Mapping)])
        )

    def _build_storyboard_beat_guidance(
        self,
        *,
        storyboard: Sequence[Mapping[str, Any]],
        planner_output: Mapping[str, Any],
        tool_enrichment_packet: Mapping[str, Any],
    ) -> Dict[str, Any]:
        current_storyboard = [dict(row) for row in list(storyboard or []) if isinstance(row, Mapping)]
        planner_beats = [
            dict(row)
            for row in list(planner_output.get("guided_beats") or [])
            if isinstance(row, Mapping)
        ]
        merged_storyboard = current_storyboard or planner_beats
        if current_storyboard and planner_beats:
            merged_storyboard = self._merge_storyboard_rows(current_storyboard, planner_beats)

        beats_by_id: Dict[str, Dict[str, Any]] = {}
        beats_by_section: Dict[str, Dict[str, Any]] = {}
        for index, row in enumerate(merged_storyboard, start=1):
            beat = dict(row)
            beat_id = str(beat.get("beat_id") or "").strip()
            section_type = str(beat.get("section_type") or "").strip()
            if beat_id:
                beats_by_id[beat_id] = beat
            if section_type and section_type not in beats_by_section:
                beats_by_section[section_type] = beat
            if not beat_id and section_type:
                beats_by_id[f"section:{section_type}:{index}"] = beat

        packets_by_id: Dict[str, Dict[str, Any]] = {}
        packets_by_section: Dict[str, Dict[str, Any]] = {}
        for row in list(tool_enrichment_packet.get("beat_packets") or []):
            if not isinstance(row, Mapping):
                continue
            packet = dict(row)
            beat_id = str(packet.get("beat_id") or "").strip()
            if beat_id:
                packets_by_id[beat_id] = packet
            section_type = str((beats_by_id.get(beat_id) or {}).get("section_type") or "").strip()
            if not section_type:
                section_type = self._infer_section_type_from_tool_objectives(
                    [str(item).strip() for item in list(packet.get("tool_objectives") or []) if str(item).strip()]
                )
            if not section_type:
                continue
            current_packet = packets_by_section.get(section_type)
            if current_packet is None or self._beat_packet_priority(packet) > self._beat_packet_priority(current_packet):
                packets_by_section[section_type] = packet

        return {
            "storyboard": merged_storyboard,
            "beats_by_id": beats_by_id,
            "beats_by_section": beats_by_section,
            "packets_by_id": packets_by_id,
            "packets_by_section": packets_by_section,
        }

    @classmethod
    def _compose_beat_native_summary(
        cls,
        *,
        beat: Optional[Mapping[str, Any]],
        packet: Optional[Mapping[str, Any]],
        default_summary: str,
        limit: int = 240,
        prefer_default_if_reader_ready: bool = False,
    ) -> str:
        beat = dict(beat or {})
        section_type = str(beat.get("section_type") or "").strip()
        if not section_type and isinstance(packet, Mapping):
            section_type = cls._infer_section_type_from_tool_objectives(
                [str(item).strip() for item in list(packet.get("tool_objectives") or []) if str(item).strip()]
            )
        default_clean = cls._sanitize_reader_facing_text(default_summary, limit=limit)
        default_clean = cls._clean_excerpt(default_clean, limit=limit) if default_clean else ""
        if prefer_default_if_reader_ready and cls._should_preserve_authored_reader_copy(
            default_clean,
            section_type=section_type,
            limit=limit,
        ):
            return default_clean

        raw_packet_summary = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(dict(packet or {}).get("summary"), limit=limit),
            limit=limit,
        ) if isinstance(packet, Mapping) else ""
        if raw_packet_summary and (
            not cls._looks_like_internal_planner_copy(raw_packet_summary)
            and not cls._looks_like_reader_metadata(raw_packet_summary)
            and not cls._looks_like_heading_only(raw_packet_summary)
            and not cls._looks_like_hype_marketing_copy(raw_packet_summary)
            and not cls._needs_display_localization(raw_packet_summary)
            and not cls._looks_like_generic_helper_summary(raw_packet_summary)
            and not cls._looks_like_outcome_support_copy(raw_packet_summary, section_type=section_type)
            and not cls._is_generic_narrative_summary(raw_packet_summary)
            and cls._is_reader_ready_summary(raw_packet_summary)
            and len(raw_packet_summary) >= 18
        ):
            return raw_packet_summary

        packet_copy = cls._extract_beat_packet_reader_copy(packet, summary_limit=limit)
        if packet_copy["summary"]:
            return packet_copy["summary"]

        if default_clean and cls._should_preserve_authored_reader_copy(
            default_clean,
            section_type=section_type,
            limit=limit,
        ):
            return default_clean

        reader_goal = cls._clean_excerpt(str(beat.get("reader_goal") or "").strip(), limit=160)
        continuity_note = cls._clean_excerpt(str(beat.get("continuity_note") or "").strip(), limit=180)
        section_fallback = {
            "hero": "这一页会交代最重要的比较和结论，帮助读者知道后面的解释围绕什么展开。",
            "focus_stage": "这部分集中呈现当前页最关键的比较，后文会继续补足这些差异的含义。",
            "reading_flow": "正文把前面的比较展开成作者的判断，说明这些差异为什么足以支撑结论。",
            "explainer_cluster": "这里补的是会影响理解的术语和机制，让结果里的比较对象与指标变得更清楚。",
            "supporting_resources": "这里补的是理解当前页所需的背景，说明这些比较对象为何重要、各自代表什么。",
            "question_lab": "把刚读懂的内容变成几个追问，帮助你继续核对而不是再看一遍摘要。",
        }.get(section_type, "")
        if default_clean and (
            cls._looks_like_internal_planner_copy(default_clean)
            or cls._needs_display_localization(default_clean)
            or (section_type != "question_lab" and cls._is_low_signal_reader_excerpt(default_clean))
            or cls._looks_like_heading_only(default_clean)
            or (
                section_type == "question_lab"
                and (
                    not cls._is_natural_explanatory_reader_copy(default_clean, section_type=section_type, limit=limit)
                    or cls._is_generic_narrative_summary(default_clean)
                )
            )
        ):
            default_clean = ""
        if default_clean:
            return cls._clean_excerpt(default_clean, limit=limit)
        candidates = [
            reader_goal,
            continuity_note,
            f"{reader_goal} {continuity_note}".strip(),
            section_fallback,
        ]
        for candidate in candidates:
            normalized = cls._sanitize_reader_facing_text(candidate, limit=limit)
            normalized = cls._clean_excerpt(normalized, limit=limit) if normalized else ""
            if normalized and cls._looks_like_internal_planner_copy(normalized):
                continue
            if candidate == section_fallback and normalized and cls._looks_like_reader_instruction_copy(normalized, section_type=section_type):
                continue
            if normalized and (
                candidate == section_fallback
                or candidate in {reader_goal, continuity_note, f"{reader_goal} {continuity_note}".strip()}
                or cls._is_natural_explanatory_reader_copy(normalized, section_type=section_type, limit=limit)
            ):
                return normalized
        return cls._clean_excerpt(default_clean, limit=limit) if default_clean else ""

    @staticmethod
    def _compact_validation_errors(exc: ValidationError, *, limit: int = 5) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in list(exc.errors() or [])[:limit]:
            if not isinstance(item, Mapping):
                continue
            loc = item.get("loc")
            if isinstance(loc, tuple):
                loc_value = [str(part) for part in loc]
            elif isinstance(loc, list):
                loc_value = [str(part) for part in loc]
            else:
                loc_value = [str(loc)] if loc else []
            rows.append(
                {
                    "loc": loc_value,
                    "msg": str(item.get("msg") or "").strip(),
                    "type": str(item.get("type") or "").strip(),
                }
            )
        return rows

    @classmethod
    def _build_web_scrape_followup_request(
        cls,
        *,
        beat_id: str,
        search_data: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        structured = search_data.get("structured_content") if isinstance(search_data, Mapping) else None
        results = structured.get("results") if isinstance(structured, Mapping) else None
        if not isinstance(results, list):
            return None
        ranked: List[tuple[int, Dict[str, Any]]] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            href = str(item.get("url") or "").strip()
            if not href:
                continue
            score = cls._resource_domain_score(href)
            if score < 0 and not cls._low_value_domain_justification(item):
                continue
            ranked.append((score, dict(item)))
        if not ranked:
            return None
        ranked.sort(key=lambda row: row[0], reverse=True)
        score, best = ranked[0]
        href = str(best.get("url") or "").strip()
        if not href:
            return None
        if score < 20 and len(ranked) > 1:
            score, best = ranked[1]
            href = str(best.get("url") or "").strip()
        if not href:
            return None
        label = str(best.get("title") or href).strip()
        return {
            "tool": "web_scrape",
            "arguments": {
                "url": href,
                "formats": ["markdown"],
                "only_main_content": True,
            },
            "reason": f"Read {label[:120]} directly so the beat can use a reader-facing explanation instead of bare search snippets.",
            "priority": "medium",
            "beat_id": str(beat_id or "").strip(),
        }

    @classmethod
    def _extract_tool_result_source_meta(
        cls,
        *,
        tool_name: str,
        result: Any,
        arguments: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        data = getattr(result, "data", None)
        if tool_name == "paper_read":
            return {"source_kind": "paper_native_read", "source_url": "", "domain_score": 0}
        if tool_name == "knowledge_search":
            return {"source_kind": "reader_native_knowledge", "source_url": "", "domain_score": 0}
        if tool_name == "web_scrape":
            source_url = str((arguments or {}).get("url") or "").strip()
            if not source_url and isinstance(data, Mapping):
                links = list(data.get("public_links") or [])
                if links and isinstance(links[0], Mapping):
                    source_url = str(links[0].get("href") or "").strip()
            return {
                "source_kind": "public_web_page",
                "source_url": source_url,
                "domain_score": cls._resource_domain_score(source_url),
            }
        if tool_name == "web_search" and isinstance(data, Mapping):
            structured = data.get("structured_content")
            results = structured.get("results") if isinstance(structured, Mapping) else None
            if isinstance(results, list):
                ranked: List[tuple[int, str]] = []
                fallback_ranked: List[tuple[int, str]] = []
                for item in list(results or []):
                    if not isinstance(item, Mapping):
                        continue
                    source_url = str(item.get("url") or "").strip()
                    if not source_url:
                        continue
                    domain_score = cls._resource_domain_score(source_url)
                    fallback_ranked.append((domain_score, source_url))
                    snippet = cls._best_reader_facing_excerpt(
                        item.get("snippet") or item.get("summary") or "",
                        tool_name="web_search",
                        domain_score=domain_score,
                        limit=180,
                    )
                    if not snippet:
                        continue
                    score = cls._score_reader_facing_text(
                        snippet,
                        tool_name="web_search",
                        domain_score=domain_score,
                    )
                    ranked.append((score, source_url))
                if ranked:
                    ranked.sort(key=lambda row: row[0], reverse=True)
                    source_url = ranked[0][1]
                    return {
                        "source_kind": "public_web_search",
                        "source_url": source_url,
                        "domain_score": cls._resource_domain_score(source_url),
                    }
                if fallback_ranked:
                    fallback_ranked.sort(key=lambda row: row[0], reverse=True)
                    source_url = fallback_ranked[0][1]
                    return {
                        "source_kind": "public_web_search",
                        "source_url": source_url,
                        "domain_score": cls._resource_domain_score(source_url),
                    }
        return {"source_kind": "", "source_url": "", "domain_score": 0}

    @classmethod
    def _extract_tool_output_excerpt(
        cls,
        *,
        tool_name: str,
        result: Any,
    ) -> str:
        data = getattr(result, "data", None)
        if tool_name == "paper_read" and isinstance(data, Mapping):
            quality_label = str(data.get("quality") or "").strip().lower()
            ranked: List[tuple[int, str]] = []
            for item in list(data.get("results") or [])[:3]:
                if not isinstance(item, Mapping):
                    continue
                candidate = cls._best_reader_facing_excerpt(
                    item.get("content") or item.get("snippet") or "",
                    tool_name="paper_read",
                    limit=220,
                )
                if not candidate:
                    continue
                score = cls._score_reader_facing_text(candidate, tool_name="paper_read")
                if quality_label == "low":
                    score -= 18
                try:
                    score += int(float(item.get("score") or 0) * 20)
                except Exception:  # pragma: no cover - defensive
                    pass
                ranked.append((score, candidate))
            if ranked:
                ranked.sort(key=lambda row: row[0], reverse=True)
                return cls._clean_excerpt(ranked[0][1], limit=220)
        if tool_name == "web_search" and isinstance(data, Mapping):
            structured = data.get("structured_content")
            if isinstance(structured, Mapping) and isinstance(structured.get("results"), list):
                ranked: List[tuple[int, str]] = []
                for item in list(structured.get("results") or []):
                    if not isinstance(item, Mapping):
                        continue
                    href = str(item.get("url") or "").strip()
                    domain_score = cls._resource_domain_score(href)
                    snippet = cls._best_reader_facing_excerpt(
                        item.get("snippet") or item.get("summary") or "",
                        tool_name="web_search",
                        domain_score=domain_score,
                        limit=180,
                    )
                    title = cls._sanitize_reader_facing_text(item.get("title"), limit=90)
                    if snippet and len(snippet) < 48 and title and title.lower() not in snippet.lower() and not cls._looks_like_heading_only(title):
                        snippet = cls._clean_excerpt(f"{title}: {snippet}", limit=180)
                    if not snippet:
                        continue
                    score = cls._score_reader_facing_text(
                        snippet,
                        tool_name="web_search",
                        domain_score=domain_score,
                    )
                    ranked.append((score, snippet))
                    if len(ranked) >= 5:
                        break
                if ranked:
                    ranked.sort(key=lambda row: row[0], reverse=True)
                    return cls._clean_excerpt(ranked[0][1], limit=220)
        return cls._best_reader_facing_excerpt(
            getattr(result, "output", "") or "",
            tool_name=tool_name,
            limit=220,
        )

    @staticmethod
    def _classify_tool_budget_bucket(tool_name: str) -> str:
        normalized = str(tool_name or "").strip()
        if normalized in {"paper_read", "knowledge_search"}:
            return "reader_native"
        if normalized in {"web_search", "web_scrape"}:
            return "public_web"
        return "other"

    @staticmethod
    def _extract_tool_request_identity(tool_name: str, arguments: Mapping[str, Any], *, policy: str) -> str:
        normalized_tool = str(tool_name or "").strip()
        if str(policy or "").strip() != "exact_query_text":
            return f"{normalized_tool}:{json.dumps(dict(arguments or {}), sort_keys=True, ensure_ascii=False)}"
        query_token = ""
        if normalized_tool in {"paper_read", "knowledge_search", "web_search"}:
            query_token = str(arguments.get("query") or "").strip().lower()
        elif normalized_tool == "web_scrape":
            query_token = str(arguments.get("url") or "").strip().lower()
        if not query_token:
            return f"{normalized_tool}:{json.dumps(dict(arguments or {}), sort_keys=True, ensure_ascii=False)}"
        bucket = "query" if normalized_tool != "web_scrape" else "url"
        return f"{bucket}:{query_token}"

    def _build_planning_brief(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        page_dossier: Optional[Mapping[str, Any]] = None,
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
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
        dossier = dict(page_dossier or {})
        current_page = dict(dossier.get("current_page") or {})
        adjacent_rows = [
            dict(item)
            for item in list(adjacent_page_context or dossier.get("adjacent_page_context") or [])
            if isinstance(item, Mapping)
        ]
        target_rows = [
            dict(item)
            for item in list(current_page.get("targets") or enrichment_bundle.get("targets") or [])
            if isinstance(item, Mapping)
        ]
        page_dossier_topics = self._derive_page_dossier_topics(
            current_page=current_page,
            target_rows=target_rows,
        )
        target_kind_counts: Dict[str, int] = {}
        for row in target_rows:
            kind = str(row.get("kind") or row.get("target_kind") or "").strip() or "unknown"
            target_kind_counts[kind] = int(target_kind_counts.get(kind, 0)) + 1

        focus_target_id = str(page_brief.get("primary_focus_target_id") or "").strip()
        focus_target = next(
            (
                dict(row)
                for row in target_rows
                if str(row.get("target_id") or "").strip() == focus_target_id
            ),
            {},
        )
        focus_label = str(
            focus_target.get("title")
            or focus_target.get("figure_label")
            or focus_target.get("section_label")
            or ""
        ).strip()
        paper_title = str(dossier.get("paper_title") or current_page.get("paper_title") or "").strip()
        page_topic_context_parts = [
            str(page_brief.get("page_goal") or "").strip(),
            str(page_brief.get("hero_angle") or "").strip(),
            str(focus_target.get("summary") or focus_target.get("excerpt") or "").strip(),
        ]
        for row in target_rows:
            section_label = str(row.get("section_label") or "").strip()
            summary = str(row.get("summary") or row.get("excerpt") or "").strip()
            if section_label:
                page_topic_context_parts.append(section_label)
            if summary:
                page_topic_context_parts.append(summary)
            if len(page_topic_context_parts) >= 6:
                break
        page_topic_context = self._clean_excerpt(
            " ".join(part for part in page_topic_context_parts if part),
            limit=220,
        )
        continuation_hints = self._dedupe_strings(
            [
                str(hint or "").strip()
                for row in adjacent_rows
                for hint in list(row.get("continuation_hints") or [])
                if str(hint or "").strip()
            ],
            limit=6,
        )
        continuity_mode = "standalone"
        if adjacent_rows and continuation_hints:
            continuity_mode = "bridged_sequence"
        elif adjacent_rows:
            continuity_mode = "light_reference"

        adjacent_summary = self._dedupe_strings(
            [
                self._clean_excerpt(str(row.get("summary") or "").strip(), limit=180)
                for row in adjacent_rows
                if str(row.get("summary") or "").strip()
            ],
            limit=2,
        )
        resource_gap_topics = [
            str(item or "").strip()
            for item in list(page_brief.get("resource_gaps") or [])
            if str(item or "").strip()
        ]
        recommended_sections = [
            str(row.get("section_type") or "").strip()
            for row in list(page_brief.get("storyboard") or [])
            if isinstance(row, Mapping) and str(row.get("section_type") or "").strip()
        ]
        tool_hints: List[str] = ["paper_read"]
        if resource_gap_topics:
            tool_hints.append("knowledge_search")
            tool_hints.append("web_search")
        if any(list(row.get("figures") or []) or list(row.get("tables") or []) for row in adjacent_rows):
            tool_hints.append("web_scrape")
        tool_hints = self._dedupe_strings(tool_hints)
        planner_notes = self._dedupe_strings(
            [
                f"Use {focus_label} as the main narrative anchor." if focus_label else "",
                "Neighboring pages should shape continuity, not sit as passive debug metadata." if adjacent_rows else "",
                "Tool use should fill concrete understanding gaps, not just decorate the page.",
                "The final page should feel like a durable reading webpage, not a short artifact that drops the page body.",
            ]
            + [str(item or "").strip() for item in continuation_hints],
            limit=6,
        )
        planning_brief_seed = {
            "tool_hints": tool_hints,
            "resource_gap_topics": resource_gap_topics,
        }
        tool_budget = self._build_tool_budget(
            planning_brief_seed=planning_brief_seed,
            adjacent_page_context=adjacent_rows,
        )
        summary = page_brief.get("page_goal") or page_brief.get("hero_angle") or "Build a dossier-driven experience page."
        return {
            "version": "v1",
            "focus_page": int(page),
            "user_intent": str(user_intent or "").strip(),
            "page_archetype_hint": str(page_brief.get("page_archetype") or "").strip(),
            "paper_title": paper_title,
            "primary_focus_target_id": focus_target_id,
            "primary_focus_label": focus_label,
            "page_topic_context": page_topic_context,
            "page_dossier_topics": page_dossier_topics,
            "reader_goal": str(page_brief.get("page_goal") or "").strip(),
            "hero_angle_hint": str(page_brief.get("hero_angle") or "").strip(),
            "continuity_mode": continuity_mode,
            "continuity_summary": adjacent_summary,
            "continuation_hints": continuation_hints,
            "resource_gap_topics": resource_gap_topics,
            "tool_hints": tool_hints,
            "tool_budget": tool_budget,
            "recommended_sections": recommended_sections,
            "guided_beat_seed": [dict(row) for row in list(page_brief.get("storyboard") or []) if isinstance(row, Mapping)],
            "target_kind_counts": target_kind_counts,
            "planner_notes": planner_notes,
            "summary": self._clean_excerpt(str(summary), limit=220),
        }

    @staticmethod
    def _query_fragment(value: Any, *, limit: int = 80) -> str:
        token = re.sub(r"\s+", " ", str(value or "").strip())
        if not token:
            return ""
        token = re.sub(r"[\[\]{}|]+", " ", token)
        token = re.sub(r"\s+", " ", token).strip(" -:;,")
        if len(token) > limit:
            token = token[:limit].rsplit(" ", 1)[0].strip() or token[:limit].strip()
        return token

    @classmethod
    def _compose_search_query(
        cls,
        *,
        parts: Sequence[str],
        suffix: str = "",
        limit: int = 180,
    ) -> str:
        ordered: List[str] = []
        seen: set[str] = set()
        for raw in list(parts or []):
            token = cls._query_fragment(raw, limit=100)
            if not token:
                continue
            identity = token.lower()
            if identity in seen:
                continue
            seen.add(identity)
            ordered.append(token)
        if suffix:
            ordered.append(suffix.strip())
        query = re.sub(r"\s+", " ", " ".join(item for item in ordered if item)).strip()
        if len(query) > limit:
            query = query[:limit].rsplit(" ", 1)[0].strip() or query[:limit].strip()
        return query

    @classmethod
    def _derive_page_dossier_topics(
        cls,
        *,
        current_page: Mapping[str, Any],
        target_rows: Sequence[Mapping[str, Any]],
    ) -> List[str]:
        topics: List[str] = []

        def append_topic(raw: Any, *, limit: int = 90) -> None:
            token = cls._query_fragment(raw, limit=limit)
            if token:
                topics.append(token)

        raw_topics = current_page.get("topics")
        if isinstance(raw_topics, Sequence) and not isinstance(raw_topics, (str, bytes)):
            for item in list(raw_topics)[:6]:
                append_topic(item, limit=80)
        append_topic(current_page.get("summary"), limit=120)
        for row in list(target_rows or []):
            if not isinstance(row, Mapping):
                continue
            append_topic(row.get("figure_label"), limit=60)
            append_topic(row.get("title"), limit=80)
            append_topic(row.get("section_label"), limit=60)
            append_topic(row.get("summary") or row.get("excerpt"), limit=120)
            if len(topics) >= 10:
                break
        return cls._dedupe_strings(topics, limit=6)

    def _derive_beat_request_context(
        self,
        *,
        beat: Mapping[str, Any],
        planning_brief: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, str]:
        target_lookup = {
            str(row.get("target_id") or "").strip(): dict(row)
            for row in list(enrichment_bundle.get("targets") or [])
            if isinstance(row, Mapping) and str(row.get("target_id") or "").strip()
        }
        beat_target_ids = [
            str(item or "").strip()
            for item in list(beat.get("target_ids") or [])
            if str(item or "").strip()
        ]
        beat_targets = [dict(target_lookup.get(target_id) or {}) for target_id in beat_target_ids if dict(target_lookup.get(target_id) or {})]
        focus_label = str(planning_brief.get("primary_focus_label") or "").strip()
        paper_title = str(planning_brief.get("paper_title") or "").strip()
        page_topic_context = str(planning_brief.get("page_topic_context") or planning_brief.get("summary") or "").strip()
        beat_title = str(beat.get("title") or "").strip()
        section_label = next((str(row.get("section_label") or "").strip() for row in beat_targets if str(row.get("section_label") or "").strip()), "")
        figure_label = next(
            (
                str(row.get("figure_label") or row.get("title") or "").strip()
                for row in beat_targets
                if str(row.get("target_kind") or row.get("kind") or "").strip() == "figure"
                and str(row.get("figure_label") or row.get("title") or "").strip()
            ),
            "",
        )
        caption_or_summary = next(
            (
                str(row.get("summary") or row.get("excerpt") or "").strip()
                for row in beat_targets
                if str(row.get("summary") or row.get("excerpt") or "").strip()
            ),
            "",
        )
        return {
            "paper_title": paper_title,
            "focus_label": focus_label,
            "page_topic_context": page_topic_context,
            "beat_title": beat_title,
            "section_label": section_label,
            "figure_label": figure_label,
            "caption_or_summary": caption_or_summary,
        }

    @staticmethod
    def _supports_staged_runtime(*, llm: Any, registry: Any) -> bool:
        return bool(callable(getattr(llm, "chat", None)) and callable(getattr(registry, "execute", None)))

    @staticmethod
    def _coerce_mapping(value: Any) -> Dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        return {}

    def _normalize_tool_request_arguments(
        self,
        *,
        tool_name: str,
        raw_arguments: Any,
    ) -> Dict[str, Any]:
        raw = self._coerce_mapping(raw_arguments)
        tool = str(tool_name or "").strip()
        if tool == "paper_read":
            query = str(raw.get("query") or "").strip()
            if not query:
                return {}
            return {
                "query": query,
                "top_k": self._coerce_budget_value(raw.get("top_k"), 5, minimum=1, maximum=8),
            }
        if tool == "knowledge_search":
            query = str(raw.get("query") or "").strip()
            if not query:
                return {}
            return {
                "query": query,
                "top_k": self._coerce_budget_value(raw.get("top_k"), 5, minimum=1, maximum=8),
                "include_adjacent_chunks": bool(raw.get("include_adjacent_chunks", True)),
                "adjacent_window": self._coerce_budget_value(raw.get("adjacent_window"), 1, minimum=0, maximum=3),
            }
        if tool == "web_search":
            query = str(raw.get("query") or "").strip()
            if not query:
                return {}
            return {
                "query": query,
                "max_results": self._coerce_budget_value(raw.get("max_results"), 5, minimum=1, maximum=8),
            }
        if tool == "web_scrape":
            url = str(raw.get("url") or raw.get("href") or "").strip()
            if not url:
                return {}
            formats = raw.get("formats")
            normalized_formats = []
            if isinstance(formats, Sequence) and not isinstance(formats, (str, bytes)):
                normalized_formats = [str(item).strip() for item in list(formats) if str(item).strip()]
            return {
                "url": url,
                "formats": normalized_formats or ["markdown"],
                "only_main_content": bool(raw.get("only_main_content", True)),
            }
        return {}

    def _derive_default_planner_tool_requests(
        self,
        *,
        planning_brief: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
        allowed_tools: Sequence[str],
    ) -> List[Dict[str, Any]]:
        allowed = {str(item or "").strip() for item in list(allowed_tools or []) if str(item or "").strip()}
        tool_budget = dict(planning_brief.get("tool_budget") or {})
        hints = self._derive_reader_grounding_hints(enrichment_bundle, limit=4)
        focus_label = str(planning_brief.get("primary_focus_label") or "").strip()
        paper_title = str(planning_brief.get("paper_title") or "").strip()
        page_topic_context = str(planning_brief.get("page_topic_context") or "").strip()
        page_dossier_topics = [
            str(item or "").strip()
            for item in list(planning_brief.get("page_dossier_topics") or [])
            if str(item or "").strip()
        ]
        primary_focus_context = self._derive_beat_request_context(
            beat={"target_ids": [str(planning_brief.get("primary_focus_target_id") or "").strip()]},
            planning_brief=planning_brief,
            enrichment_bundle=enrichment_bundle,
        )
        gap_topics = [str(item or "").strip() for item in list(planning_brief.get("resource_gap_topics") or []) if str(item or "").strip()]
        beat_seed = [
            dict(row)
            for row in list(planning_brief.get("guided_beat_seed") or [])
            if isinstance(row, Mapping)
        ]

        def find_beat_id(objectives: Sequence[str], fallback_sections: Sequence[str] = ()) -> str:
            objective_set = {str(item or "").strip() for item in objectives if str(item or "").strip()}
            fallback_section_set = {str(item or "").strip() for item in fallback_sections if str(item or "").strip()}
            for beat in beat_seed:
                beat_id = str(beat.get("beat_id") or "").strip()
                if not beat_id:
                    continue
                beat_objectives = {str(item or "").strip() for item in list(beat.get("tool_objectives") or []) if str(item or "").strip()}
                beat_section = str(beat.get("section_type") or "").strip()
                if objective_set and beat_objectives.intersection(objective_set):
                    return beat_id
                if fallback_section_set and beat_section in fallback_section_set:
                    return beat_id
            return ""

        requests: List[Dict[str, Any]] = []
        seen_queries: set[tuple[str, str]] = set()

        def query_title_for_beat(beat: Mapping[str, Any]) -> str:
            title = str(beat.get("title") or "").strip()
            section_type = str(beat.get("section_type") or "").strip()
            if section_type == "focus_stage" and title in {"图里的关键比较", "最关键的证据"}:
                return "拆解这张图"
            if section_type == "reading_flow" and title in {"再看正文怎么解释这些差异", "正文如何展开这些结果"}:
                return "完整阅读本页内容"
            if section_type == "supporting_resources" and title in {"理解结果需要的背景", "理解这一页需要的背景"}:
                return "补充背景与上下文"
            return title

        def push_request(tool: str, arguments: Mapping[str, Any], reason: str, priority: str, beat_id: str) -> None:
            query_key = str(arguments.get("query") or arguments.get("url") or "").strip().lower()
            identity = (tool, query_key)
            if query_key and identity in seen_queries:
                return
            if query_key:
                seen_queries.add(identity)
            requests.append(
                {
                    "tool": tool,
                    "arguments": dict(arguments),
                    "reason": reason,
                    "priority": priority,
                    "beat_id": beat_id,
                }
            )

        if "paper_read" in allowed and hints and self._coerce_budget_value(tool_budget.get("max_reader_native_requests"), 2, minimum=0, maximum=4) > 0:
            paper_read_hint = hints[0]
            paper_read_parts = [
                primary_focus_context.get("paper_title") or paper_title,
                primary_focus_context.get("beat_title") or focus_label,
                primary_focus_context.get("figure_label"),
                primary_focus_context.get("caption_or_summary") or page_topic_context,
                paper_read_hint.get("paper_read_query") or "",
            ]
            paper_read_query = self._compose_search_query(
                parts=paper_read_parts,
                suffix="paper grounding",
            )
            if paper_read_query:
                push_request(
                    "paper_read",
                    {"query": paper_read_query, "top_k": 5},
                    "Ground the current page in the paper's own wording before expanding outward.",
                    "high",
                    find_beat_id(["continuation_bridge"], ["reading_flow", "focus_stage"]),
                )
        if (
            "knowledge_search" in allowed
            and (gap_topics or hints)
            and self._coerce_budget_value(tool_budget.get("max_reader_native_requests"), 2, minimum=0, maximum=4) > 1
        ):
            query = gap_topics[0] if gap_topics else str(hints[0].get("knowledge_search_query") or "").strip()
            if focus_label:
                query = f"{focus_label} {query}".strip()
            push_request(
                "knowledge_search",
                {
                    "query": query,
                    "top_k": 5,
                    "include_adjacent_chunks": True,
                    "adjacent_window": 1,
                },
                "Fill page-level background gaps with nearby knowledge-base evidence.",
                "medium",
                find_beat_id(["term_explain", "method_background"], ["explainer_cluster"]),
            )
        max_public_web_requests = self._coerce_budget_value(tool_budget.get("max_public_web_requests"), 1, minimum=0, maximum=6)
        if "web_search" in allowed and max_public_web_requests > 0:
            web_requests: List[Dict[str, Any]] = []
            if gap_topics:
                query = self._compose_search_query(
                    parts=[
                        primary_focus_context.get("paper_title") or paper_title,
                        gap_topics[0],
                        primary_focus_context.get("figure_label") or focus_label,
                        primary_focus_context.get("caption_or_summary") or page_topic_context,
                        *page_dossier_topics[:2],
                    ],
                    suffix="official context",
                )
                web_requests.append(
                    {
                        "query": query,
                        "max_results": 5,
                        "reason": "Attach authoritative public resources that materially improve comprehension for this page.",
                        "priority": "medium",
                        "beat_id": find_beat_id(["why_it_matters", "external_comparison"], ["supporting_resources"]),
                    }
                )
            if focus_label:
                query = self._compose_search_query(
                    parts=[
                        primary_focus_context.get("paper_title") or paper_title,
                        primary_focus_context.get("figure_label") or focus_label,
                        primary_focus_context.get("caption_or_summary") or page_topic_context,
                        *page_dossier_topics[:2],
                    ],
                    suffix="official explanation",
                )
                web_requests.append(
                    {
                        "query": query,
                        "max_results": 5,
                        "reason": "Find public explanation or authoritative framing for the current page's main concept or figure.",
                        "priority": "medium",
                        "beat_id": find_beat_id(["figure_context", "why_it_matters"], ["focus_stage", "supporting_resources"]),
                    }
                )
            for beat in beat_seed:
                beat_id = str(beat.get("beat_id") or "").strip()
                objectives = {
                    str(item or "").strip()
                    for item in list(beat.get("tool_objectives") or [])
                    if str(item or "").strip()
                }
                if not objectives:
                    continue
                beat_title = query_title_for_beat(beat)
                beat_context = self._derive_beat_request_context(
                    beat=beat,
                    planning_brief=planning_brief,
                    enrichment_bundle=enrichment_bundle,
                )
                contextual_topics = self._dedupe_strings(
                    [
                        str(beat_context.get("section_label") or "").strip(),
                        str(beat_context.get("page_topic_context") or "").strip(),
                        *page_dossier_topics[:3],
                    ],
                    limit=4,
                )
                if "figure_context" in objectives and focus_label:
                    query = self._compose_search_query(
                        parts=[
                            beat_context.get("paper_title") or paper_title,
                            beat_title,
                            beat_context.get("figure_label") or focus_label,
                            beat_context.get("caption_or_summary") or page_topic_context,
                            *contextual_topics,
                        ],
                        suffix="figure explanation",
                    )
                    web_requests.append(
                        {
                            "query": query,
                            "max_results": 4,
                            "reason": "Bring in a public explanation that helps decode the figure for non-specialist readers.",
                            "priority": "medium",
                            "beat_id": beat_id,
                        }
                    )
                if objectives.intersection({"term_explain", "method_background"}):
                    context_query = self._compose_search_query(
                        parts=[
                            beat_context.get("paper_title") or paper_title,
                            beat_title,
                            beat_context.get("section_label"),
                            gap_topics[0] if gap_topics else focus_label,
                            *contextual_topics,
                        ],
                        suffix="tutorial explanation",
                    )
                    if context_query:
                        web_requests.append(
                            {
                                "query": context_query,
                                "max_results": 4,
                                "reason": "Fill the method or term background this beat needs in reader-facing language.",
                                "priority": "medium",
                                "beat_id": beat_id,
                            }
                        )
                if objectives.intersection({"why_it_matters", "external_comparison"}):
                    context_query = self._compose_search_query(
                        parts=[
                            beat_context.get("paper_title") or paper_title,
                            beat_title,
                            beat_context.get("figure_label") or focus_label,
                            gap_topics[0] if gap_topics else "",
                            beat_context.get("caption_or_summary") or page_topic_context,
                            *contextual_topics,
                        ],
                        suffix="official overview comparison",
                    )
                    if context_query:
                        web_requests.append(
                            {
                                "query": context_query,
                                "max_results": 4,
                                "reason": "Bring in authoritative context that explains why this beat matters beyond the paper itself.",
                                "priority": "medium",
                                "beat_id": beat_id,
                            }
                        )
                if objectives.intersection({"continuation_bridge"}) and focus_label:
                    query = self._compose_search_query(
                        parts=[
                            beat_context.get("paper_title") or paper_title,
                            beat_title,
                            beat_context.get("figure_label") or focus_label,
                            beat_context.get("caption_or_summary") or page_topic_context,
                            *contextual_topics,
                        ],
                        suffix="background overview",
                    )
                    web_requests.append(
                        {
                            "query": query,
                            "max_results": 3,
                            "reason": "Add one concise public bridge that helps the reader move through this page without losing context.",
                            "priority": "low",
                            "beat_id": beat_id,
                        }
                    )
            for row in web_requests:
                if len([item for item in requests if str(item.get('tool') or '').strip() == 'web_search']) >= max_public_web_requests:
                    break
                push_request(
                    "web_search",
                    {"query": str(row.get("query") or "").strip(), "max_results": int(row.get("max_results") or 4)},
                    str(row.get("reason") or "").strip(),
                    str(row.get("priority") or "medium").strip() or "medium",
                    str(row.get("beat_id") or "").strip(),
                )
        max_tool_requests = self._coerce_budget_value(tool_budget.get("max_tool_requests"), 4, minimum=1, maximum=6)
        return requests[:max_tool_requests]

    def _normalize_planner_guided_beats(
        self,
        *,
        raw_guided_beats: Optional[Sequence[Mapping[str, Any]]],
        planning_brief: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        seed_rows = [
            dict(row)
            for row in list(planning_brief.get("guided_beat_seed") or [])
            if isinstance(row, Mapping)
        ]
        current_rows = [dict(row) for row in list(raw_guided_beats or []) if isinstance(row, Mapping)]
        beat_rows = current_rows or seed_rows
        normalized: List[Dict[str, Any]] = []
        for index, row in enumerate(beat_rows, start=1):
            beat_id = str(row.get("beat_id") or f"beat_{index}").strip() or f"beat_{index}"
            normalized.append(
                {
                    "beat_id": beat_id,
                    "role": str(row.get("role") or row.get("section_type") or "").strip(),
                    "section_type": str(row.get("section_type") or "").strip(),
                    "title": str(row.get("title") or "").strip(),
                    "purpose": str(row.get("purpose") or "").strip(),
                    "reader_goal": str(row.get("reader_goal") or "").strip(),
                    "continuity_note": str(row.get("continuity_note") or "").strip(),
                    "target_ids": self._dedupe_strings([str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()]),
                    "tool_objectives": self._dedupe_strings([str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()]),
                    "block_stack": self._dedupe_strings([str(item).strip() for item in list(row.get("block_stack") or []) if str(item).strip()]),
                    "drop_notes": self._dedupe_strings([str(item).strip() for item in list(row.get("drop_notes") or []) if str(item).strip()], limit=6),
                    "priority": max(1, int(row.get("priority") or index)),
                    "meta": dict(row.get("meta") or {}),
                }
            )
        return normalized

    def _normalize_planner_output(
        self,
        *,
        raw: Optional[Mapping[str, Any]],
        planning_brief: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
        allowed_tools: Sequence[str],
    ) -> Dict[str, Any]:
        allowed = {str(item or "").strip() for item in list(allowed_tools or []) if str(item or "").strip()}
        payload = dict(raw or {})
        tool_budget = dict(planning_brief.get("tool_budget") or {})
        max_tool_requests = self._coerce_budget_value(tool_budget.get("max_tool_requests"), 4, minimum=1, maximum=10)
        max_reader_native_requests = self._coerce_budget_value(tool_budget.get("max_reader_native_requests"), 2, minimum=0, maximum=4)
        max_public_web_requests = self._coerce_budget_value(tool_budget.get("max_public_web_requests"), 1, minimum=0, maximum=6)
        allow_web_scrape = bool(tool_budget.get("allow_web_scrape", True))
        duplicate_query_policy = str(tool_budget.get("duplicate_query_policy") or "exact_query_text").strip() or "exact_query_text"
        section_strategy = self._dedupe_strings(
            [
                str(item or "").strip()
                for item in list(payload.get("section_strategy") or payload.get("recommended_sections") or [])
                if str(item or "").strip() in self.EXPERIENCE_SECTION_TYPES
            ]
        ) or [str(item or "").strip() for item in list(planning_brief.get("recommended_sections") or []) if str(item or "").strip()]

        normalized_requests: List[Dict[str, Any]] = []
        native_requests = 0
        public_requests = 0
        seen_request_keys: set[str] = set()
        for row in list(payload.get("tool_requests") or []):
            if not isinstance(row, Mapping):
                continue
            tool_name = str(row.get("tool") or "").strip()
            if tool_name not in allowed:
                continue
            if tool_name == "web_scrape" and not allow_web_scrape:
                continue
            arguments = self._normalize_tool_request_arguments(
                tool_name=tool_name,
                raw_arguments=row.get("arguments") or {},
            )
            if not arguments:
                continue
            request_bucket = self._classify_tool_budget_bucket(tool_name)
            effective_max_tool_requests = max(1, max_tool_requests - 1) if allow_web_scrape else max_tool_requests
            effective_max_public_web_requests = (
                max(1, max_public_web_requests - 1)
                if allow_web_scrape and max_public_web_requests > 0
                else max_public_web_requests
            )
            if len(normalized_requests) >= effective_max_tool_requests:
                break
            if request_bucket == "reader_native" and native_requests >= max_reader_native_requests:
                continue
            if request_bucket == "public_web" and public_requests >= effective_max_public_web_requests:
                continue
            request_identity = self._extract_tool_request_identity(tool_name, arguments, policy=duplicate_query_policy)
            if request_identity in seen_request_keys:
                continue
            seen_request_keys.add(request_identity)
            normalized_requests.append(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "reason": self._clean_excerpt(str(row.get("reason") or "").strip(), limit=180),
                    "priority": str(row.get("priority") or "medium").strip() or "medium",
                }
            )
            if request_bucket == "reader_native":
                native_requests += 1
            elif request_bucket == "public_web":
                public_requests += 1
        if not normalized_requests:
            normalized_requests = self._derive_default_planner_tool_requests(
                planning_brief=planning_brief,
                enrichment_bundle=enrichment_bundle,
                allowed_tools=sorted(allowed),
            )

        resource_objectives = self._dedupe_strings(
            [str(item or "").strip() for item in list(payload.get("resource_objectives") or []) if str(item or "").strip()],
            limit=4,
        ) or [str(item or "").strip() for item in list(planning_brief.get("resource_gap_topics") or []) if str(item or "").strip()]
        page_generation_notes = self._dedupe_strings(
            [str(item or "").strip() for item in list(payload.get("page_generation_notes") or []) if str(item or "").strip()],
            limit=6,
        ) or [str(item or "").strip() for item in list(planning_brief.get("planner_notes") or []) if str(item or "").strip()]
        guided_beats = self._normalize_planner_guided_beats(
            raw_guided_beats=payload.get("guided_beats") if isinstance(payload.get("guided_beats"), Sequence) and not isinstance(payload.get("guided_beats"), (str, bytes)) else None,
            planning_brief=planning_brief,
        )
        valid_beat_ids = {str(row.get("beat_id") or "").strip() for row in guided_beats if str(row.get("beat_id") or "").strip()}

        def infer_request_beat_id(tool_name: str, raw_row: Mapping[str, Any]) -> str:
            explicit = str(raw_row.get("beat_id") or "").strip()
            if explicit in valid_beat_ids:
                return explicit
            objective_preferences: List[str]
            if tool_name == "paper_read":
                objective_preferences = ["figure_context", "continuation_bridge"]
            elif tool_name == "knowledge_search":
                objective_preferences = ["term_explain", "method_background", "continuation_bridge"]
            else:
                objective_preferences = ["why_it_matters", "external_comparison"]
            for objective in objective_preferences:
                for beat in guided_beats:
                    beat_id = str(beat.get("beat_id") or "").strip()
                    beat_objectives = {str(item).strip() for item in list(beat.get("tool_objectives") or []) if str(item).strip()}
                    if beat_id and objective in beat_objectives:
                        return beat_id
            return ""

        return {
            "version": "v1",
            "page_objective": self._clean_excerpt(
                str(payload.get("page_objective") or planning_brief.get("reader_goal") or planning_brief.get("summary") or "").strip(),
                limit=220,
            ),
            "narrative_strategy": self._clean_excerpt(
                str(payload.get("narrative_strategy") or planning_brief.get("hero_angle_hint") or planning_brief.get("summary") or "").strip(),
                limit=240,
            ),
            "section_strategy": section_strategy,
            "guided_beats": guided_beats,
            "tool_requests": [
                {
                    **dict(row),
                    "beat_id": infer_request_beat_id(str(row.get("tool") or "").strip(), row),
                }
                for row in normalized_requests[:max_tool_requests]
            ],
            "tool_budget": tool_budget,
            "resource_objectives": resource_objectives,
            "widget_focus": self._clean_excerpt(
                str(payload.get("widget_focus") or planning_brief.get("primary_focus_label") or "").strip(),
                limit=140,
            ),
            "page_generation_notes": page_generation_notes,
        }

    def _build_planner_prompt(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
        page_dossier: Optional[Mapping[str, Any]] = None,
        planning_brief: Optional[Mapping[str, Any]] = None,
        allowed_tools: Sequence[str],
    ) -> str:
        compact_targets = self._compact_enrichment_targets(enrichment_bundle, limit=18)
        adjacent_refs = [
            {
                "page": int(item.get("page") or 0),
                "relation": str(item.get("relation") or "").strip(),
                "summary": self._clean_excerpt(str(item.get("summary") or "").strip(), limit=280),
                "figures": [
                    {
                        "label": str(row.get("label") or "").strip(),
                        "description": self._clean_excerpt(str(row.get("description") or "").strip(), limit=180),
                    }
                    for row in list(item.get("figures") or [])
                    if isinstance(row, Mapping)
                ][:3],
                "tables": [
                    {
                        "label": str(row.get("label") or "").strip(),
                        "description": self._clean_excerpt(str(row.get("description") or "").strip(), limit=180),
                    }
                    for row in list(item.get("tables") or [])
                    if isinstance(row, Mapping)
                ][:3],
                "equations": [
                    {
                        "label": str(row.get("label") or "").strip(),
                        "description": self._clean_excerpt(str(row.get("description") or "").strip(), limit=180),
                    }
                    for row in list(item.get("equations") or [])
                    if isinstance(row, Mapping)
                ][:3],
                "continuation_hints": [
                    self._clean_excerpt(str(row or "").strip(), limit=140)
                    for row in list(item.get("continuation_hints") or [])
                    if str(row or "").strip()
                ][:4],
            }
            for item in list(adjacent_page_context or [])
            if isinstance(item, Mapping)
        ]
        output_schema = {
            "version": "v1",
            "page_objective": "Turn the page into a rich reading webpage while preserving the current-page body flow.",
            "narrative_strategy": "Preserve the current-page reading flow as the backbone, then use the strongest figure and supporting context to enrich comprehension.",
            "section_strategy": ["hero", "focus_stage", "reading_flow", "explainer_cluster", "supporting_resources", "question_lab"],
            "guided_beats": [
                {
                    "beat_id": "beat_focus",
                    "role": "focus_evidence",
                    "section_type": "focus_stage",
                    "title": "最关键的证据",
                    "purpose": "Use the figure as the page anchor.",
                    "reader_goal": "The central figure carries the page's most important comparison.",
                    "continuity_note": "The body text should explain why that comparison matters.",
                    "target_ids": ["p7:figure_1"],
                    "tool_objectives": ["figure_context"],
                    "block_stack": ["figure_walkthrough"],
                    "drop_notes": [],
                    "priority": 2
                }
            ],
            "tool_requests": [
                {
                    "beat_id": "beat_focus",
                    "tool": "paper_read",
                    "arguments": {"query": "Fig 3 concordance and insight", "top_k": 5},
                    "reason": "Ground the page in the paper's own evidence.",
                    "priority": "high",
                }
            ],
            "resource_objectives": ["USMLE structure", "evaluation metrics"],
            "widget_focus": "Fig 3",
            "page_generation_notes": [
                "The final page should feel like a durable reading artifact.",
                "Use adjacent-page continuity when it improves comprehension.",
            ],
        }
        tool_budget = dict((planning_brief or {}).get("tool_budget") or {})
        return (
            "You are the planner stage for a generative reading experience.\n"
            "Return JSON only.\n"
            "Your job is to decide what kind of webpage should be built, which sections matter, and which small set of tools should run before page generation.\n"
            "Rules:\n"
            "1) Preserve the current-page body flow as the backbone of the webpage; do not compress away the main reading content.\n"
            "2) Use adjacent-page context aggressively when it improves continuity.\n"
            "3) Use reader-native grounding for anchor facts, then add public-web/MCP resources whenever they make the beat more teachable.\n"
            "4) Respect tool_budget exactly: do not exceed max_tool_requests, reader-native caps, or public-web caps.\n"
            "5) Every tool request must have concrete arguments.\n"
            "6) The page should become a rich reading webpage, not just a pile of support cards.\n"
            "7) Use only allowed tools.\n"
            "8) Avoid duplicate queries when the same intent can be satisfied by one request.\n"
            "9) User-facing intent may be in Chinese, but planner JSON may stay concise and implementation-oriented.\n"
            "10) guided_beats should refine the planning_brief seed rather than replace it with an unrelated structure.\n"
            "11) When a tool request supports a specific beat, attach beat_id so the runtime can map enrichment back to that guided reading segment.\n"
            f"page={int(page)}\n"
            f"user_intent={json.dumps(str(user_intent or '').strip(), ensure_ascii=False)}\n"
            f"allowed_tools={json.dumps([str(item or '').strip() for item in list(allowed_tools or []) if str(item or '').strip()], ensure_ascii=False)}\n"
            f"tool_budget={json.dumps(tool_budget, ensure_ascii=False)}\n"
            f"planning_brief={json.dumps(dict(planning_brief or {}), ensure_ascii=False)}\n"
            f"page_dossier={json.dumps(dict(page_dossier or {}), ensure_ascii=False)}\n"
            f"adjacent_page_context={json.dumps(adjacent_refs, ensure_ascii=False)}\n"
            f"enrichment_targets={json.dumps(compact_targets, ensure_ascii=False)}\n"
            f"output_schema_example={json.dumps(output_schema, ensure_ascii=False)}\n"
        )

    async def _run_json_stage(
        self,
        *,
        llm: Any,
        prompt: str,
        system_prompt: str,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float = 0.1,
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        response = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout=timeout_seconds,
        )
        return self._extract_json_dict(str((response or {}).get("content") or "")), dict(response or {})

    async def _execute_planner_tool_requests(
        self,
        *,
        registry: Any,
        planner_output: Mapping[str, Any],
        allowed_tools: Sequence[str],
    ) -> tuple[List[str], List[Dict[str, Any]], Dict[str, Any]]:
        allowed = {str(item or "").strip() for item in list(allowed_tools or []) if str(item or "").strip()}
        used_tools: List[str] = []
        tool_trace: List[Dict[str, Any]] = []
        tool_budget = dict(planner_output.get("tool_budget") or {})
        max_tool_requests = self._coerce_budget_value(tool_budget.get("max_tool_requests"), 4, minimum=1, maximum=10)
        max_reader_native_requests = self._coerce_budget_value(tool_budget.get("max_reader_native_requests"), 2, minimum=0, maximum=4)
        max_public_web_requests = self._coerce_budget_value(tool_budget.get("max_public_web_requests"), 1, minimum=0, maximum=6)
        per_tool_timeout_seconds = self._coerce_budget_value(tool_budget.get("per_tool_timeout_seconds"), 12, minimum=4, maximum=45)
        allow_web_scrape = bool(tool_budget.get("allow_web_scrape", True))
        duplicate_query_policy = str(tool_budget.get("duplicate_query_policy") or "exact_query_text").strip() or "exact_query_text"
        public_web_allowlist = [
            str(item or "").strip().lower()
            for item in list(tool_budget.get("public_web_allowlist") or [])
            if str(item or "").strip()
        ]
        seen_requests: set[str] = set()
        budget_events: List[Dict[str, Any]] = []
        native_count = 0
        public_count = 0
        executed_count = 0
        guided_beats = [
            dict(row)
            for row in list(planner_output.get("guided_beats") or [])
            if isinstance(row, Mapping)
        ]
        beat_lookup = {
            str(row.get("beat_id") or "").strip(): dict(row)
            for row in guided_beats
            if str(row.get("beat_id") or "").strip()
        }
        beat_packets: Dict[str, Dict[str, Any]] = {}
        pending_followups: List[Dict[str, Any]] = []

        def ensure_beat_packet(beat_id: str) -> Dict[str, Any]:
            packet = beat_packets.get(beat_id)
            if packet is None:
                beat = beat_lookup.get(beat_id, {})
                packet = {
                    "beat_id": beat_id,
                    "title": str(beat.get("title") or "").strip(),
                    "reader_goal": str(beat.get("reader_goal") or "").strip(),
                    "tool_objectives": [str(item).strip() for item in list(beat.get("tool_objectives") or []) if str(item).strip()],
                    "requested_tools": [],
                    "tool_findings": [],
                    "public_links": [],
                    "summary": "",
                    "supporting_points": [],
                    "reader_facing_notes": [],
                    "tool_accounting": {},
                }
                beat_packets[beat_id] = packet
            return packet

        def attach_public_links_from_data(*, beat_packet: Optional[Dict[str, Any]], tool_name: str, data: Mapping[str, Any]) -> None:
            if beat_packet is None:
                return
            if isinstance(data.get("public_links"), Sequence) and not isinstance(data.get("public_links"), (str, bytes)):
                for link in list(data.get("public_links") or []):
                    if isinstance(link, Mapping):
                        beat_packet["public_links"].append(dict(link))
            if tool_name == "web_search":
                structured = data.get("structured_content")
                if isinstance(structured, Mapping) and isinstance(structured.get("results"), list):
                    for item in list(structured.get("results") or []):
                        if not isinstance(item, Mapping):
                            continue
                        href = str(item.get("url") or "").strip()
                        if not href:
                            continue
                        beat_packet["public_links"].append(
                            {
                                "label": str(item.get("title") or href).strip(),
                                "href": href,
                                "snippet": str(item.get("snippet") or "").strip(),
                            }
                        )

        def queue_followup_scrape(*, beat_id: str, data: Mapping[str, Any]) -> None:
            if not allow_web_scrape:
                return
            request = self._build_web_scrape_followup_request(
                beat_id=beat_id,
                search_data=data,
            )
            if request is None:
                return
            signature = self._extract_tool_request_identity(
                str(request.get("tool") or "").strip(),
                dict(request.get("arguments") or {}),
                policy=duplicate_query_policy,
            )
            if signature in seen_requests:
                return
            seen_requests.add(signature)
            pending_followups.append(request)

        reserved_followup_slots = 1 if allow_web_scrape and max_public_web_requests > 1 else 0
        initial_public_request_cap = max(0, max_public_web_requests - reserved_followup_slots)
        initial_tool_request_cap = max(1, max_tool_requests - reserved_followup_slots) if reserved_followup_slots else max_tool_requests

        for row in list(planner_output.get("tool_requests") or []):
            if not isinstance(row, Mapping):
                continue
            tool_name = str(row.get("tool") or "").strip()
            beat_id = str(row.get("beat_id") or "").strip()
            if tool_name not in allowed:
                continue
            if executed_count >= initial_tool_request_cap:
                budget_events.append({"type": "suppressed", "tool": tool_name, "reason": "max_tool_requests", "beat_id": beat_id})
                continue
            arguments = self._normalize_tool_request_arguments(
                tool_name=tool_name,
                raw_arguments=row.get("arguments") or {},
            )
            if not arguments:
                continue
            request_bucket = self._classify_tool_budget_bucket(tool_name)
            if tool_name == "web_scrape" and not allow_web_scrape:
                budget_events.append({"type": "suppressed", "tool": tool_name, "reason": "web_scrape_disabled", "beat_id": beat_id})
                continue
            if request_bucket == "reader_native" and native_count >= max_reader_native_requests:
                budget_events.append({"type": "suppressed", "tool": tool_name, "reason": "max_reader_native_requests", "beat_id": beat_id})
                continue
            if request_bucket == "public_web" and public_count >= initial_public_request_cap:
                budget_events.append({"type": "suppressed", "tool": tool_name, "reason": "max_public_web_requests", "beat_id": beat_id})
                continue
            if public_web_allowlist and tool_name == "web_scrape":
                hostname = str(urlparse(str(arguments.get("url") or "")).hostname or "").strip().lower()
                if hostname and hostname not in public_web_allowlist and not any(hostname.endswith(f".{domain}") for domain in public_web_allowlist):
                    budget_events.append({"type": "suppressed", "tool": tool_name, "reason": "public_web_allowlist", "hostname": hostname, "beat_id": beat_id})
                    continue
            signature = self._extract_tool_request_identity(tool_name, arguments, policy=duplicate_query_policy)
            if signature in seen_requests:
                budget_events.append({"type": "suppressed", "tool": tool_name, "reason": "duplicate_query", "beat_id": beat_id})
                continue
            seen_requests.add(signature)
            reason = self._clean_excerpt(str(row.get("reason") or "").strip(), limit=180)
            priority = str(row.get("priority") or "medium").strip() or "medium"
            request_origin = "planner"
            beat_packet = ensure_beat_packet(beat_id) if beat_id else None
            if beat_packet is not None:
                beat_packet["requested_tools"].append(
                    {
                        "tool": tool_name,
                        "arguments": dict(arguments),
                        "reason": reason,
                        "priority": priority,
                        "request_origin": request_origin,
                    }
                )
            tool_trace.append(
                {
                    "type": "action",
                    "data": {
                        "tool": tool_name,
                        "input": arguments,
                        "reason": reason,
                        "priority": priority,
                        "request_origin": request_origin,
                        "stage": "tool_enricher",
                        "beat_id": beat_id,
                    },
                }
            )
            used_tools.append(tool_name)
            executed_count += 1
            if request_bucket == "reader_native":
                native_count += 1
            elif request_bucket == "public_web":
                public_count += 1
            try:
                result = await asyncio.wait_for(
                    registry.execute(tool_name, **arguments),
                    timeout=float(per_tool_timeout_seconds),
                )
                observation = {
                    "tool": tool_name,
                    "success": bool(getattr(result, "success", False)),
                    "input": arguments,
                    "output": str(getattr(result, "output", "") or ""),
                    "error": getattr(result, "error", None),
                    "request_origin": request_origin,
                    "stage": "tool_enricher",
                    "beat_id": beat_id,
                }
                data = getattr(result, "data", None)
                if isinstance(data, Mapping):
                    observation["data"] = dict(data)
                    attach_public_links_from_data(beat_packet=beat_packet, tool_name=tool_name, data=data)
                    if tool_name == "web_search":
                        queue_followup_scrape(beat_id=beat_id, data=data)
                if beat_packet is not None:
                    output_excerpt = self._extract_tool_output_excerpt(
                        tool_name=tool_name,
                        result=result,
                    )
                    source_meta = self._extract_tool_result_source_meta(
                        tool_name=tool_name,
                        result=result,
                        arguments=arguments,
                    )
                    beat_packet["tool_findings"].append(
                        {
                            "tool": tool_name,
                            "success": bool(getattr(result, "success", False)),
                            "output_excerpt": output_excerpt,
                            "error": str(getattr(result, "error", None) or "").strip(),
                            "request_origin": request_origin,
                            **source_meta,
                        }
                    )
                    beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))
                tool_trace.append({"type": "observation", "data": observation})
            except asyncio.TimeoutError:
                budget_events.append({"type": "timeout", "tool": tool_name, "reason": "per_tool_timeout_seconds", "beat_id": beat_id})
                tool_trace.append(
                    {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": False,
                            "input": arguments,
                            "output": "",
                            "error": "tool_timeout",
                            "request_origin": request_origin,
                            "stage": "tool_enricher",
                            "beat_id": beat_id,
                        },
                    }
                )
                if beat_packet is not None:
                    beat_packet["tool_findings"].append(
                        {
                            "tool": tool_name,
                            "success": False,
                            "output_excerpt": "",
                            "error": "tool_timeout",
                            "request_origin": request_origin,
                        }
                    )
                    beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))
            except Exception as exc:  # pragma: no cover - defensive
                tool_trace.append(
                    {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": False,
                            "input": arguments,
                            "output": "",
                            "error": str(exc),
                            "request_origin": request_origin,
                            "stage": "tool_enricher",
                            "beat_id": beat_id,
                        },
                    }
                )
                if beat_packet is not None:
                    beat_packet["tool_findings"].append(
                        {
                            "tool": tool_name,
                            "success": False,
                            "output_excerpt": "",
                            "error": str(exc),
                            "request_origin": request_origin,
                        }
                    )
                    beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))

        if "web_search" in allowed and public_count < max_public_web_requests and not self._extract_public_links_from_tool_trace(tool_trace, limit=1):
            followup = self._build_public_web_backfill_request(planner_output=planner_output)
            if followup is not None:
                tool_name = str(followup.get("tool") or "").strip()
                arguments = self._normalize_tool_request_arguments(
                    tool_name=tool_name,
                    raw_arguments=followup.get("arguments") or {},
                )
                request_identity = self._extract_tool_request_identity(tool_name, arguments, policy=duplicate_query_policy)
                beat_id = str(followup.get("beat_id") or "").strip()
                if arguments and request_identity not in seen_requests and executed_count < max_tool_requests:
                    seen_requests.add(request_identity)
                    beat_packet = ensure_beat_packet(beat_id) if beat_id else None
                    reason = self._clean_excerpt(str(followup.get("reason") or "").strip(), limit=180)
                    priority = str(followup.get("priority") or "medium").strip() or "medium"
                    request_origin = "backfill"
                    if beat_packet is not None:
                        beat_packet["requested_tools"].append(
                            {
                                "tool": tool_name,
                                "arguments": dict(arguments),
                                "reason": reason,
                                "priority": priority,
                                "request_origin": request_origin,
                            }
                        )
                    tool_trace.append(
                        {
                            "type": "action",
                            "data": {
                                "tool": tool_name,
                                "input": arguments,
                                "reason": reason,
                                "priority": priority,
                                "request_origin": request_origin,
                                "stage": "tool_enricher",
                                "beat_id": beat_id,
                            },
                        }
                    )
                    used_tools.append(tool_name)
                    executed_count += 1
                    public_count += 1
                    try:
                        result = await asyncio.wait_for(
                            registry.execute(tool_name, **arguments),
                            timeout=float(per_tool_timeout_seconds),
                        )
                        observation = {
                            "tool": tool_name,
                            "success": bool(getattr(result, "success", False)),
                            "input": arguments,
                            "output": str(getattr(result, "output", "") or ""),
                            "error": getattr(result, "error", None),
                            "request_origin": request_origin,
                            "stage": "tool_enricher",
                            "beat_id": beat_id,
                        }
                        data = getattr(result, "data", None)
                        if isinstance(data, Mapping):
                            observation["data"] = dict(data)
                            attach_public_links_from_data(beat_packet=beat_packet, tool_name=tool_name, data=data)
                            if tool_name == "web_search":
                                queue_followup_scrape(beat_id=beat_id, data=data)
                        if beat_packet is not None:
                            output_excerpt = self._extract_tool_output_excerpt(
                                tool_name=tool_name,
                                result=result,
                            )
                            source_meta = self._extract_tool_result_source_meta(
                                tool_name=tool_name,
                                result=result,
                                arguments=arguments,
                            )
                            beat_packet["tool_findings"].append(
                                {
                                    "tool": tool_name,
                                    "success": bool(getattr(result, "success", False)),
                                    "output_excerpt": output_excerpt,
                                    "error": str(getattr(result, "error", None) or "").strip(),
                                    "request_origin": request_origin,
                                    **source_meta,
                                }
                            )
                            beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))
                        tool_trace.append({"type": "observation", "data": observation})
                    except asyncio.TimeoutError:
                        budget_events.append({"type": "timeout", "tool": tool_name, "reason": "per_tool_timeout_seconds", "beat_id": beat_id})
                        tool_trace.append(
                            {
                                "type": "observation",
                                "data": {
                                    "tool": tool_name,
                                    "success": False,
                                    "input": arguments,
                                    "output": "",
                                    "error": "tool_timeout",
                                    "request_origin": request_origin,
                                    "stage": "tool_enricher",
                                    "beat_id": beat_id,
                                },
                            }
                        )
                        if beat_packet is not None:
                            beat_packet["tool_findings"].append(
                                {
                                    "tool": tool_name,
                                    "success": False,
                                    "output_excerpt": "",
                                    "error": "tool_timeout",
                                    "request_origin": request_origin,
                                }
                            )
                            beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))
                    except Exception as exc:  # pragma: no cover - defensive
                        tool_trace.append(
                            {
                                "type": "observation",
                                "data": {
                                    "tool": tool_name,
                                    "success": False,
                                    "input": arguments,
                                    "output": "",
                                    "error": str(exc),
                                    "request_origin": request_origin,
                                    "stage": "tool_enricher",
                                    "beat_id": beat_id,
                                },
                            }
                        )
                        if beat_packet is not None:
                            beat_packet["tool_findings"].append(
                                {
                                    "tool": tool_name,
                                    "success": False,
                                    "output_excerpt": "",
                                    "error": str(exc),
                                    "request_origin": request_origin,
                                }
                            )
                            beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))

        while pending_followups and executed_count < max_tool_requests and public_count < max_public_web_requests:
            followup = pending_followups.pop(0)
            tool_name = str(followup.get("tool") or "").strip()
            beat_id = str(followup.get("beat_id") or "").strip()
            arguments = self._normalize_tool_request_arguments(
                tool_name=tool_name,
                raw_arguments=followup.get("arguments") or {},
            )
            if not arguments:
                continue
            beat_packet = ensure_beat_packet(beat_id) if beat_id else None
            reason = self._clean_excerpt(str(followup.get("reason") or "").strip(), limit=180)
            priority = str(followup.get("priority") or "medium").strip() or "medium"
            request_origin = "followup"
            if beat_packet is not None:
                beat_packet["requested_tools"].append(
                    {
                        "tool": tool_name,
                        "arguments": dict(arguments),
                        "reason": reason,
                        "priority": priority,
                        "request_origin": request_origin,
                    }
                )
            tool_trace.append(
                {
                    "type": "action",
                    "data": {
                        "tool": tool_name,
                        "input": arguments,
                        "reason": reason,
                        "priority": priority,
                        "request_origin": request_origin,
                        "stage": "tool_enricher",
                        "beat_id": beat_id,
                    },
                }
            )
            used_tools.append(tool_name)
            executed_count += 1
            public_count += 1
            try:
                result = await asyncio.wait_for(
                    registry.execute(tool_name, **arguments),
                    timeout=float(per_tool_timeout_seconds),
                )
                observation = {
                    "tool": tool_name,
                    "success": bool(getattr(result, "success", False)),
                    "input": arguments,
                    "output": str(getattr(result, "output", "") or ""),
                    "error": getattr(result, "error", None),
                    "request_origin": request_origin,
                    "stage": "tool_enricher",
                    "beat_id": beat_id,
                }
                data = getattr(result, "data", None)
                if isinstance(data, Mapping):
                    observation["data"] = dict(data)
                    attach_public_links_from_data(beat_packet=beat_packet, tool_name=tool_name, data=data)
                if beat_packet is not None:
                    output_excerpt = self._extract_tool_output_excerpt(
                        tool_name=tool_name,
                        result=result,
                    )
                    source_meta = self._extract_tool_result_source_meta(
                        tool_name=tool_name,
                        result=result,
                        arguments=arguments,
                    )
                    beat_packet["tool_findings"].append(
                        {
                            "tool": tool_name,
                            "success": bool(getattr(result, "success", False)),
                            "output_excerpt": output_excerpt,
                            "error": str(getattr(result, "error", None) or "").strip(),
                            "request_origin": request_origin,
                            **source_meta,
                        }
                    )
                    beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))
                tool_trace.append({"type": "observation", "data": observation})
            except asyncio.TimeoutError:
                budget_events.append({"type": "timeout", "tool": tool_name, "reason": "per_tool_timeout_seconds", "beat_id": beat_id})
                tool_trace.append(
                    {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": False,
                            "input": arguments,
                            "output": "",
                            "error": "tool_timeout",
                            "request_origin": request_origin,
                            "stage": "tool_enricher",
                            "beat_id": beat_id,
                        },
                    }
                )
                if beat_packet is not None:
                    beat_packet["tool_findings"].append(
                        {
                            "tool": tool_name,
                            "success": False,
                            "output_excerpt": "",
                            "error": "tool_timeout",
                            "request_origin": request_origin,
                        }
                    )
                    beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))
            except Exception as exc:  # pragma: no cover - defensive
                tool_trace.append(
                    {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": False,
                            "input": arguments,
                            "output": "",
                            "error": str(exc),
                            "request_origin": request_origin,
                            "stage": "tool_enricher",
                            "beat_id": beat_id,
                        },
                    }
                )
                if beat_packet is not None:
                    beat_packet["tool_findings"].append(
                        {
                            "tool": tool_name,
                            "success": False,
                            "output_excerpt": "",
                            "error": str(exc),
                            "request_origin": request_origin,
                        }
                    )
                    beat_packet.update(self._build_reader_facing_beat_enrichment(beat_packet=beat_packet))

        tool_action_rows = [
            dict(row.get("data") or {})
            for row in list(tool_trace or [])
            if str(row.get("type") or "").strip() == "action"
            and isinstance(row.get("data"), Mapping)
        ]
        runtime_requested_tools = [
            {
                "beat_id": str(item.get("beat_id") or "").strip(),
                "tool": str(item.get("tool") or "").strip(),
                "reason": self._clean_excerpt(str(item.get("reason") or "").strip(), limit=180),
                "priority": str(item.get("priority") or "").strip(),
                "request_origin": str(item.get("request_origin") or "planner").strip() or "planner",
            }
            for item in tool_action_rows
            if str(item.get("tool") or "").strip()
        ]
        planner_executed_count = sum(1 for item in runtime_requested_tools if str(item.get("request_origin") or "planner") == "planner")
        backfill_executed_count = sum(1 for item in runtime_requested_tools if str(item.get("request_origin") or "planner") == "backfill")
        followup_executed_count = sum(1 for item in runtime_requested_tools if str(item.get("request_origin") or "planner") == "followup")

        tool_findings: List[Dict[str, Any]] = []
        for row in list(tool_trace or []):
            if str(row.get("type") or "").strip() != "observation":
                continue
            data = dict(row.get("data") or {})
            output_excerpt = self._sanitize_reader_facing_text(str(data.get("output") or "").strip(), limit=320)
            tool_findings.append(
                {
                    "beat_id": str(data.get("beat_id") or "").strip(),
                    "tool": str(data.get("tool") or "").strip(),
                    "success": bool(data.get("success")),
                    "input": dict(data.get("input") or {}) if isinstance(data.get("input"), Mapping) else {},
                    "output_excerpt": output_excerpt,
                    "error": str(data.get("error") or "").strip(),
                    "request_origin": str(data.get("request_origin") or "planner").strip() or "planner",
                }
            )

        planner_requested_tools = [
            dict(row)
            for row in list(planner_output.get("tool_requests") or [])[:max_tool_requests]
            if isinstance(row, Mapping)
        ]
        planner_requested_count = len([row for row in list(planner_output.get("tool_requests") or []) if isinstance(row, Mapping)])
        requested_tool_count = planner_requested_count + backfill_executed_count
        enrichment_packet = {
            "version": "v1",
            "tool_budget": tool_budget,
            "requested_tools": planner_requested_tools,
            "runtime_requested_tools": runtime_requested_tools[:max_tool_requests + max_public_web_requests],
            "executed_tools": list(dict.fromkeys([str(item).strip() for item in used_tools if str(item).strip()])),
            "tool_findings": tool_findings[:8],
            "public_links": self._extract_public_links_from_tool_trace(tool_trace, limit=4),
            "resource_objectives": [str(item or "").strip() for item in list(planner_output.get("resource_objectives") or []) if str(item or "").strip()],
            "page_generation_notes": [str(item or "").strip() for item in list(planner_output.get("page_generation_notes") or []) if str(item or "").strip()],
            "budget_events": budget_events,
            "budget_summary": {
                "requested_tool_count": requested_tool_count,
                "planner_requested_tool_count": planner_requested_count,
                "backfill_request_count": backfill_executed_count,
                "followup_request_count": followup_executed_count,
                "total_requested_tool_count": requested_tool_count + followup_executed_count,
                "executed_tool_count": executed_count,
                "executed_requested_tool_count": planner_executed_count + backfill_executed_count,
                "executed_planner_tool_count": planner_executed_count,
                "executed_backfill_tool_count": backfill_executed_count,
                "executed_followup_tool_count": followup_executed_count,
                "suppressed_request_count": len([row for row in budget_events if str(row.get("type") or "").strip() == "suppressed"]),
                "timeout_count": len([row for row in budget_events if str(row.get("type") or "").strip() == "timeout"]),
            },
        }
        enrichment_packet["beat_packets"] = [
            {
                **dict(packet),
                "public_links": self._normalize_public_links(
                    [
                        dict(link)
                        for link in list(packet.get("public_links") or [])
                        if isinstance(link, Mapping)
                    ],
                    limit=4,
                ),
            }
            for packet in beat_packets.values()
        ]
        return list(dict.fromkeys([str(item).strip() for item in used_tools if str(item).strip()])), tool_trace, enrichment_packet

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
    def _compose_target_from_ui_node(
        cls,
        *,
        page: int,
        row: Mapping[str, Any],
    ) -> Dict[str, Any]:
        node_id = str(row.get("id") or "").strip()
        node_type = str(row.get("type") or "").strip()
        props = dict(row.get("props") or {})
        if not node_id or not node_type:
            return {}

        target_kind = ""
        title = ""
        figure_label = ""
        section_label = ""
        excerpt = ""
        full_text = ""

        if node_type == "FigurePanel":
            target_kind = "figure"
            figure_label = str(props.get("source_label") or "").strip()
            title = str(props.get("title") or "").strip() or figure_label
            caption = str(props.get("caption") or "").strip()
            excerpt = cls._clean_excerpt(caption, limit=280)
            full_text = " ".join(part for part in [figure_label, title, caption] if part).strip()
        elif node_type == "TablePanel":
            target_kind = "table"
            title = str(props.get("title") or "").strip()
            caption = str(props.get("caption") or "").strip()
            excerpt = cls._clean_excerpt(caption, limit=280)
            full_text = " ".join(part for part in [title, caption] if part).strip()
        elif node_type == "EquationPanel":
            target_kind = "equation"
            title = str(props.get("title") or "").strip()
            caption = str(props.get("caption") or "").strip()
            excerpt = cls._clean_excerpt(caption, limit=280)
            full_text = " ".join(part for part in [title, caption] if part).strip()
        elif node_type == "ParagraphProse":
            target_kind = "paragraph"
            title = str(props.get("title") or row.get("title") or "").strip()
            section_label = str(
                props.get("section_label")
                or props.get("heading")
                or props.get("source_label")
                or row.get("title")
                or ""
            ).strip()
            text_parts: List[str] = []
            paragraphs = props.get("paragraphs")
            if isinstance(paragraphs, Sequence) and not isinstance(paragraphs, (str, bytes)):
                for item in paragraphs:
                    if not isinstance(item, Mapping):
                        continue
                    value = str(item.get("text") or "").strip()
                    if value:
                        text_parts.append(value)
            if not text_parts:
                for candidate in (
                    props.get("text"),
                    props.get("content"),
                    props.get("body"),
                ):
                    value = str(candidate or "").strip()
                    if value:
                        text_parts.append(value)
            full_text = " ".join(text_parts).strip()
            excerpt = cls._clean_excerpt(full_text, limit=280)
        elif node_type == "SectionHeading":
            target_kind = "section"
            title = str(props.get("text") or props.get("title") or row.get("title") or "").strip()
            section_label = title
            excerpt = cls._clean_excerpt(title, limit=120)
            full_text = title
        else:
            return {}

        target_id = f"p{int(page)}:{node_id}" if int(page or 0) > 0 else node_id
        if not excerpt and not full_text and not title and not section_label and not figure_label:
            return {}
        return {
            "target_id": target_id,
            "node_id": node_id,
            "target_kind": target_kind,
            "kind": target_kind,
            "component_type": node_type,
            "title": title,
            "figure_label": figure_label,
            "section_label": section_label,
            "summary": excerpt,
            "excerpt": excerpt,
            "full_text": full_text,
            "source_block_ids": cls._extract_source_block_ids_from_ui_node(row),
        }

    @classmethod
    def _extract_current_page_targets_from_compose_payload(
        cls,
        compose_payload: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        page = int((compose_payload or {}).get("page") or 0)
        ui_plan = dict((compose_payload or {}).get("ui_plan") or {})
        flat_nodes = cls._flatten_ui_nodes(list(ui_plan.get("components") or []))
        extracted: List[Dict[str, Any]] = []
        seen_node_ids: set[str] = set()
        for row in flat_nodes:
            if not isinstance(row, Mapping):
                continue
            target = cls._compose_target_from_ui_node(page=page, row=row)
            node_id = str(target.get("node_id") or "").strip()
            if not target or not node_id or node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            extracted.append(target)
        return extracted

    @classmethod
    def _build_current_page_target_map(
        cls,
        *,
        enrichment_bundle: Mapping[str, Any],
        compose_payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        indexed = cls._index_targets(enrichment_bundle)
        node_id_index = {
            str(row.get("node_id") or "").strip(): target_id
            for target_id, row in list(indexed.items())
            if str(row.get("node_id") or "").strip()
        }
        if compose_payload:
            for target_id, row in list(indexed.items()):
                compose_details = cls._extract_compose_target_details(
                    compose_payload=compose_payload,
                    target_id=target_id,
                )
                if compose_details:
                    merged = dict(row)
                    for key, value in compose_details.items():
                        if value:
                            merged[key] = value
                    indexed[target_id] = merged
            for row in cls._extract_current_page_targets_from_compose_payload(compose_payload):
                node_id = str(row.get("node_id") or "").strip()
                existing_target_id = node_id_index.get(node_id) or ""
                if existing_target_id:
                    merged = dict(indexed.get(existing_target_id) or {})
                    for key, value in row.items():
                        if value and (
                            not merged.get(key)
                            or key in {"summary", "excerpt", "full_text"}
                            or cls._looks_like_abstract_page_target(merged)
                        ):
                            merged[key] = value
                    indexed[existing_target_id] = merged
                    source_block_ids = cls._extract_source_block_ids_from_ui_node(row)
                    if source_block_ids:
                        merged["source_block_ids"] = cls._dedupe_strings(
                            [
                                *[
                                    str(item).strip()
                                    for item in list(merged.get("source_block_ids") or [])
                                    if str(item).strip()
                                ],
                                *source_block_ids,
                            ],
                            limit=8,
                        )
                        indexed[existing_target_id] = merged
                        for block_id in source_block_ids:
                            alias = dict(merged)
                            alias["target_id"] = block_id
                            alias["resolved_from_target_id"] = existing_target_id
                            alias["source_block_ids"] = list(merged.get("source_block_ids") or [])
                            indexed[block_id] = alias
                    continue
                materialized = dict(row)
                source_block_ids = cls._extract_source_block_ids_from_ui_node(row)
                if source_block_ids:
                    materialized["source_block_ids"] = source_block_ids
                indexed[str(row.get("target_id") or "").strip()] = materialized
                for block_id in source_block_ids:
                    alias = dict(materialized)
                    alias["target_id"] = block_id
                    alias["resolved_from_target_id"] = str(row.get("target_id") or "").strip()
                    indexed[block_id] = alias
        return indexed

    @classmethod
    def _target_reader_surface_text(cls, target: Mapping[str, Any], *, limit: int = 280) -> str:
        return cls._clean_excerpt(
            cls._sanitize_reader_facing_text(
                target.get("full_text")
                or target.get("excerpt")
                or target.get("summary")
                or target.get("title")
                or target.get("section_label")
                or target.get("figure_label")
                or "",
                limit=limit,
            ),
            limit=limit,
        )

    @staticmethod
    def _target_has_source_block_signature(target: Mapping[str, Any]) -> bool:
        for token in (
            str(target.get("target_id") or "").strip(),
            str(target.get("node_id") or "").strip(),
            *[
                str(item).strip()
                for item in list(target.get("source_block_ids") or [])
                if str(item).strip()
            ],
        ):
            if not token:
                continue
            if re.search(r"(?:^|[:_])g\d+$", token, flags=re.IGNORECASE):
                continue
            if re.match(r"^p\d+_(?!g\d+$)[a-z0-9_]+$", token, flags=re.IGNORECASE):
                return True
            if "no_drop_" in token.lower() or re.search(r"_l\d{3,}_b\d{3,}", token, flags=re.IGNORECASE):
                return True
        return False

    @classmethod
    def _looks_like_abstract_page_target(cls, target: Mapping[str, Any]) -> bool:
        target_id = str(target.get("target_id") or "").strip()
        node_id = str(target.get("node_id") or "").strip()
        component_type = str(target.get("component_type") or "").strip().lower()
        text = cls._target_reader_surface_text(target, limit=220)
        generic_patterns = (
            r"\b(?:guide|summary|overview|context|glossary|question|resource|starter|explainer|callout|widget)\b",
            r"(?:概览|摘要|总结|引导|背景|术语|追问|资源|讲解)",
        )
        if (
            component_type in {"guidesummarycard", "summarycard", "overviewcard"}
            or component_type.startswith("guide")
            or component_type.endswith("summarycard")
        ):
            return True
        if re.search(r"(?:^|:)g\d+$", target_id, flags=re.IGNORECASE) or re.fullmatch(r"g\d+", node_id, flags=re.IGNORECASE):
            return True
        if component_type and any(re.search(pattern, component_type, flags=re.IGNORECASE) for pattern in generic_patterns):
            return True
        if text and any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in generic_patterns):
            return True
        return False

    @classmethod
    def _score_current_page_target_for_role(
        cls,
        target: Mapping[str, Any],
        *,
        role: str,
        seed_target_ids: Sequence[str] = (),
    ) -> int:
        current = dict(target or {})
        target_id = str(current.get("target_id") or "").strip()
        kind = str(current.get("target_kind") or current.get("kind") or "").strip().lower()
        component_type = str(current.get("component_type") or "").strip().lower()
        text = cls._target_reader_surface_text(current, limit=280)
        score = 0

        if role == "figure":
            if kind in {"figure", "table", "equation"}:
                score += 220
            else:
                score -= 260
        else:
            if kind in {"figure", "table", "equation"}:
                score -= 260
            else:
                score += 180

        if component_type in {"figurepanel", "tablepanel", "equationpanel"}:
            score += 90 if role == "figure" else -80
        elif component_type == "paragraphprose":
            score += 95 if role == "body" else -80
        elif component_type == "sectionheading":
            score += 35 if role == "body" else -40
        elif component_type:
            score += 10

        if cls._target_has_source_block_signature(current):
            score += 80
        if cls._looks_like_abstract_page_target(current):
            score -= 220
        if text:
            if not cls._is_fragment_like_excerpt(text):
                score += 35
            if cls._has_reader_facing_predicate(text):
                score += 25
            if role == "body":
                score += len(cls._extract_grounding_numbers(text, limit=3)) * 20
                if cls._extract_grounding_terms(text, limit=3):
                    score += 15
        if target_id and target_id in {str(item).strip() for item in list(seed_target_ids or []) if str(item).strip()}:
            score += 40
        return score

    @classmethod
    def _select_preferred_current_page_target_ids(
        cls,
        *,
        target_map: Mapping[str, Mapping[str, Any]],
        role: str,
        seed_target_ids: Sequence[str] = (),
        limit: int = 1,
    ) -> List[str]:
        ranked: List[tuple[int, int, str, bool]] = []
        for order, (target_id, raw_target) in enumerate(list(target_map.items())):
            current = dict(raw_target or {})
            if not target_id or not current:
                continue
            score = cls._score_current_page_target_for_role(
                current,
                role=role,
                seed_target_ids=seed_target_ids,
            )
            ranked.append((score, -order, target_id, cls._looks_like_abstract_page_target(current)))
        ranked.sort(reverse=True)
        concrete_ranked = [
            (score, order, target_id, is_abstract)
            for score, order, target_id, is_abstract in ranked
            if score >= 40 and not is_abstract
        ]
        candidate_rows = concrete_ranked or ranked
        selected: List[str] = []
        for score, _, target_id, is_abstract in candidate_rows:
            if score < 40 and selected:
                break
            if score < -40:
                continue
            if concrete_ranked and is_abstract:
                continue
            selected.append(target_id)
            if len(selected) >= limit:
                break
        if selected:
            return selected
        return cls._dedupe_strings([str(item).strip() for item in list(seed_target_ids or []) if str(item).strip()], limit=limit)

    def _prefer_concrete_target_ids_for_story_substrate(
        self,
        *,
        story_substrate: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        current = dict(story_substrate or {})
        claims: List[Dict[str, Any]] = []
        for row in list(current.get("main_claims") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            seed_target_ids = list(item.get("source_target_ids") or item.get("target_ids") or [])
            claim_text = " ".join(
                part
                for part in (
                    str(item.get("text") or "").strip(),
                    str(item.get("display_text") or "").strip(),
                )
                if part
            ).strip()
            claim_role = "figure" if (
                any(
                    str(dict(target_map.get(str(target_id).strip()) or {}).get("target_kind") or dict(target_map.get(str(target_id).strip()) or {}).get("kind") or "").strip().lower()
                    in {"figure", "table", "equation"}
                    for target_id in seed_target_ids
                    if str(target_id).strip()
                )
                or re.search(r"\b(?:fig(?:ure)?|table|equation)\b", claim_text, flags=re.IGNORECASE)
            ) else "body"
            item["source_target_ids"] = self._select_preferred_current_page_target_ids(
                target_map=target_map,
                role=claim_role,
                seed_target_ids=seed_target_ids,
                limit=1 if claim_role == "figure" else 2,
            )
            claims.append(item)
        if claims:
            current["main_claims"] = claims

        evidence_units: List[Dict[str, Any]] = []
        for row in list(current.get("evidence_units") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            role = "figure" if str(item.get("kind") or "").strip() in {"figure", "table", "equation"} else "body"
            item["source_target_ids"] = self._select_preferred_current_page_target_ids(
                target_map=target_map,
                role=role,
                seed_target_ids=list(item.get("source_target_ids") or []),
                limit=2 if role == "body" else 1,
            )
            evidence_units.append(item)
        if evidence_units:
            current["evidence_units"] = evidence_units

        terms: List[Dict[str, Any]] = []
        for row in list(current.get("terms_to_explain") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            item["source_target_ids"] = self._select_preferred_current_page_target_ids(
                target_map=target_map,
                role="body",
                seed_target_ids=list(item.get("source_target_ids") or []),
                limit=2,
            )
            terms.append(item)
        if terms:
            current["terms_to_explain"] = terms

        turns: List[Dict[str, Any]] = []
        for row in list(current.get("narrative_turns") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            turn_kind = str(item.get("kind") or "").strip()
            role = "figure" if turn_kind == "figure_focus" else "body"
            item["target_ids"] = self._select_preferred_current_page_target_ids(
                target_map=target_map,
                role=role,
                seed_target_ids=list(item.get("target_ids") or []),
                limit=2 if role == "body" else 1,
            )
            turns.append(item)
        if turns:
            current["narrative_turns"] = turns
        return current

    def _prefer_concrete_target_ids_for_page_brief(
        self,
        *,
        page_brief: Mapping[str, Any],
        target_map: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        current = dict(page_brief or {})
        primary_focus_target_id = str(current.get("primary_focus_target_id") or "").strip()
        preferred_focus_ids = self._select_preferred_current_page_target_ids(
            target_map=target_map,
            role="figure",
            seed_target_ids=[primary_focus_target_id] if primary_focus_target_id else [],
            limit=1,
        )
        if preferred_focus_ids:
            current["primary_focus_target_id"] = preferred_focus_ids[0]

        body_seed_ids = [
            str(item).strip()
            for item in list(current.get("body_flow_target_ids") or current.get("secondary_support_target_ids") or [])
            if str(item).strip()
        ]
        preferred_body_ids = self._select_preferred_current_page_target_ids(
            target_map=target_map,
            role="body",
            seed_target_ids=body_seed_ids,
            limit=4,
        )
        if preferred_body_ids:
            current["secondary_support_target_ids"] = preferred_body_ids[:3]
            current["body_flow_target_ids"] = self._dedupe_strings(
                [str(current.get("primary_focus_target_id") or "").strip(), *preferred_body_ids],
                limit=8,
            )

        storyboard_rows: List[Dict[str, Any]] = []
        for row in list(current.get("storyboard") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            section_type = str(item.get("section_type") or "").strip()
            role = "figure" if section_type in {"hero", "focus_stage"} else "body"
            item["target_ids"] = self._select_preferred_current_page_target_ids(
                target_map=target_map,
                role=role,
                seed_target_ids=list(item.get("target_ids") or []),
                limit=2 if role == "body" else 1,
            )
            storyboard_rows.append(item)
        if storyboard_rows:
            current["storyboard"] = storyboard_rows
        return current

    def _compose_page_dossier_from_current_page_targets(
        self,
        *,
        page: int,
        enrichment_bundle: Mapping[str, Any],
        compose_payload: Optional[Mapping[str, Any]] = None,
        existing_page_dossier: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing = dict(existing_page_dossier or {})
        existing_current_page = dict(existing.get("current_page") or {})
        merged_targets: Dict[str, Dict[str, Any]] = {
            str(item.get("target_id") or "").strip(): dict(item)
            for item in list(existing_current_page.get("targets") or [])
            if isinstance(item, Mapping) and str(item.get("target_id") or "").strip()
        }
        target_map = self._build_current_page_target_map(
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
        )
        for target_id, row in list(target_map.items()):
            current = dict(merged_targets.get(target_id) or {})
            current.update(
                {
                    "target_id": target_id,
                    "kind": str(row.get("target_kind") or row.get("kind") or "").strip(),
                    "node_id": str(row.get("node_id") or current.get("node_id") or "").strip(),
                    "component_type": str(row.get("component_type") or current.get("component_type") or "").strip(),
                    "title": str(row.get("title") or current.get("title") or "").strip(),
                    "figure_label": str(row.get("figure_label") or current.get("figure_label") or "").strip(),
                    "section_label": str(row.get("section_label") or current.get("section_label") or "").strip(),
                    "summary": str(row.get("summary") or row.get("excerpt") or current.get("summary") or "").strip(),
                    "excerpt": str(row.get("excerpt") or current.get("excerpt") or "").strip(),
                    "full_text": str(row.get("full_text") or current.get("full_text") or "").strip(),
                }
            )
            merged_targets[target_id] = current

        current_targets = list(merged_targets.values())
        current_targets.sort(
            key=lambda item: (
                -max(
                    self._score_current_page_target_for_role(item, role="figure"),
                    self._score_current_page_target_for_role(item, role="body"),
                ),
                str(item.get("target_id") or ""),
            )
        )
        current_page = {
            **existing_current_page,
            "page": int(existing_current_page.get("page") or page or 0),
            "targets": current_targets[:24],
            "target_ids": [str(item.get("target_id") or "").strip() for item in current_targets if str(item.get("target_id") or "").strip()][:24],
        }
        return {
            **existing,
            "focus_page": int(existing.get("focus_page") or current_page.get("page") or page or 0),
            "current_page": current_page,
        }

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
        normalized["body_flow_target_ids"] = self._dedupe_strings(
            [str(item).strip() for item in list(current.get("body_flow_target_ids") or []) if str(item).strip()]
        ) or list(fallback.get("body_flow_target_ids") or [])
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
                "reader_goal": str(row.get("reader_goal") or "").strip() or str(next((item.get("reader_goal") for item in fallback_storyboard if str(item.get("section_type") or "").strip() == section_type), "")).strip(),
                "continuity_note": str(row.get("continuity_note") or "").strip() or str(next((item.get("continuity_note") for item in fallback_storyboard if str(item.get("section_type") or "").strip() == section_type), "")).strip(),
                "target_ids": self._dedupe_strings([str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()]),
                "tool_objectives": self._dedupe_strings([str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()]),
                "block_stack": self._dedupe_strings([str(item).strip() for item in list(row.get("block_stack") or []) if str(item).strip()]),
                "drop_notes": self._dedupe_strings([str(item).strip() for item in list(row.get("drop_notes") or []) if str(item).strip()], limit=6),
                "priority": idx,
                "meta": dict(row.get("meta") or {}),
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

    @staticmethod
    def _storyboard_row_identity(row: Mapping[str, Any], index: int) -> str:
        beat_id = str(row.get("beat_id") or "").strip()
        if beat_id:
            return f"beat:{beat_id}"
        section_type = str(row.get("section_type") or "").strip()
        if section_type:
            return f"section:{section_type}"
        role = str(row.get("role") or "").strip()
        if role:
            return f"role:{role}:{index}"
        return f"row:{index}"

    @classmethod
    def _merge_storyboard_rows(
        cls,
        base_rows: Sequence[Mapping[str, Any]],
        overlay_rows: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        def merge_row(base_row: Mapping[str, Any], overlay_row: Mapping[str, Any]) -> Dict[str, Any]:
            merged = dict(base_row or {})
            overlay = dict(overlay_row or {})
            for key in ["beat_id", "role", "section_type"]:
                if not str(merged.get(key) or "").strip() and str(overlay.get(key) or "").strip():
                    merged[key] = overlay.get(key)
            for key in ["title", "purpose", "reader_goal", "continuity_note"]:
                if str(overlay.get(key) or "").strip():
                    merged[key] = overlay.get(key)
            for key in ["target_ids", "tool_objectives", "drop_notes", "block_stack"]:
                merged[key] = cls._dedupe_strings(
                    [
                        str(item).strip()
                        for item in [*list(merged.get(key) or []), *list(overlay.get(key) or [])]
                        if str(item).strip()
                    ],
                    limit=12 if key == "target_ids" else 6,
                )
            if not merged.get("priority") and overlay.get("priority"):
                merged["priority"] = overlay.get("priority")
            return merged

        result: List[Dict[str, Any]] = []
        overlay_by_identity = {
            cls._storyboard_row_identity(row, index): dict(row)
            for index, row in enumerate(overlay_rows, start=1)
            if isinstance(row, Mapping)
        }
        consumed: set[str] = set()
        for index, row in enumerate(base_rows, start=1):
            if not isinstance(row, Mapping):
                continue
            identity = cls._storyboard_row_identity(row, index)
            overlay = overlay_by_identity.get(identity)
            result.append(merge_row(row, overlay) if overlay is not None else dict(row))
            consumed.add(identity)
        for index, row in enumerate(overlay_rows, start=1):
            if not isinstance(row, Mapping):
                continue
            identity = cls._storyboard_row_identity(row, index)
            if identity in consumed:
                continue
            result.append(dict(row))
        return result

    def _restore_page_brief_guided_reading_contract(
        self,
        *,
        page_brief: Mapping[str, Any],
        meta: Mapping[str, Any],
    ) -> Dict[str, Any]:
        restored = dict(page_brief or {})
        meta = dict(meta or {})
        planning_brief = dict(meta.get("planning_brief") or {})
        planner_output = dict(meta.get("planner_output") or {})
        current_storyboard = [
            dict(row)
            for row in list(restored.get("storyboard") or [])
            if isinstance(row, Mapping)
        ]
        planning_seed = [
            dict(row)
            for row in list(planning_brief.get("guided_beat_seed") or [])
            if isinstance(row, Mapping)
        ]
        planner_beats = [
            dict(row)
            for row in list(planner_output.get("guided_beats") or [])
            if isinstance(row, Mapping)
        ]

        storyboard_rows: List[Dict[str, Any]] = current_storyboard or planning_seed or planner_beats
        if storyboard_rows and planning_seed:
            storyboard_rows = self._merge_storyboard_rows(storyboard_rows, planning_seed)
        if storyboard_rows and planner_beats:
            storyboard_rows = self._merge_storyboard_rows(storyboard_rows, planner_beats)
        if storyboard_rows:
            restored["storyboard"] = storyboard_rows
            restored["reading_path"] = self._storyboard_to_reading_path(storyboard_rows)

        body_flow_target_ids = [
            str(item).strip()
            for item in list(restored.get("body_flow_target_ids") or [])
            if str(item).strip()
        ]
        if not body_flow_target_ids:
            body_flow_target_ids = [
                str(item).strip()
                for item in list(planning_brief.get("body_flow_target_ids") or [])
                if str(item).strip()
            ]
        if body_flow_target_ids:
            restored["body_flow_target_ids"] = self._dedupe_strings(body_flow_target_ids, limit=24)

        return restored

    def _apply_beat_native_guidance_to_plan(
        self,
        *,
        parsed: Mapping[str, Any],
    ) -> Dict[str, Any]:
        current = dict(parsed or {})
        meta = dict(current.get("meta") or {})
        page_brief = self._restore_page_brief_guided_reading_contract(
            page_brief=dict(current.get("page_brief") or {}),
            meta=meta,
        )
        planner_output = dict(meta.get("planner_output") or {})
        tool_enrichment_packet = dict(meta.get("tool_enrichment_packet") or {})
        guidance = self._build_storyboard_beat_guidance(
            storyboard=list(page_brief.get("storyboard") or []),
            planner_output=planner_output,
            tool_enrichment_packet=tool_enrichment_packet,
        )
        if guidance["storyboard"]:
            page_brief["storyboard"] = guidance["storyboard"]
            page_brief["reading_path"] = self._storyboard_to_reading_path(guidance["storyboard"])

        storyboard_rows: List[Dict[str, Any]] = []
        for row in list(page_brief.get("storyboard") or []):
            if not isinstance(row, Mapping):
                continue
            beat = dict(row)
            beat_id = str(beat.get("beat_id") or "").strip()
            section_type = str(beat.get("section_type") or "").strip()
            packet = guidance["packets_by_id"].get(beat_id) or guidance["packets_by_section"].get(section_type)
            beat_meta = dict(beat.get("meta") or {})
            beat["meta"] = beat_meta
            storyboard_rows.append(beat)
        if storyboard_rows:
            page_brief["storyboard"] = storyboard_rows

        if not str(page_brief.get("resource_strategy") or "").strip():
            supporting_packet = guidance["packets_by_section"].get("supporting_resources")
            supporting_copy = self._extract_beat_packet_reader_copy(supporting_packet)
            if supporting_copy["summary"]:
                page_brief["resource_strategy"] = supporting_copy["summary"]

        max_hooks = self._coerce_budget_value(
            dict(page_brief.get("content_budget") or {}).get("max_hooks"),
            2,
            minimum=0,
            maximum=4,
        )
        if max_hooks > 0 and not list(page_brief.get("experience_hooks") or []):
            hook_candidates: List[str] = []
            for section_type in ("focus_stage", "reading_flow", "supporting_resources"):
                beat = dict(guidance["beats_by_section"].get(section_type) or {})
                packet = guidance["packets_by_section"].get(section_type)
                summary = self._compose_beat_native_summary(
                    beat=beat,
                    packet=packet,
                    default_summary="",
                    limit=120,
                )
                if summary and not self._needs_display_localization(summary):
                    hook_candidates.append(summary)
            if hook_candidates:
                page_brief["experience_hooks"] = self._dedupe_strings(hook_candidates, limit=max_hooks)

        current["page_brief"] = page_brief
        primary_focus_target_id = str(page_brief.get("primary_focus_target_id") or "").strip()

        def _preferred_guided_display_summary(
            *,
            beat: Mapping[str, Any],
            packet: Optional[Mapping[str, Any]],
            default_summary: str,
            limit: int,
        ) -> str:
            raw_packet_summary = self._clean_excerpt(
                self._sanitize_reader_facing_text(
                    dict(packet or {}).get("summary"),
                    limit=limit,
                ),
                limit=limit,
            ) if isinstance(packet, Mapping) else ""
            if raw_packet_summary and (
                not self._looks_like_internal_planner_copy(raw_packet_summary)
                and not self._looks_like_reader_metadata(raw_packet_summary)
                and not self._looks_like_heading_only(raw_packet_summary)
                and not self._looks_like_hype_marketing_copy(raw_packet_summary)
                and not self._needs_display_localization(raw_packet_summary)
                and not self._looks_like_generic_helper_summary(raw_packet_summary)
                and not self._is_generic_narrative_summary(raw_packet_summary)
                and self._is_reader_ready_summary(raw_packet_summary)
                and len(raw_packet_summary) >= 18
            ):
                return raw_packet_summary
            packet_copy = self._extract_beat_packet_reader_copy(packet, summary_limit=limit)
            packet_summary = self._clean_excerpt(str(packet_copy.get("summary") or "").strip(), limit=limit)
            if packet_summary and len(packet_summary) >= 18 and not self._is_generic_narrative_summary(packet_summary):
                return packet_summary
            return self._compose_beat_native_summary(
                beat=beat,
                packet=packet,
                default_summary=default_summary,
                limit=limit,
                prefer_default_if_reader_ready=True,
            )

        resource_modules: List[Dict[str, Any]] = []
        for row in list(current.get("resource_modules") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            module_type = str(item.get("module_type") or "").strip()
            target_ids = {str(token).strip() for token in list(item.get("target_ids") or []) if str(token).strip()}
            section_type = "supporting_resources"
            if module_type == "FigureExplainPanel" and primary_focus_target_id and primary_focus_target_id in target_ids:
                section_type = "focus_stage"
            beat = dict(guidance["beats_by_section"].get(section_type) or {})
            packet = guidance["packets_by_section"].get(section_type)
            beat_title = str(beat.get("title") or "").strip()
            summary = _preferred_guided_display_summary(
                beat=beat,
                packet=packet,
                default_summary=str(item.get("display_summary") or item.get("summary") or "").strip(),
                limit=220,
            )
            if beat_title and self._should_replace_with_guided_title(
                item.get("display_title") or item.get("title"),
                section_type=section_type,
            ):
                item["display_title"] = beat_title
            if summary:
                item["display_summary"] = summary
            packet_copy = self._extract_beat_packet_reader_copy(packet)
            if packet_copy["summary"] and (not str(item.get("summary") or "").strip() or self._is_generic_module_title(str(item.get("title") or ""))):
                item["summary"] = packet_copy["summary"]
            item_meta = dict(item.get("meta") or {})
            if str(beat.get("beat_id") or "").strip():
                item_meta["guided_beat_id"] = str(beat.get("beat_id") or "").strip()
            item["meta"] = item_meta
            resource_modules.append(item)
        current["resource_modules"] = resource_modules

        interaction_modules: List[Dict[str, Any]] = []
        for row in list(current.get("interaction_modules") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            module_type = str(item.get("module_type") or "").strip()
            if module_type == "GlossaryPanel":
                section_type = "explainer_cluster"
            elif module_type == "QuestionStarterPanel":
                section_type = "question_lab"
            else:
                section_type = "supporting_resources"
            beat = dict(guidance["beats_by_section"].get(section_type) or {})
            packet = guidance["packets_by_section"].get(section_type)
            beat_title = str(beat.get("title") or "").strip()
            summary = _preferred_guided_display_summary(
                beat=beat,
                packet=packet,
                default_summary=str(item.get("display_summary") or "").strip(),
                limit=180,
            )
            if beat_title and self._should_replace_with_guided_title(
                item.get("display_title") or item.get("title"),
                section_type=section_type,
            ):
                item["display_title"] = beat_title
            if summary:
                item["display_summary"] = summary
            item_meta = dict(item.get("meta") or {})
            if str(beat.get("beat_id") or "").strip():
                item_meta["guided_beat_id"] = str(beat.get("beat_id") or "").strip()
            item["meta"] = item_meta
            interaction_modules.append(item)
        current["interaction_modules"] = interaction_modules

        widgets: List[Dict[str, Any]] = []
        for row in list(current.get("js_widgets") or []):
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            beat = dict(guidance["beats_by_section"].get("focus_stage") or {})
            packet = guidance["packets_by_section"].get("focus_stage")
            beat_title = str(beat.get("title") or "").strip()
            summary = _preferred_guided_display_summary(
                beat=beat,
                packet=packet,
                default_summary=str(item.get("display_summary") or "").strip(),
                limit=180,
            )
            if beat_title and self._should_replace_with_guided_title(
                item.get("display_title") or item.get("title"),
                section_type="focus_stage",
            ):
                item["display_title"] = beat_title
            if summary:
                item["display_summary"] = summary
            item_meta = dict(item.get("meta") or {})
            if str(beat.get("beat_id") or "").strip():
                item_meta["guided_beat_id"] = str(beat.get("beat_id") or "").strip()
            item["meta"] = item_meta
            widgets.append(item)
        current["js_widgets"] = widgets
        return current

    @staticmethod
    def _has_missing_identifier(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    @classmethod
    def _materialize_missing_block_ids(
        cls,
        *,
        rows: Sequence[Mapping[str, Any]],
        id_field: str,
        type_field: str,
        prefix: str,
        page: int,
    ) -> tuple[List[Dict[str, Any]], int]:
        normalized_rows = [dict(row) for row in list(rows or []) if isinstance(row, Mapping)]
        seen_ids = {
            str(row.get(id_field) or "").strip()
            for row in normalized_rows
            if isinstance(row.get(id_field), str) and str(row.get(id_field) or "").strip()
        }
        repaired_count = 0
        materialized: List[Dict[str, Any]] = []
        for index, row in enumerate(normalized_rows, start=1):
            item = dict(row)
            current_id = item.get(id_field)
            if cls._has_missing_identifier(current_id):
                seed = json.dumps(
                    {
                        "page": int(page),
                        "type": str(item.get(type_field) or "").strip(),
                        "title": cls._clean_excerpt(str(item.get("title") or "").strip(), limit=80),
                        "target_ids": [str(token).strip() for token in list(item.get("target_ids") or []) if str(token).strip()],
                        "index": index,
                        "prefix": prefix,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
                candidate = f"{prefix}_{int(page)}_{index}_{digest}"
                suffix = 2
                while candidate in seen_ids:
                    candidate = f"{prefix}_{int(page)}_{index}_{digest}_{suffix}"
                    suffix += 1
                item[id_field] = candidate
                seen_ids.add(candidate)
                repaired_count += 1
            elif isinstance(current_id, str):
                seen_ids.add(current_id.strip())
            materialized.append(item)
        return materialized, repaired_count

    def _materialize_missing_generative_plan_ids(
        self,
        *,
        parsed: Mapping[str, Any],
        page: int,
    ) -> Dict[str, Any]:
        current = dict(parsed or {})
        resource_modules, resource_repairs = self._materialize_missing_block_ids(
            rows=current.get("resource_modules") or [],
            id_field="module_id",
            type_field="module_type",
            prefix="res",
            page=int(page),
        )
        interaction_modules, interaction_repairs = self._materialize_missing_block_ids(
            rows=current.get("interaction_modules") or [],
            id_field="module_id",
            type_field="module_type",
            prefix="int",
            page=int(page),
        )
        js_widgets, widget_repairs = self._materialize_missing_block_ids(
            rows=current.get("js_widgets") or [],
            id_field="widget_id",
            type_field="widget_type",
            prefix="widget",
            page=int(page),
        )
        current["resource_modules"] = resource_modules
        current["interaction_modules"] = interaction_modules
        current["js_widgets"] = js_widgets
        if resource_repairs or interaction_repairs or widget_repairs:
            meta = dict(current.get("meta") or {})
            meta["id_materialization"] = {
                "resource_modules": int(resource_repairs),
                "interaction_modules": int(interaction_repairs),
                "js_widgets": int(widget_repairs),
                "status": "repaired_missing_ids",
            }
            current["meta"] = meta
        return current

    def _validate_generative_plan_contract(
        self,
        *,
        parsed: Mapping[str, Any],
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
    ) -> Dict[str, Any]:
        current = self._materialize_missing_generative_plan_ids(
            parsed=parsed,
            page=int(page),
        )
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
        current["page_brief"] = self._restore_page_brief_guided_reading_contract(
            page_brief=current["page_brief"],
            meta=current.get("meta") or {},
        )
        try:
            validated = ReaderGenerativePlan.model_validate(current).model_dump(mode="python")
            meta = dict(validated.get("meta") or {})
            meta["contract_validation"] = {"status": "validated", "contract": "generative_plan_v2"}
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
            planner_output = dict(fallback_meta.get("planner_output") or {})
            tool_packet = dict(fallback_meta.get("tool_enrichment_packet") or {})
            guided_beats_preview = [
                dict(item)
                for item in list(planner_output.get("guided_beats") or current["page_brief"].get("storyboard") or [])
                if isinstance(item, Mapping)
            ]
            beat_packets = [
                dict(item)
                for item in list(tool_packet.get("beat_packets") or [])
                if isinstance(item, Mapping)
            ]
            if guided_beats_preview:
                fallback_meta["guided_beats_preview"] = guided_beats_preview
                fallback_meta["guided_beat_count"] = len(guided_beats_preview)
            if beat_packets:
                fallback_meta["tool_enrichment_packet"] = tool_packet
                fallback_meta["beat_packet_count"] = len(beat_packets)
            fallback_meta["contract_validation"] = {
                "status": "fallback",
                "contract": "generative_plan_v2",
                "error_count": exc.error_count(),
                "errors_preview": self._compact_validation_errors(exc),
            }
            fallback["meta"] = fallback_meta
            return ReaderGenerativePlan.model_validate(fallback).model_dump(mode="python")

    def _validate_experience_plan_contract(self, plan: Mapping[str, Any]) -> Dict[str, Any]:
        current = dict(plan or {})
        sanitized_supporting_resources = self._sanitize_supporting_resources_for_reader(
            list(current.get("supporting_resources") or []),
        )
        current["supporting_resources"] = sanitized_supporting_resources
        valid_resource_ids = {
            str(row.get("module_id") or "").strip()
            for row in sanitized_supporting_resources
            if isinstance(row, Mapping) and str(row.get("module_id") or "").strip()
        }
        normalized_sections: List[Dict[str, Any]] = []
        for row in list(current.get("main_sections") or []):
            if not isinstance(row, Mapping):
                continue
            section = dict(row)
            section["resource_module_ids"] = [
                module_id
                for module_id in [
                    str(item).strip()
                    for item in list(section.get("resource_module_ids") or [])
                    if str(item).strip()
                ]
                if module_id in valid_resource_ids
            ]
            normalized_sections.append(section)
        current["main_sections"] = normalized_sections
        current = self._normalize_experience_narrative_contract(plan=current)
        current["main_sections"] = self._normalize_experience_section_blocks(
            sections=list(current.get("main_sections") or []),
            resource_modules=sanitized_supporting_resources,
            interaction_modules=list(current.get("interactive_blocks") or []),
            widget_blocks=list(current.get("widget_blocks") or []),
        )
        current["guided_beats"] = self._normalize_experience_guided_beats(
            guided_beats=list(current.get("guided_beats") or []),
            hero=dict(current.get("hero") or {}),
            sections=list(current.get("main_sections") or []),
        )
        current = self._normalize_experience_narrative_contract(plan=current)
        try:
            validated = ReaderExperiencePlan.model_validate(current).model_dump(mode="python")
            if isinstance(current.get("teaching_manuscript"), Mapping):
                validated["teaching_manuscript"] = dict(current.get("teaching_manuscript") or {})
            meta = dict(validated.get("meta") or {})
            meta["contract_validation"] = {"status": "validated", "contract": "experience_plan_v3"}
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
                "guided_beats": [],
                "teaching_manuscript": current.get("teaching_manuscript"),
                "supporting_resources": [dict(row) for row in list(current.get("supporting_resources") or []) if isinstance(row, Mapping)],
                "interactive_blocks": [dict(row) for row in list(current.get("interactive_blocks") or []) if isinstance(row, Mapping)],
                "widget_blocks": [dict(row) for row in list(current.get("widget_blocks") or []) if isinstance(row, Mapping)],
                "reading_path": [str(item).strip() for item in list(current.get("reading_path") or []) if str(item).strip()],
                "used_tools": [str(item).strip() for item in list(current.get("used_tools") or []) if str(item).strip()],
                "meta": dict(current.get("meta") or {}),
            }
            fallback["guided_beats"] = self._normalize_experience_guided_beats(
                guided_beats=list(current.get("guided_beats") or []),
                hero=hero,
                sections=list(current.get("main_sections") or []),
            )
            fallback["meta"]["contract_validation"] = {
                "status": "fallback",
                "contract": "experience_plan_v3",
                "error_count": exc.error_count(),
            }
            validated_fallback = ReaderExperiencePlan.model_validate(fallback).model_dump(mode="python")
            if isinstance(fallback.get("teaching_manuscript"), Mapping):
                validated_fallback["teaching_manuscript"] = dict(fallback.get("teaching_manuscript") or {})
            return validated_fallback

    def _normalize_experience_narrative_contract(self, *, plan: Mapping[str, Any]) -> Dict[str, Any]:
        current = dict(plan or {})
        meta = dict(current.get("meta") or {})
        hero = dict(current.get("hero") or {})
        page_archetype = str(meta.get("page_archetype") or "").strip() or "finding_digest"
        focus_label = str(hero.get("focus_label") or "").strip()
        planning_brief = dict(meta.get("planning_brief") or {})
        page_dossier = dict(meta.get("page_dossier") or {}) if isinstance(meta.get("page_dossier"), Mapping) else {}
        current_page_targets = [
            dict(item)
            for item in list(dict(page_dossier.get("current_page") or {}).get("targets") or [])
            if isinstance(item, Mapping)
        ]
        dossier_target_map = {
            str(item.get("target_id") or "").strip(): {
                **dict(item),
                "target_kind": str(item.get("kind") or item.get("target_kind") or "").strip(),
            }
            for item in current_page_targets
            if str(item.get("target_id") or "").strip()
        }
        dossier_focus_target_id = str(
            planning_brief.get("primary_focus_target_id")
            or next(
                (
                    item.get("target_id")
                    for item in current_page_targets
                    if str(item.get("kind") or item.get("target_kind") or "").strip() in {"figure", "table"}
                ),
                "",
            )
            or ""
        ).strip()
        dossier_body_target_id = str(
            next(
                (
                    item.get("target_id")
                    for item in current_page_targets
                    if str(item.get("kind") or item.get("target_kind") or "").strip() not in {"figure", "table", "equation"}
                ),
                "",
            )
            or ""
        ).strip()
        hero_target_ids = self._dedupe_strings([dossier_focus_target_id, dossier_body_target_id], limit=2)
        background_topics = [
            str(item).strip()
            for item in list(planning_brief.get("resource_gap_topics") or [])
            if str(item).strip()
        ]
        resource_strategy = str(meta.get("resource_strategy") or "").strip()
        adjacent_bridge_cues = self._derive_adjacent_bridge_cues(list(meta.get("adjacent_page_context") or []))
        teacher_spine = dict(meta.get("teacher_narrative_spine") or {})
        normalized_story_substrate = dict(meta.get("story_substrate") or {}) if isinstance(meta.get("story_substrate"), Mapping) else {}
        if not teacher_spine:
            teacher_spine = self._build_teacher_narrative_spine(
                page_brief={
                    "resource_gaps": background_topics,
                    "resource_strategy": resource_strategy,
                },
                story_substrate=normalized_story_substrate,
                focus_label=focus_label,
                adjacent_bridge_cues=adjacent_bridge_cues,
            )
        if adjacent_bridge_cues:
            meta["adjacent_bridge_cues"] = adjacent_bridge_cues
        meta["teacher_narrative_spine"] = teacher_spine
        section_packet_rows = [
            dict(row)
            for row in list(dict(meta.get("tool_enrichment_packet") or {}).get("beat_packets") or [])
            if isinstance(row, Mapping)
        ]
        section_packets_by_beat_id = {
            str(row.get("beat_id") or "").strip(): row
            for row in section_packet_rows
            if str(row.get("beat_id") or "").strip()
        }
        section_packets_by_section: Dict[str, Dict[str, Any]] = {}
        for row in section_packet_rows:
            section_hint = self._infer_section_type_from_tool_objectives(
                [str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()]
            )
            if section_hint and section_hint not in section_packets_by_section:
                section_packets_by_section[section_hint] = row

        def _preferred_section_summary(
            *,
            section_type: str,
            planner_beat_id: str,
            default_summary: str,
            limit: int = 240,
        ) -> str:
            packet = section_packets_by_beat_id.get(planner_beat_id) or section_packets_by_section.get(section_type)
            return self._compose_beat_native_summary(
                beat={"section_type": section_type},
                packet=packet,
                default_summary=default_summary,
                limit=limit,
                prefer_default_if_reader_ready=False,
            )

        teacher_spine = self._overlay_teacher_spine_with_packet_copy(
            teacher_spine=teacher_spine,
            beat_guidance={"packets_by_section": section_packets_by_section},
        )
        meta["teacher_narrative_spine"] = teacher_spine

        anchor_terms = [str(item).strip() for item in list(teacher_spine.get("anchor_terms") or []) if str(item).strip()]
        adjacent_page_continuity = self._build_adjacent_page_continuity_rows(
            adjacent_rows=list(meta.get("adjacent_page_context") or []),
            adjacent_bridge_cues=adjacent_bridge_cues,
        )
        hero_title_fallback = self._compose_segment_grounding_title(
            segment_type="opening",
            target_ids=hero_target_ids,
            target_map=dossier_target_map,
            focus_label=focus_label,
        )
        hero_summary_fallback = self._compose_segment_grounding_copy(
            segment_type="opening",
            target_ids=hero_target_ids,
            target_map=dossier_target_map,
            focus_label=focus_label,
            limit=240,
        )

        preferred_opening = self._clean_excerpt(
            self._sanitize_reader_facing_text(teacher_spine.get("opening"), limit=240),
            limit=240,
        )
        hero_fallback = (
            preferred_opening
            or hero_summary_fallback
            or
            self._clean_excerpt(str(current.get("narrative_goal") or "").strip(), limit=220)
            or self._compose_section_display_summary(
                section_type="hero",
                archetype=page_archetype,
                focus_label=focus_label,
                background_topics=background_topics,
                resource_strategy=resource_strategy,
            )
        )
        hero_summary, hero_secondary = self._repair_reader_visible_summary(
            raw_value=hero.get("display_summary") or hero.get("summary"),
            fallback=hero_fallback,
            section_type="hero",
            anchor_terms=anchor_terms,
            limit=240,
        )
        if hero_summary:
            hero["display_summary"] = hero_summary
            if self._should_use_display_copy_as_primary(
                raw_value=hero.get("summary"),
                display_value=hero_summary,
                section_type="hero",
            ):
                if hero_secondary and hero_secondary != hero_summary:
                    hero_meta = dict(hero.get("meta") or {})
                    hero_meta["secondary_evidence_summary"] = hero_secondary
                    hero["meta"] = hero_meta
                hero["summary"] = hero_summary
        current_hero_title = self._clean_excerpt(
            self._sanitize_reader_facing_text(hero.get("display_title") or hero.get("title"), limit=80),
            limit=80,
        )
        if hero_title_fallback and (
            not current_hero_title
            or current_hero_title in {"开场", "阅读导言"}
            or self._manuscript_title_needs_repair(segment_type="opening", title=current_hero_title)
        ):
            hero["display_title"] = hero_title_fallback
            hero["title"] = hero_title_fallback
        current_subtitle = self._clean_excerpt(
            self._sanitize_reader_facing_text(hero.get("display_subtitle") or hero.get("subtitle"), limit=180),
            limit=180,
        )
        if (preferred_opening or hero_summary_fallback) and (
            not current_subtitle
            or self._looks_like_generic_helper_summary(current_subtitle)
            or self._is_generic_narrative_summary(current_subtitle)
            or self._manuscript_lacks_grounded_substance(
                segment_type="opening",
                title=current_hero_title or hero_title_fallback,
                text=current_subtitle,
            )
        ):
            subtitle_fallback = preferred_opening or hero_summary_fallback
            hero["display_subtitle"] = subtitle_fallback
            hero["subtitle"] = subtitle_fallback
        current_summary = self._clean_excerpt(
            self._sanitize_reader_facing_text(hero.get("display_summary") or hero.get("summary"), limit=240),
            limit=240,
        )
        if (preferred_opening or hero_summary_fallback) and self._manuscript_lacks_grounded_substance(
            segment_type="opening",
            title=current_hero_title or hero_title_fallback,
            text=current_summary,
        ):
            summary_fallback = preferred_opening or hero_summary_fallback
            hero["display_summary"] = summary_fallback
            hero["summary"] = summary_fallback
            current_summary = summary_fallback
        if (
            current_subtitle
            and not self._manuscript_lacks_grounded_substance(
                segment_type="opening",
                title=current_hero_title or hero_title_fallback,
                text=current_subtitle,
            )
            and self._manuscript_lacks_grounded_substance(
                segment_type="opening",
                title=current_hero_title or hero_title_fallback,
                text=current_summary,
            )
        ):
            hero["display_summary"] = current_subtitle
            hero["summary"] = current_subtitle
        current["hero"] = hero

        first_adjacent_cue = dict(adjacent_bridge_cues[0]) if adjacent_bridge_cues else {}
        first_adjacent_text = str(first_adjacent_cue.get("text") or "").strip()
        readable_claim = next(
            (
                self._clean_excerpt(str(item.get("display_text") or item.get("text") or "").strip(), limit=120)
                for item in list(normalized_story_substrate.get("main_claims") or [])
                if isinstance(item, Mapping)
                and str(item.get("display_text") or item.get("text") or "").strip()
                and not self._is_english_heavy_text(str(item.get("display_text") or item.get("text") or "").strip())
            ),
            "",
        )
        cue_summary = (
            self._compose_adjacent_reading_flow_summary(
                first_adjacent_text,
                focus_label=focus_label,
                readable_claim=readable_claim,
                anchor_terms=anchor_terms,
            )
            if first_adjacent_text else ""
        )

        normalized_sections: List[Dict[str, Any]] = []
        for row in list(current.get("main_sections") or []):
            if not isinstance(row, Mapping):
                continue
            section = dict(row)
            section_type = str(section.get("section_type") or "").strip()
            fallback_summary = self._compose_section_display_summary(
                section_type=section_type,
                archetype=page_archetype,
                focus_label=focus_label,
                background_topics=background_topics,
                resource_strategy=resource_strategy,
            )
            if section_type == "hero":
                fallback_summary = str(teacher_spine.get("opening") or "").strip() or fallback_summary
            elif section_type == "focus_stage":
                fallback_summary = str(teacher_spine.get("focus_guidance") or "").strip() or fallback_summary
            if section_type == "reading_flow" and cue_summary:
                fallback_summary = cue_summary
            elif section_type == "reading_flow":
                fallback_summary = str(teacher_spine.get("body_guidance") or "").strip() or fallback_summary
            elif section_type == "explainer_cluster":
                fallback_summary = str(teacher_spine.get("term_guidance") or "").strip() or fallback_summary
            elif section_type == "supporting_resources":
                fallback_summary = str(teacher_spine.get("support_guidance") or "").strip() or fallback_summary
            repaired_summary, secondary_summary = self._repair_reader_visible_summary(
                raw_value=section.get("display_summary") or section.get("summary"),
                fallback=fallback_summary,
                section_type=section_type,
                anchor_terms=anchor_terms,
                require_anchor_alignment=section_type in {"explainer_cluster", "supporting_resources"},
                limit=240,
            )
            section_meta = dict(section.get("meta") or {})
            if section_type == "reading_flow":
                section_meta["adjacent_page_continuity"] = adjacent_page_continuity
                if adjacent_bridge_cues:
                    section_meta["adjacent_bridge_cues"] = adjacent_bridge_cues
                else:
                    section_meta.pop("adjacent_bridge_cues", None)
            planner_beat_id = str(section_meta.get("planner_beat_id") or "").strip()
            preferred_summary = _preferred_section_summary(
                section_type=section_type,
                planner_beat_id=planner_beat_id,
                default_summary=fallback_summary,
                limit=240,
            )
            if repaired_summary:
                current_summary = self._clean_excerpt(
                    self._sanitize_reader_facing_text(section.get("display_summary") or section.get("summary"), limit=240),
                    limit=240,
                )
                if preferred_summary and (
                    not current_summary
                    or current_summary == fallback_summary
                    or current_summary == preferred_summary
                    or self._is_generic_narrative_summary(current_summary)
                    or self._looks_like_primary_evidence_dump(current_summary, section_type=section_type)
                    or self._needs_display_localization(current_summary)
                ):
                    repaired_summary = preferred_summary
                if section_type in {"explainer_cluster", "supporting_resources"} or (
                    section_type != "reading_flow"
                    and (
                        not current_summary
                        or self._is_generic_narrative_summary(current_summary)
                        or self._needs_display_localization(current_summary)
                        or self._looks_like_primary_evidence_dump(current_summary, section_type=section_type)
                        or self._looks_like_hype_marketing_copy(current_summary)
                    )
                ) or (
                    section_type == "reading_flow"
                    and (
                        not current_summary
                        or self._is_generic_narrative_summary(current_summary)
                        or self._needs_display_localization(current_summary)
                        or self._looks_like_primary_evidence_dump(current_summary, section_type="reading_flow")
                    )
                ):
                    section["display_summary"] = repaired_summary
                    if self._should_use_display_copy_as_primary(
                        raw_value=section.get("summary"),
                        display_value=repaired_summary,
                        section_type=section_type,
                    ):
                        section["summary"] = repaired_summary
                        if secondary_summary and secondary_summary != repaired_summary:
                            section_meta["secondary_evidence_summary"] = secondary_summary
            section["meta"] = section_meta
            normalized_sections.append(section)
        hero_display_summary = self._clean_excerpt(
            self._sanitize_reader_facing_text(hero.get("display_summary") or hero.get("summary"), limit=240),
            limit=240,
        )
        hero_display_title = self._clean_excerpt(
            self._sanitize_reader_facing_text(hero.get("display_title") or hero.get("title"), limit=120),
            limit=120,
        )
        if hero_display_summary or hero_display_title:
            for section in normalized_sections:
                if str(section.get("section_type") or "").strip() != "hero":
                    continue
                if hero_display_title:
                    section["title"] = hero_display_title
                    section["display_title"] = hero_display_title
                section_hero_summary = preferred_opening or hero_display_summary
                if section_hero_summary:
                    section["summary"] = section_hero_summary
                    section["display_summary"] = section_hero_summary
                break
        current["main_sections"] = normalized_sections

        normalized_beats: List[Dict[str, Any]] = []
        applied_adjacent_cue = False
        for row in list(current.get("guided_beats") or []):
            if not isinstance(row, Mapping):
                continue
            beat = dict(row)
            section_type = str(beat.get("section_type_hint") or "").strip() or "reading_flow"
            fallback_summary = self._compose_section_display_summary(
                section_type=section_type,
                archetype=page_archetype,
                focus_label=focus_label,
                background_topics=background_topics,
                resource_strategy=resource_strategy,
            )
            if section_type == "focus_stage":
                fallback_summary = str(teacher_spine.get("focus_guidance") or "").strip() or fallback_summary
            if str(beat.get("beat_type") or "").strip() == "body_segment" and cue_summary and not applied_adjacent_cue:
                fallback_summary = cue_summary
            elif str(beat.get("beat_type") or "").strip() == "body_segment":
                fallback_summary = str(teacher_spine.get("body_guidance") or "").strip() or fallback_summary
            elif section_type == "explainer_cluster":
                fallback_summary = str(teacher_spine.get("term_guidance") or "").strip() or fallback_summary
            elif section_type == "supporting_resources":
                fallback_summary = str(teacher_spine.get("support_guidance") or "").strip() or fallback_summary
            repaired_summary, secondary_summary = self._repair_reader_visible_summary(
                raw_value=beat.get("display_summary") or beat.get("summary"),
                fallback=fallback_summary,
                section_type=section_type,
                anchor_terms=anchor_terms,
                require_anchor_alignment=section_type in {"explainer_cluster", "supporting_resources"},
                limit=240,
            )
            if repaired_summary:
                beat["display_summary"] = repaired_summary
                if self._should_use_display_copy_as_primary(
                    raw_value=beat.get("summary"),
                    display_value=repaired_summary,
                    section_type=section_type,
                ):
                    beat["summary"] = repaired_summary
                    if secondary_summary and secondary_summary != repaired_summary:
                        beat_meta = dict(beat.get("meta") or {})
                        beat_meta["secondary_evidence_summary"] = secondary_summary
                        beat["meta"] = beat_meta
            continuity_note = self._clean_excerpt(
                self._sanitize_reader_facing_text(beat.get("continuity_note"), limit=180),
                limit=180,
            )
            if (
                not continuity_note
                or self._is_generic_narrative_summary(continuity_note)
                or self._looks_like_generic_helper_summary(continuity_note)
                or self._looks_like_internal_planner_copy(continuity_note)
                or self._needs_display_localization(continuity_note)
                or self._looks_like_primary_evidence_dump(continuity_note, section_type=section_type)
            ):
                continuity_note = ""
            beat["continuity_note"] = continuity_note
            if str(beat.get("beat_type") or "").strip() == "body_segment" and first_adjacent_text and not applied_adjacent_cue:
                if not continuity_note:
                    beat["continuity_note"] = self._compose_adjacent_bridge_note(first_adjacent_text)
                beat_meta = dict(beat.get("meta") or {})
                beat_meta["adjacent_bridge_cue"] = first_adjacent_cue
                beat["meta"] = beat_meta
                applied_adjacent_cue = True
            normalized_beats.append(beat)
        section_summary_by_type = {
            str(row.get("section_type") or "").strip(): self._clean_excerpt(
                self._sanitize_reader_facing_text(row.get("display_summary") or row.get("summary"), limit=240),
                limit=240,
            )
            for row in normalized_sections
            if isinstance(row, Mapping) and str(row.get("section_type") or "").strip()
        }
        for beat in normalized_beats:
            beat_type = str(beat.get("beat_type") or "").strip()
            section_type = str(beat.get("section_type_hint") or "").strip() or "reading_flow"
            section_summary = str(section_summary_by_type.get(section_type) or "").strip()
            if not section_summary or beat_type == "guide_intro":
                continue
            if beat_type in {"figure_walkthrough", "body_segment", "term_segment", "support_segment"}:
                beat["display_summary"] = section_summary
                if self._should_use_display_copy_as_primary(
                    raw_value=beat.get("summary"),
                    display_value=section_summary,
                    section_type=section_type,
                ):
                    beat["summary"] = section_summary
        current["guided_beats"] = normalized_beats

        tool_packet = dict(meta.get("tool_enrichment_packet") or {})
        normalized_manuscript = self._normalize_teaching_manuscript(
            manuscript=dict(current.get("teaching_manuscript") or tool_packet.get("teaching_manuscript") or {}),
            teacher_spine=teacher_spine,
            focus_label=focus_label,
            anchor_terms=anchor_terms,
            adjacent_bridge_cues=adjacent_bridge_cues,
            status=str(current.get("status") or "done").strip() or "done",
        )
        normalized_manuscript = self._polish_teaching_manuscript_with_dossier(
            manuscript=normalized_manuscript or {},
            page_dossier=dict(meta.get("page_dossier") or {}),
            adjacent_page_context=list(meta.get("adjacent_page_context") or []),
            focus_label=focus_label,
        )
        if self._teaching_manuscript_needs_upgrade(
            manuscript=normalized_manuscript,
            adjacent_bridge_cues=adjacent_bridge_cues,
            adjacent_page_context=list(meta.get("adjacent_page_context") or []),
            resource_modules=list(current.get("supporting_resources") or []),
            interaction_modules=list(current.get("interactive_blocks") or []),
            tool_enrichment_packet=tool_packet,
        ) and dossier_target_map:
            rebuilt_manuscript = self._build_teaching_manuscript(
                status=str(current.get("status") or "done").strip() or "done",
                target_map=dossier_target_map,
                story_substrate=normalized_story_substrate,
                page_brief=dict(meta.get("page_brief") or {}),
                teacher_spine=teacher_spine,
                adjacent_bridge_cues=adjacent_bridge_cues,
                tool_enrichment_packet=tool_packet,
                resource_modules=list(current.get("supporting_resources") or []),
                interaction_modules=list(current.get("interactive_blocks") or []),
                focus_label=focus_label,
            )
            rebuilt_manuscript = self._polish_teaching_manuscript_with_dossier(
                manuscript=rebuilt_manuscript,
                page_dossier=dict(meta.get("page_dossier") or {}),
                adjacent_page_context=list(meta.get("adjacent_page_context") or []),
                focus_label=focus_label,
            )
            if rebuilt_manuscript.get("segments"):
                normalized_manuscript = rebuilt_manuscript
        current["teaching_manuscript"] = normalized_manuscript
        if normalized_manuscript:
            tool_packet["teaching_manuscript"] = normalized_manuscript

        normalized_packets: List[Dict[str, Any]] = []
        for row in list(tool_packet.get("beat_packets") or []):
            if not isinstance(row, Mapping):
                continue
            packet = dict(row)
            packet_section_type, packet_fallback = self._infer_teacher_guidance_for_packet(
                packet=packet,
                teacher_spine=teacher_spine,
            )
            preferred_packet_summary = self._compose_beat_native_summary(
                beat={"section_type": packet_section_type},
                packet=packet,
                default_summary=packet_fallback,
                limit=240,
                prefer_default_if_reader_ready=False,
            )
            repaired_packet_summary, _ = self._repair_reader_visible_summary(
                raw_value=packet.get("summary"),
                fallback=packet_fallback,
                section_type=packet_section_type,
                anchor_terms=anchor_terms,
                require_anchor_alignment=packet_section_type in {
                    "focus_stage",
                    "reading_flow",
                    "explainer_cluster",
                    "supporting_resources",
                },
                limit=240,
            )
            if (
                preferred_packet_summary
                and repaired_packet_summary == packet_fallback
                and preferred_packet_summary != packet_fallback
            ):
                repaired_packet_summary = preferred_packet_summary
            if repaired_packet_summary:
                packet["summary"] = repaired_packet_summary

            packet_links = self._normalize_public_links(
                [dict(item) for item in list(packet.get("public_links") or []) if isinstance(item, Mapping)],
                limit=3,
            )
            if packet_section_type == "focus_stage":
                packet_links = [
                    link for link in packet_links
                    if "youtube.com" not in str(link.get("domain") or "").lower()
                    and "youtu.be" != str(link.get("domain") or "").lower()
                ]
            packet["public_links"] = packet_links

            cleaned_points: List[str] = []
            for point in list(packet.get("supporting_points") or []):
                normalized_point = self._clean_excerpt(
                    self._sanitize_reader_facing_text(point, limit=220),
                    limit=220,
                )
                if (
                    not normalized_point
                    or self._looks_like_hype_marketing_copy(normalized_point)
                    or self._is_generic_narrative_summary(normalized_point)
                    or self._looks_like_generic_helper_summary(normalized_point)
                    or self._looks_like_internal_planner_copy(normalized_point)
                    or (
                        packet_section_type in {"explainer_cluster", "supporting_resources"}
                        and not self._has_anchor_term_overlap(normalized_point, anchor_terms, min_matches=1)
                    )
                ):
                    continue
                cleaned_points.append(normalized_point)
            packet["supporting_points"] = self._dedupe_strings(cleaned_points, limit=3)
            normalized_packets.append(packet)
        if normalized_packets:
            tool_packet["beat_packets"] = normalized_packets
            meta["tool_enrichment_packet"] = tool_packet

        experience_contract_boundary = {
            "primary_reader_fields": [
                "focus_page",
                "status",
                "layout_variant",
                "hero",
                "main_sections",
                "guided_beats",
                "supporting_resources",
                "interactive_blocks",
                "widget_blocks",
            ],
            "secondary_fallback_fields": [
                "teaching_manuscript.segments",
            ],
            "inspect_only_fields": [
                "page_dossier",
                "story_substrate",
                "page_brief",
                "planning_brief",
                "planner_output",
                "tool_enrichment_packet",
                "runtime_stage_trace",
                "contract_validation",
                "adjacent_page_context",
                "tool_trace",
            ],
        }
        manuscript_artifact = self._build_experience_manuscript_artifact(
            manuscript=normalized_manuscript or {},
            page_dossier=dict(meta.get("page_dossier") or {}),
            adjacent_page_context=list(meta.get("adjacent_page_context") or []),
            paper_id=int(meta.get("paper_id") or 0),
            focus_page=int(current.get("focus_page") or 0),
            reader_profile=str(current.get("reader_profile") or "").strip() or "curious_generalist",
            page_archetype=page_archetype,
            resource_modules=list(current.get("supporting_resources") or []),
            tool_enrichment_packet=tool_packet,
        )
        content_artifact = {
            "contract": "reader_content_v1",
            "render_surface": "/experience",
            "primary_output": "content_units",
            "reader_constraints": {
                "current_page_primary": True,
                "adjacent_pages_bridge_only": True,
                "authoritative_resources_only": True,
                "non_fragment_picker": True,
                "ui_flexible_content_units": True,
                "prefer_natural_explanatory_copy": True,
                "deprioritize_instructional_scaffold_copy": True,
                "planner_and_debug_copy_inspect_only": True,
            },
            "final_content": {
                "hero_ready": bool(dict(current.get("hero") or {})),
                "section_count": len(list(current.get("main_sections") or [])),
                "guided_beat_count": len(list(current.get("guided_beats") or [])),
                "resource_count": len(list(current.get("supporting_resources") or [])),
                "interactive_count": len(list(current.get("interactive_blocks") or [])),
                "widget_count": len(list(current.get("widget_blocks") or [])),
            },
        }
        meta["experience_primary_contract"] = "reader_content_v1"
        meta["experience_secondary_contract"] = "teaching_manuscript_v2"
        meta["experience_contract_boundary"] = experience_contract_boundary
        meta["manuscript_artifact"] = manuscript_artifact
        meta["content_artifact"] = content_artifact
        meta["experience_output"] = {
            "primary_contract": "reader_content_v1",
            "primary_artifact_path": "main_sections",
            "primary_content_paths": [
                "hero",
                "main_sections",
                "guided_beats",
                "supporting_resources",
                "interactive_blocks",
                "widget_blocks",
            ],
            "secondary_contracts": ["guided_beats_v3", "teaching_manuscript.segments"],
            "inspect_only_path": "meta",
        }
        meta["page_dossier"] = dict(meta.get("page_dossier") or {})
        manuscript_stage_rows = [
            self._build_runtime_stage_row(
                stage_id="manuscript_dossier_assembly",
                stage_kind="manuscript",
                status="done",
                summary="Experience manuscript dossier assembled with current-page evidence primary.",
                meta=dict((manuscript_artifact.get("stages") or [{}])[0].get("meta") or {}),
            ),
            self._build_runtime_stage_row(
                stage_id="manuscript_draft",
                stage_kind="manuscript",
                status=str((manuscript_artifact.get("stages") or [{}, {}])[1].get("status") or "done").strip() or "done",
                summary="Reader-facing manuscript draft assembled before critic checks.",
                meta=dict((manuscript_artifact.get("stages") or [{}, {}])[1].get("meta") or {}),
            ),
            self._build_runtime_stage_row(
                stage_id="manuscript_critic",
                stage_kind="manuscript",
                status=str((manuscript_artifact.get("stages") or [{}, {}, {}])[2].get("status") or "done").strip() or "done",
                summary="Manuscript critic findings captured for inspect-only workbench review.",
                meta=dict((manuscript_artifact.get("stages") or [{}, {}, {}])[2].get("meta") or {}),
            ),
            self._build_runtime_stage_row(
                stage_id="manuscript_final",
                stage_kind="manuscript",
                status=str((manuscript_artifact.get("stages") or [{}, {}, {}, {}])[3].get("status") or "done").strip() or "done",
                summary="Reader-facing content stays primary for `/experience`; manuscript remains an inspectable supporting artifact.",
                meta=dict((manuscript_artifact.get("stages") or [{}, {}, {}, {}])[3].get("meta") or {}),
            ),
        ]
        meta["runtime_stage_trace"] = self._merge_runtime_stage_trace(
            existing_rows=[dict(row) for row in list(meta.get("runtime_stage_trace") or []) if isinstance(row, Mapping)],
            additions=manuscript_stage_rows,
        )

        current["meta"] = meta
        return current

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
        materialized_generative_plan = self._materialize_missing_generative_plan_ids(
            parsed=generative_plan,
            page=int(focus_page),
        )
        materialized_generative_plan = self._apply_beat_native_guidance_to_plan(
            parsed=materialized_generative_plan,
        )
        if isinstance(generative_plan, dict):
            generative_plan.clear()
            generative_plan.update(materialized_generative_plan)
        generative_plan = materialized_generative_plan
        enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
        target_map = self._build_current_page_target_map(
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
        )
        plan_meta = dict((generative_plan or {}).get("meta") or {})
        page_brief = self._restore_page_brief_guided_reading_contract(
            page_brief=dict((generative_plan or {}).get("page_brief") or {}),
            meta=plan_meta,
        )
        page_brief = self._prefer_concrete_target_ids_for_page_brief(
            page_brief=page_brief,
            target_map=target_map,
        )
        fidelity_mode = str(page_brief.get("fidelity_mode") or "").strip() or "light_repair"
        if fidelity_mode not in {"strict", "light_repair", "guided_explainer"}:
            fidelity_mode = "light_repair"
        story_substrate = self._prefer_concrete_target_ids_for_story_substrate(
            story_substrate=dict((generative_plan or {}).get("story_substrate") or {}),
            target_map=target_map,
        )
        readable_claim = next(
            (
                self._clean_excerpt(str(item.get("display_text") or item.get("text") or "").strip(), limit=120)
                for item in list(story_substrate.get("main_claims") or [])
                if isinstance(item, Mapping)
                and str(item.get("display_text") or item.get("text") or "").strip()
                and not self._is_english_heavy_text(str(item.get("display_text") or item.get("text") or "").strip())
            ),
            "",
        )
        resource_modules = self._materialize_resource_display_copy(
            [dict(row) for row in list((generative_plan or {}).get("resource_modules") or []) if isinstance(row, Mapping)],
            page_brief=page_brief,
            target_map=target_map,
        )
        resource_modules = self._sanitize_supporting_resources_for_reader(resource_modules)
        interaction_modules = self._materialize_interaction_display_copy(
            [dict(row) for row in list((generative_plan or {}).get("interaction_modules") or []) if isinstance(row, Mapping)],
            page_brief=page_brief,
        )
        widget_blocks = self._materialize_widget_display_copy(
            [dict(row) for row in list((generative_plan or {}).get("js_widgets") or []) if isinstance(row, Mapping)],
            target_map=target_map,
        )
        rationale = [str(item).strip() for item in list((generative_plan or {}).get("rationale") or []) if str(item).strip()]
        used_tools = [str(item).strip() for item in list((generative_plan or {}).get("used_tools") or []) if str(item).strip()]
        tool_trace = [dict(row) for row in list((generative_plan or {}).get("tool_trace") or []) if isinstance(row, Mapping)]
        planner_output = dict(plan_meta.get("planner_output") or {})
        tool_enrichment_packet = dict(plan_meta.get("tool_enrichment_packet") or {})
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
        beat_guidance = self._build_storyboard_beat_guidance(
            storyboard=storyboard,
            planner_output=planner_output,
            tool_enrichment_packet=tool_enrichment_packet,
        )
        storyboard = [dict(row) for row in list(beat_guidance.get("storyboard") or []) if isinstance(row, Mapping)]
        if storyboard:
            reading_path = self._storyboard_to_reading_path(storyboard)
        page_brief_meta = dict(page_brief.get("meta") or {})
        include_story_map = bool(page_brief_meta.get("include_story_map"))
        rationale = self._dedupe_strings(rationale, limit=2)
        adjacent_meta_rows = [
            {
                "page": int(row.get("page") or 0),
                "relation": str(row.get("relation") or "").strip(),
                "summary": str(row.get("summary") or "").strip(),
                "source": str(row.get("source") or "").strip(),
                "reference_only": bool(row.get("reference_only")),
                "figure_count": len([item for item in list(row.get("figures") or []) if isinstance(item, Mapping)]),
                "table_count": len([item for item in list(row.get("tables") or []) if isinstance(item, Mapping)]),
                "equation_count": len([item for item in list(row.get("equations") or []) if isinstance(item, Mapping)]),
                "continuation_hints": [
                    str(item).strip()
                    for item in list(row.get("continuation_hints") or [])[:3]
                    if str(item).strip()
                ],
                "figure_hints": [
                    str(item).strip()
                    for item in (
                        list(row.get("figure_hints") or [])
                        or [
                            (
                                f"{str(item.get('label') or '').strip()}：{str(item.get('description') or '').strip()}"
                                if str(item.get("label") or "").strip() and str(item.get("description") or "").strip()
                                else str(item.get("description") or "").strip()
                            )
                            for item in list(row.get("figures") or [])
                            if isinstance(item, Mapping)
                        ]
                    )[:2]
                    if str(item).strip()
                ],
                "table_hints": [
                    str(item).strip()
                    for item in (
                        list(row.get("table_hints") or [])
                        or [
                            (
                                f"{str(item.get('label') or '').strip()}：{str(item.get('description') or '').strip()}"
                                if str(item.get("label") or "").strip() and str(item.get("description") or "").strip()
                                else str(item.get("description") or "").strip()
                            )
                            for item in list(row.get("tables") or [])
                            if isinstance(item, Mapping)
                        ]
                    )[:2]
                    if str(item).strip()
                ],
                "equation_hints": [
                    str(item).strip()
                    for item in (
                        list(row.get("equation_hints") or [])
                        or [
                            (
                                f"{str(item.get('label') or '').strip()}：{str(item.get('description') or '').strip()}"
                                if str(item.get("label") or "").strip() and str(item.get("description") or "").strip()
                                else str(item.get("description") or "").strip()
                            )
                            for item in list(row.get("equations") or [])
                            if isinstance(item, Mapping)
                        ]
                    )[:2]
                    if str(item).strip()
                ],
            }
            for row in list(plan_meta.get("adjacent_page_context") or [])
            if isinstance(row, Mapping) and int(row.get("page") or 0) > 0
        ]
        adjacent_bridge_cues = self._derive_adjacent_bridge_cues(adjacent_meta_rows)
        adjacent_page_continuity = self._build_adjacent_page_continuity_rows(
            adjacent_rows=adjacent_meta_rows,
            adjacent_bridge_cues=adjacent_bridge_cues,
        )
        compact_experience_tool_trace = self._compact_tool_trace_for_experience(tool_trace, limit=8)
        compact_tool_trace = [dict(row) for row in compact_experience_tool_trace]
        compact_experience_tool_packet = self._compact_tool_enrichment_packet_for_experience(tool_enrichment_packet)
        if adjacent_meta_rows:
            compact_experience_tool_packet["adjacent_page_context"] = [
                dict(row) for row in adjacent_meta_rows[:2]
            ]
        if adjacent_bridge_cues:
            compact_experience_tool_packet["adjacent_bridge_cues"] = [
                dict(row) for row in adjacent_bridge_cues[:2]
            ]
        if adjacent_page_continuity:
            compact_experience_tool_packet["adjacent_page_continuity"] = [
                dict(row) for row in adjacent_page_continuity[:2]
            ]
        planning_brief = dict(plan_meta.get("planning_brief") or {})
        page_dossier = dict(plan_meta.get("page_dossier") or {}) if isinstance(plan_meta.get("page_dossier"), Mapping) else {}
        runtime_stage_trace = [
            dict(row)
            for row in list(plan_meta.get("runtime_stage_trace") or [])
            if isinstance(row, Mapping)
        ]
        runtime_stage_trace.append(
            self._build_runtime_stage_row(
                stage_id="experience_materialization",
                stage_kind="materialization",
                status="done",
                summary="Generative plan converted into an experience page contract.",
                meta={
                    "page_archetype": page_archetype,
                    "resource_module_count": len(resource_modules),
                    "interaction_module_count": len(interaction_modules),
                    "widget_count": len(widget_blocks),
                },
            )
        )

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
        resource_modules = [
            self._align_primary_experience_copy(
                item=row,
                section_type=(
                    "focus_stage"
                    if (
                        str(row.get("module_type") or "").strip() == "FigureExplainPanel"
                        and primary_focus_target_id
                        and primary_focus_target_id in {str(item or "").strip() for item in list(row.get("target_ids") or []) if str(item or "").strip()}
                    )
                    else "supporting_resources"
                ),
                title_limit=120,
                summary_limit=220,
            )
            for row in resource_modules
        ]
        normalized_interactions: List[Dict[str, Any]] = []
        for row in interaction_modules:
            module_type = str(row.get("module_type") or "").strip()
            current = dict(row)
            if module_type == "QuestionStarterPanel":
                current = self._normalize_question_panel_module(
                    module=current,
                    page_brief=page_brief,
                    story_substrate=story_substrate,
                ) or {}
                if not current:
                    continue
            normalized_interactions.append(
                self._align_primary_experience_copy(
                    item=current,
                    section_type="question_lab" if module_type == "QuestionStarterPanel" else "explainer_cluster",
                    title_limit=120,
                    summary_limit=180,
                )
            )
        interaction_modules = normalized_interactions
        question_modules = [
            row for row in interaction_modules
            if str(row.get("module_type") or "").strip() == "QuestionStarterPanel"
        ]
        explainer_modules = [
            row for row in interaction_modules
            if str(row.get("module_type") or "").strip() != "QuestionStarterPanel"
        ]
        widget_blocks = [
            self._align_primary_experience_copy(
                item=row,
                section_type="focus_stage",
                title_limit=120,
                summary_limit=180,
            )
            for row in widget_blocks
        ]
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
        supporting_resource_modules = self._dedupe_supporting_resource_modules(supporting_resource_modules)

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
            or ""
        ).strip()
        hero_target_ids = self._dedupe_strings(
            [primary_focus_target_id, *secondary_support_ids[:1]],
            limit=2,
        )
        teacher_spine = self._build_teacher_narrative_spine(
            page_brief=page_brief,
            story_substrate=story_substrate,
            focus_label=focus_label,
            adjacent_bridge_cues=adjacent_bridge_cues,
        )
        teacher_spine = self._overlay_teacher_spine_with_packet_copy(
            teacher_spine=teacher_spine,
            beat_guidance=beat_guidance,
        )
        hero_title = focus_label or f"论文 {int(paper_id)} 展开阅读"
        page_goal = str(page_brief.get("page_goal") or "").strip()
        top_claims = [row for row in list(story_substrate.get("main_claims") or []) if isinstance(row, Mapping)]
        hero_grounding_title = self._compose_segment_grounding_title(
            segment_type="opening",
            target_ids=hero_target_ids,
            target_map=target_map,
            focus_label=focus_label,
        )
        hero_grounding_copy = self._compose_segment_grounding_copy(
            segment_type="opening",
            target_ids=hero_target_ids,
            target_map=target_map,
            focus_label=focus_label,
            limit=240,
        )
        hero_focus_grounding_copy = self._compose_segment_grounding_copy(
            segment_type="figure",
            target_ids=[primary_focus_target_id] if primary_focus_target_id else hero_target_ids[:1],
            target_map=target_map,
            focus_label=focus_label,
            limit=180,
        )
        lead_beat = dict(
            beat_guidance["beats_by_section"].get("focus_stage")
            or beat_guidance["beats_by_section"].get("reading_flow")
            or {}
        )
        lead_packet = (
            beat_guidance["packets_by_section"].get("focus_stage")
            or beat_guidance["packets_by_section"].get("reading_flow")
        )
        lead_summary = self._compose_beat_native_summary(
            beat=lead_beat,
            packet=lead_packet,
            default_summary="",
            limit=180,
        )
        def _hero_copy_candidate(value: Any, *, limit: int, require_summary: bool = True) -> str:
            clean = self._sanitize_reader_facing_text(value, limit=limit)
            clean = self._clean_excerpt(clean, limit=limit) if clean else ""
            if not clean:
                return ""
            if self._looks_like_internal_planner_copy(clean):
                return ""
            if self._looks_like_reader_instruction_copy(clean, section_type="hero"):
                return ""
            if self._looks_like_outcome_support_copy(clean, section_type="hero"):
                return ""
            if self._needs_display_localization(clean, short_form=limit <= 180):
                return ""
            if require_summary and not self._is_reader_ready_summary(clean):
                return ""
            return clean

        page_goal_copy = _hero_copy_candidate(page_goal, limit=220)
        hero_angle_copy = _hero_copy_candidate(hero_angle, limit=160)
        lead_summary_copy = _hero_copy_candidate(lead_summary, limit=180)
        rationale_copy = next(
            (
                clean for clean in (
                    _hero_copy_candidate(item, limit=240)
                    for item in rationale
                )
                if clean
            ),
            "",
        )
        hero_subtitle = ""
        if hero_angle_copy:
            hero_subtitle = hero_angle_copy
        elif page_goal_copy:
            hero_subtitle = page_goal_copy
        elif str(teacher_spine.get("opening") or "").strip():
            hero_subtitle = str(teacher_spine.get("opening") or "").strip()
        elif lead_summary_copy:
            hero_subtitle = lead_summary_copy
        elif hero_focus_grounding_copy:
            hero_subtitle = hero_focus_grounding_copy
        if not hero_subtitle:
            hero_subtitle = page_goal_copy or str(teacher_spine.get("opening") or "").strip() or "这一页围绕核心比较与关键细节展开解释。"
        hero_summary = (
            page_goal_copy
            or str(teacher_spine.get("opening") or "").strip()
            or hero_angle_copy
            or lead_summary_copy
            or hero_focus_grounding_copy
            or hero_subtitle
        )
        if rationale_copy:
            hero_summary = rationale_copy
        elif str(teacher_spine.get("opening") or "").strip():
            hero_summary = str(teacher_spine.get("opening") or "").strip()
        elif lead_summary_copy:
            hero_summary = lead_summary_copy
        hero_subtitle = _hero_copy_candidate(hero_subtitle, limit=160, require_summary=False) or "这一页围绕核心比较与关键细节展开解释。"
        hero_summary = (
            _hero_copy_candidate(hero_summary, limit=240)
            or str(teacher_spine.get("opening") or "").strip()
            or hero_focus_grounding_copy
            or page_goal_copy
            or str(teacher_spine.get("opening") or "").strip()
            or hero_subtitle
        )
        if hero_grounding_copy and (
            not hero_subtitle
            or self._looks_like_generic_helper_summary(hero_subtitle)
            or self._is_generic_narrative_summary(hero_subtitle)
            or self._looks_like_outcome_support_copy(hero_subtitle, section_type="hero")
            or self._manuscript_surface_needs_repair(segment_type="opening", text=hero_subtitle)
            or self._manuscript_lacks_grounded_substance(segment_type="opening", title=hero_title, text=hero_subtitle)
        ):
            hero_subtitle = hero_focus_grounding_copy or hero_grounding_copy
        if hero_grounding_copy and (
            not hero_summary
            or self._looks_like_generic_helper_summary(hero_summary)
            or self._is_generic_narrative_summary(hero_summary)
            or self._looks_like_outcome_support_copy(hero_summary, section_type="hero")
            or self._manuscript_surface_needs_repair(segment_type="opening", text=hero_summary)
            or self._manuscript_lacks_grounded_substance(segment_type="opening", title=hero_title, text=hero_summary)
        ):
            hero_summary = (
                hero_focus_grounding_copy
                or str(teacher_spine.get("opening") or "").strip()
                or hero_grounding_copy
            )
        if page_archetype == "figure_explainer" and focus_label:
            hero_title = hero_grounding_title or f"{focus_label} 说明了什么"
        elif page_archetype == "methods_decoder":
            hero_title = "这个方法是如何工作的"
        elif page_archetype == "concept_decoder":
            hero_title = "读懂这一页的核心概念"
        elif page_archetype == "finding_digest" and top_claims:
            hero_title = "这一页的关键发现"
        elif hero_grounding_title:
            hero_title = hero_grounding_title

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
        body_flow_target_ids = [str(item).strip() for item in list(page_brief.get("body_flow_target_ids") or []) if str(item).strip()]
        for item in [
            *body_flow_target_ids,
            primary_focus_target_id,
            *secondary_support_ids,
            *section_candidates,
            *story_turn_ids,
        ]:
            token = str(item or "").strip()
            if token and token not in reading_targets:
                reading_targets.append(token)
        if not reading_targets:
            reading_targets = list(target_map.keys())
        deferred_visual_targets = [
            token
            for token in reading_targets
            if str(dict(target_map.get(token) or {}).get("target_kind") or "").strip() in {"figure", "table"}
        ]
        prose_first_targets = [
            token
            for token in reading_targets
            if str(dict(target_map.get(token) or {}).get("target_kind") or "").strip() not in {"figure", "table"}
        ]
        if prose_first_targets:
            reading_targets = prose_first_targets

        if page_archetype == "figure_explainer":
            section_titles = {
                "focus_stage": "拆解这张图",
                "reading_flow": "再看正文怎么解释这些差异",
                "explainer_cluster": "结果里的关键概念",
                "supporting_resources": "补充背景与上下文",
                "question_lab": "接下来值得追问的问题",
            }
        elif page_archetype == "methods_decoder":
            section_titles = {
                "focus_stage": "方法里的关键设置",
                "reading_flow": "正文如何展开方法细节",
                "explainer_cluster": "方法中的核心机制",
                "supporting_resources": "理解方法需要的背景",
                "question_lab": "可以继续验证的方法问题",
            }
        elif page_archetype == "concept_decoder":
            section_titles = {
                "focus_stage": "最能说明问题的例子",
                "reading_flow": "正文如何展开这个概念",
                "explainer_cluster": "概念中的关键含义",
                "supporting_resources": "理解概念需要的背景",
                "question_lab": "可以继续验证的概念问题",
            }
        else:
            section_titles = {
                "focus_stage": "最关键的证据",
                "reading_flow": "正文如何展开这些结果",
                "explainer_cluster": "结果里的关键概念",
                "supporting_resources": "理解结果需要的背景",
                "question_lab": "可以继续验证的问题",
            }
        section_summaries = {}
        for section_type in [
            "hero",
            "focus_stage",
            "reading_flow",
            "explainer_cluster",
            "supporting_resources",
            "question_lab",
        ]:
            default_summary = self._compose_section_display_summary(
                section_type=section_type,
                archetype=page_archetype,
                focus_label=focus_label,
                background_topics=page_brief.get("resource_gaps") or [],
                resource_strategy=resource_strategy,
            )
            if section_type == "hero":
                default_summary = str(teacher_spine.get("opening") or "").strip() or default_summary
            elif section_type == "focus_stage":
                default_summary = str(teacher_spine.get("focus_guidance") or "").strip() or default_summary
            elif section_type == "reading_flow" and adjacent_bridge_cues:
                default_summary = (
                    self._compose_adjacent_reading_flow_summary(
                        self._strip_adjacent_bridge_provenance(str(adjacent_bridge_cues[0].get("text") or "").strip()),
                        focus_label=focus_label,
                        readable_claim=readable_claim,
                        anchor_terms=list(teacher_spine.get("anchor_terms") or []),
                    )
                    or str(teacher_spine.get("body_guidance") or "").strip()
                    or default_summary
                )
            elif section_type == "reading_flow":
                default_summary = str(teacher_spine.get("body_guidance") or "").strip() or default_summary
            elif section_type == "explainer_cluster":
                default_summary = str(teacher_spine.get("term_guidance") or "").strip() or default_summary
            elif section_type == "supporting_resources":
                default_summary = str(teacher_spine.get("support_guidance") or "").strip() or default_summary
            if section_type == "hero" and default_summary:
                section_summaries[section_type] = default_summary
            else:
                section_summaries[section_type] = self._compose_beat_native_summary(
                    beat=beat_guidance["beats_by_section"].get(section_type),
                    packet=beat_guidance["packets_by_section"].get(section_type),
                    default_summary=default_summary,
                    limit=240,
                )

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
                "meta": {
                    "reader_profile": str(reader_profile or "curious_generalist").strip() or "curious_generalist",
                    "content_lane": "main_narrative",
                },
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
                "meta": {
                    "focus_label": focus_label,
                    "content_lane": "current_page_evidence",
                },
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
                "meta": {
                    "preserve_provenance": True,
                    "body_flow_mode": "full_page",
                    "body_flow_target_count": len(reading_targets),
                    "content_lane": "main_narrative",
                    "deferred_visual_target_ids": deferred_visual_targets,
                    "adjacent_bridge_cues": adjacent_bridge_cues,
                    "adjacent_page_continuity": adjacent_page_continuity,
                },
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
                "meta": {"content_lane": "current_page_support"},
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
                "meta": {"content_lane": "curated_external_resources"},
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
                "meta": {"content_lane": "followup"},
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
            beat_id = str(beat.get("beat_id") or "").strip()
            packet = beat_guidance["packets_by_id"].get(beat_id) or beat_guidance["packets_by_section"].get(section_type)
            packet_copy = self._extract_beat_packet_reader_copy(packet)
            beat_title = str(beat.get("title") or "").strip()
            beat_purpose = str(beat.get("purpose") or "").strip()
            beat_reader_goal = str(beat.get("reader_goal") or "").strip()
            beat_continuity = str(beat.get("continuity_note") or "").strip()
            beat_target_ids = [str(item).strip() for item in list(beat.get("target_ids") or []) if str(item).strip()]
            beat_tool_objectives = self._dedupe_strings([str(item).strip() for item in list(beat.get("tool_objectives") or []) if str(item).strip()])
            beat_drop_notes = self._dedupe_strings([str(item).strip() for item in list(beat.get("drop_notes") or []) if str(item).strip()], limit=6)
            beat_block_stack = self._dedupe_strings([str(item).strip() for item in list(beat.get("block_stack") or []) if str(item).strip()])
            section_segment_type = {
                "hero": "opening",
                "focus_stage": "figure",
                "reading_flow": "body",
                "supporting_resources": "wrapup",
            }.get(section_type, "body")
            if beat_title and beat_title not in {
                "完整阅读本页内容",
                "完整阅读方法正文",
                "补充背景与上下文",
                "补充可靠的方法背景",
                "补足外部背景",
                "可靠的补充材料",
            } and not self._manuscript_title_needs_repair(segment_type=section_segment_type, title=beat_title):
                entry["title"] = beat_title
                entry["display_title"] = beat_title
            beat_summary = self._compose_beat_native_summary(
                beat=beat,
                packet=packet,
                default_summary=str(entry.get("display_summary") or entry.get("summary") or "").strip(),
                limit=240,
            )
            if beat_summary:
                entry["summary"] = beat_summary
                entry["display_summary"] = beat_summary
            if beat_purpose or beat_reader_goal or beat_continuity or beat_tool_objectives or beat_drop_notes or beat_block_stack:
                entry_meta = dict(entry.get("meta") or {})
                entry_meta["planner_purpose"] = beat_purpose
                entry_meta["planner_role"] = str(beat.get("role") or "").strip()
                entry_meta["planner_beat_id"] = beat_id
                guided_continuity = self._sanitize_reader_facing_text(beat_continuity, limit=180)
                if guided_continuity and not self._is_reader_surface_noise(guided_continuity):
                    entry_meta["guided_continuity_note"] = guided_continuity
                if beat_tool_objectives:
                    entry_meta["guided_tool_objectives"] = list(beat_tool_objectives)
                if beat_drop_notes:
                    entry_meta["guided_drop_notes"] = list(beat_drop_notes)
                if beat_block_stack:
                    entry_meta["planner_block_stack"] = beat_block_stack
                entry["meta"] = entry_meta
            if beat_target_ids:
                entry["target_ids"] = self._dedupe_strings([*beat_target_ids, *list(entry.get("target_ids") or [])])

        storyboard_sequence = [
            str(row.get("section_type") or "").strip()
            for row in sorted(storyboard, key=lambda row: int(row.get("priority") or 0))
            if str(row.get("section_type") or "").strip() in section_entries
        ]
        ordered_section_types = storyboard_sequence or self._resolve_section_sequence(reading_path, list(section_entries.keys()))
        resolved_reading_path = self._storyboard_to_reading_path(
            [
                {"section_type": section_type}
                for section_type in ordered_section_types
                if section_type in section_entries
            ]
        )
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
        normalized_main_sections = self._normalize_experience_section_blocks(
            sections=main_sections,
            resource_modules=resource_modules,
            interaction_modules=interaction_modules,
            widget_blocks=[*focus_widget_blocks, *question_widget_blocks],
        )
        guided_beats = self._build_guided_beats_from_sections(
            hero={
                "display_title": hero_title,
                "title": hero_title,
                "display_summary": self._prefer_display_copy(hero_summary, page_goal or hero_subtitle, limit=240),
                "summary": hero_summary,
                "target_ids": [primary_focus_target_id] if primary_focus_target_id else [],
            },
            main_sections=normalized_main_sections,
        )
        teaching_manuscript = self._build_teaching_manuscript(
            status=str((generative_plan or {}).get("status") or "done").strip() or "done",
            target_map=target_map,
            story_substrate=story_substrate,
            page_brief=page_brief,
            teacher_spine=teacher_spine,
            adjacent_bridge_cues=adjacent_bridge_cues,
            tool_enrichment_packet=compact_experience_tool_packet,
            resource_modules=resource_modules,
            interaction_modules=interaction_modules,
            focus_label=focus_label,
        )
        if teaching_manuscript.get("segments"):
            compact_experience_tool_packet["teaching_manuscript"] = teaching_manuscript

        return self._validate_experience_plan_contract({
            "version": "v1",
            "status": str((generative_plan or {}).get("status") or "done").strip() or "done",
            "scope": "section" if section_candidates else ("page_focus" if focus_page else "paper"),
            "focus_page": int(focus_page),
            "reader_profile": str(reader_profile or "curious_generalist").strip() or "curious_generalist",
            "layout_variant": layout_variant,
            "fidelity_mode": fidelity_mode,
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
            "main_sections": normalized_main_sections,
            "guided_beats": guided_beats,
            "teaching_manuscript": teaching_manuscript,
            "supporting_resources": resource_modules,
            "interactive_blocks": interaction_modules,
            "widget_blocks": [*focus_widget_blocks, *question_widget_blocks],
            "reading_path": list(dict.fromkeys(resolved_reading_path or reading_path or ["hero_summary", "focus_evidence", "reading_flow", "supporting_resources", "explore_questions"])),
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
                    "display_copy_contract": "display_copy_v3",
                    "teacher_narrative_spine": teacher_spine,
                    "content_budget": content_budget,
                    "storyboard": storyboard,
                    "guided_reading_contract": "guided_beats_v3",
                    "experience_primary_contract": "reader_content_v1",
                    "resource_strategy": resource_strategy,
                    "used_tools": used_tools,
                    "page_dossier": page_dossier,
                    "story_substrate": dict(story_substrate or {}),
                    "page_brief": dict(page_brief or {}),
                    "planner_output": planner_output,
                    "tool_enrichment_packet": compact_experience_tool_packet,
                    "tool_trace": compact_experience_tool_trace,
                    "tool_trace_summary": compact_tool_trace,
                    "adjacent_page_context": adjacent_meta_rows,
                    "adjacent_bridge_cues": adjacent_bridge_cues,
                    "planning_brief": planning_brief,
                    "runtime_stage_trace": runtime_stage_trace,
                    "fidelity_mode": fidelity_mode,
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

    @classmethod
    def _chunk_guided_reading_targets(
        cls,
        target_ids: Sequence[str],
    ) -> List[List[str]]:
        tokens = cls._dedupe_strings([str(item or "").strip() for item in list(target_ids or []) if str(item or "").strip()])
        if not tokens:
            return []
        if len(tokens) <= 3:
            return [tokens]
        if len(tokens) <= 8:
            chunk_size = 3
        elif len(tokens) <= 12:
            chunk_size = 4
        else:
            chunk_size = 5
        return [tokens[index:index + chunk_size] for index in range(0, len(tokens), chunk_size)]

    @classmethod
    def _infer_guided_beat_type_from_section(cls, section_type: str) -> str:
        normalized = str(section_type or "").strip()
        mapping = {
            "hero": "guide_intro",
            "focus_stage": "figure_walkthrough",
            "reading_flow": "body_segment",
            "explainer_cluster": "concept_bridge",
            "supporting_resources": "why_it_matters",
            "question_lab": "checkpoint",
            "story_map": "runtime_notes",
        }
        return mapping.get(normalized, "supporting_beat")

    @classmethod
    def _build_guided_beats_from_sections(
        cls,
        *,
        hero: Mapping[str, Any],
        main_sections: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        def section_meta(section: Mapping[str, Any]) -> Dict[str, Any]:
            return dict(section.get("meta") or {}) if isinstance(section, Mapping) else {}

        def section_reader_goal(section: Mapping[str, Any], default: str) -> str:
            normalized_default = cls._sanitize_reader_facing_text(default, limit=200)
            return normalized_default or default

        def section_continuity(section: Mapping[str, Any], default: str) -> str:
            meta = section_meta(section)
            guided_value = cls._sanitize_reader_facing_text(meta.get("guided_continuity_note"), limit=200)
            if guided_value and not cls._is_reader_surface_noise(guided_value):
                return guided_value
            normalized_default = cls._sanitize_reader_facing_text(default, limit=200)
            return normalized_default or default

        def section_tool_objectives(section: Mapping[str, Any], defaults: Sequence[str]) -> List[str]:
            meta = section_meta(section)
            return cls._dedupe_strings(
                [
                    str(item).strip()
                    for item in list(
                        meta.get("guided_tool_objectives")
                        or meta.get("planner_tool_objectives")
                        or defaults
                        or []
                    )
                    if str(item).strip()
                ]
            )

        def section_drop_notes(section: Mapping[str, Any]) -> List[str]:
            meta = section_meta(section)
            return cls._dedupe_strings(
                [
                    str(item).strip()
                    for item in list(meta.get("guided_drop_notes") or meta.get("planner_drop_notes") or [])
                    if str(item).strip()
                ],
                limit=6,
            )

        sections_by_type = {
            str(section.get("section_type") or "").strip(): dict(section)
            for section in list(main_sections or [])
            if isinstance(section, Mapping) and str(section.get("section_type") or "").strip()
        }
        guided_beats: List[Dict[str, Any]] = []
        importance = 1

        hero_title = str(hero.get("display_title") or hero.get("title") or "").strip()
        hero_summary = str(hero.get("display_summary") or hero.get("summary") or "").strip()
        hero_targets = [str(item).strip() for item in list(hero.get("target_ids") or []) if str(item).strip()]
        if hero_title or hero_summary:
            guided_beats.append({
                "beat_id": "guided_intro",
                "beat_type": "guide_intro",
                "section_type_hint": "hero",
                "title": hero_title or "阅读导言",
                "display_title": hero_title or "阅读导言",
                "summary": hero_summary,
                "display_summary": hero_summary,
                "reader_goal": hero_summary or "先建立这一页的阅读目标和观察顺序。",
                "continuity_note": "",
                "target_ids": hero_targets,
                "tool_objectives": [],
                "block_stack": [],
                "drop_notes": [],
                "importance": importance,
                "meta": {"source_section_id": "hero"},
            })
            importance += 1

        focus_section = sections_by_type.get("focus_stage")
        if focus_section and (
            list(focus_section.get("target_ids") or [])
            or list(focus_section.get("blocks") or [])
        ):
            focus_title = str(focus_section.get("display_title") or focus_section.get("title") or "").strip() or "拆解这张图"
            focus_summary = str(focus_section.get("display_summary") or focus_section.get("summary") or "").strip()
            guided_beats.append({
                "beat_id": str(dict(focus_section.get("meta") or {}).get("planner_beat_id") or "guided_focus").strip() or "guided_focus",
                "beat_type": "figure_walkthrough",
                "section_type_hint": "focus_stage",
                "title": focus_title,
                "display_title": focus_title,
                "summary": focus_summary,
                "display_summary": focus_summary,
                "reader_goal": section_reader_goal(focus_section, "先抓住本页最强的图示或证据，再回到正文读论证。"),
                "continuity_note": section_continuity(focus_section, "这一段是理解全页的抓手，不要求一次读完所有细节。"),
                "target_ids": [str(item).strip() for item in list(focus_section.get("target_ids") or []) if str(item).strip()],
                "tool_objectives": section_tool_objectives(focus_section, ["figure_context"]),
                "block_stack": [dict(item) for item in list(focus_section.get("blocks") or []) if isinstance(item, Mapping)],
                "drop_notes": section_drop_notes(focus_section),
                "importance": importance,
                "meta": {"source_section_id": str(focus_section.get("section_id") or "").strip()},
            })
            importance += 1

        reading_section = sections_by_type.get("reading_flow")
        explainer_section = sections_by_type.get("explainer_cluster")
        resource_section = sections_by_type.get("supporting_resources")
        question_section = sections_by_type.get("question_lab")

        explainer_blocks = [dict(item) for item in list((explainer_section or {}).get("blocks") or []) if isinstance(item, Mapping)]
        resource_blocks = [dict(item) for item in list((resource_section or {}).get("blocks") or []) if isinstance(item, Mapping)]
        question_blocks = [dict(item) for item in list((question_section or {}).get("blocks") or []) if isinstance(item, Mapping)]

        explainer_index = 0
        resource_index = 0

        if reading_section:
            reading_title = str(reading_section.get("display_title") or reading_section.get("title") or "").strip() or "正文如何展开这些结果"
            reading_summary = str(reading_section.get("display_summary") or reading_section.get("summary") or "").strip()
            reading_targets = [str(item).strip() for item in list(reading_section.get("target_ids") or []) if str(item).strip()]
            reading_chunks = cls._chunk_guided_reading_targets(reading_targets)
            total_chunks = len(reading_chunks)
            for index, chunk in enumerate(reading_chunks, start=1):
                title = reading_title if index == 1 else f"{reading_title} · 第 {index} 段"
                summary = reading_summary if index == 1 else "顺着这一段读原文，再用后续解释补足理解负担。"
                guided_beats.append({
                    "beat_id": f"guided_read_{index}",
                    "beat_type": "body_segment",
                    "section_type_hint": "reading_flow",
                    "title": title,
                    "display_title": title,
                    "summary": summary,
                    "display_summary": summary,
                    "reader_goal": section_reader_goal(reading_section, "保留原文主干，先读这一段的论证，再接受必要解释。"),
                    "continuity_note": section_continuity(reading_section, (
                        f"这是正文主干的第 {index}/{total_chunks} 段。"
                        if total_chunks > 1 else
                        "这是当前页正文主干。"
                    )),
                    "target_ids": chunk,
                    "tool_objectives": section_tool_objectives(reading_section, ["continuation_bridge"]),
                    "block_stack": [],
                    "drop_notes": section_drop_notes(reading_section),
                    "importance": importance,
                    "meta": {
                        "source_section_id": str(reading_section.get("section_id") or "").strip(),
                        "chunk_index": index,
                        "chunk_count": total_chunks,
                    },
                })
                importance += 1

                if explainer_index < len(explainer_blocks):
                    beat_title = str((explainer_section or {}).get("display_title") or (explainer_section or {}).get("title") or "").strip() or "只补这里需要的解释"
                    beat_summary = str((explainer_section or {}).get("display_summary") or (explainer_section or {}).get("summary") or "").strip()
                    guided_beats.append({
                        "beat_id": f"guided_explain_{explainer_index + 1}",
                        "beat_type": "concept_bridge",
                        "section_type_hint": "explainer_cluster",
                        "title": beat_title,
                        "display_title": beat_title,
                        "summary": beat_summary,
                        "display_summary": beat_summary,
                        "reader_goal": section_reader_goal(explainer_section or {}, "解释会卡住理解的术语和机制，而不是重复正文。"),
                        "continuity_note": section_continuity(explainer_section or {}, "把刚读过的正文段落变成更容易吸收的解释。"),
                        "target_ids": chunk,
                        "tool_objectives": section_tool_objectives(explainer_section or {}, ["term_explain", "method_background"]),
                        "block_stack": [explainer_blocks[explainer_index]],
                        "drop_notes": section_drop_notes(explainer_section or {}),
                        "importance": importance,
                        "meta": {
                            "source_section_id": str((explainer_section or {}).get("section_id") or "").strip(),
                            "after_chunk": index,
                        },
                    })
                    importance += 1
                    explainer_index += 1

                if resource_index < len(resource_blocks) and index in {1, total_chunks}:
                    beat_title = str((resource_section or {}).get("display_title") or (resource_section or {}).get("title") or "").strip()
                    if beat_title in {"补充背景与上下文", "理解结果需要的背景", "理解这一页需要的背景"}:
                        beat_title = ""
                    beat_title = beat_title or "读到这里再补背景"
                    beat_summary = str((resource_section or {}).get("display_summary") or (resource_section or {}).get("summary") or "").strip()
                    guided_beats.append({
                        "beat_id": f"guided_context_{resource_index + 1}",
                        "beat_type": "why_it_matters",
                        "section_type_hint": "supporting_resources",
                        "title": beat_title,
                        "display_title": beat_title,
                        "summary": beat_summary,
                        "display_summary": beat_summary,
                        "reader_goal": section_reader_goal(resource_section or {}, "补足背景和现实意义，让这一段不只是被读过，而是被理解。"),
                        "continuity_note": section_continuity(resource_section or {}, "这一段引入的是帮助理解当前页的外部背景，不是替代正文的新主线。"),
                        "target_ids": chunk,
                        "tool_objectives": section_tool_objectives(resource_section or {}, ["why_it_matters", "external_comparison"]),
                        "block_stack": [resource_blocks[resource_index]],
                        "drop_notes": section_drop_notes(resource_section or {}),
                        "importance": importance,
                        "meta": {
                            "source_section_id": str((resource_section or {}).get("section_id") or "").strip(),
                            "after_chunk": index,
                        },
                    })
                    importance += 1
                    resource_index += 1

        while explainer_index < len(explainer_blocks):
            guided_beats.append({
                "beat_id": f"guided_explain_{explainer_index + 1}",
                "beat_type": "concept_bridge",
                "section_type_hint": "explainer_cluster",
                "title": str((explainer_section or {}).get("display_title") or (explainer_section or {}).get("title") or "").strip() or "只补这里需要的解释",
                "display_title": str((explainer_section or {}).get("display_title") or (explainer_section or {}).get("title") or "").strip() or "只补这里需要的解释",
                "summary": str((explainer_section or {}).get("display_summary") or (explainer_section or {}).get("summary") or "").strip(),
                "display_summary": str((explainer_section or {}).get("display_summary") or (explainer_section or {}).get("summary") or "").strip(),
                "reader_goal": section_reader_goal(explainer_section or {}, "补充还没织进正文的关键术语解释。"),
                "continuity_note": "",
                "target_ids": [str(item).strip() for item in list((explainer_section or {}).get("target_ids") or []) if str(item).strip()],
                "tool_objectives": section_tool_objectives(explainer_section or {}, ["term_explain", "method_background"]),
                "block_stack": [explainer_blocks[explainer_index]],
                "drop_notes": section_drop_notes(explainer_section or {}),
                "importance": importance,
                "meta": {"source_section_id": str((explainer_section or {}).get("section_id") or "").strip()},
            })
            importance += 1
            explainer_index += 1

        while resource_index < len(resource_blocks):
            guided_beats.append({
                "beat_id": f"guided_context_{resource_index + 1}",
                "beat_type": "context_bridge",
                "section_type_hint": "supporting_resources",
                "title": (
                    (lambda raw: raw if raw not in {"补充背景与上下文", "理解结果需要的背景", "理解这一页需要的背景"} else "")(
                        str((resource_section or {}).get("display_title") or (resource_section or {}).get("title") or "").strip()
                    )
                    or "读到这里再补背景"
                ),
                "display_title": (
                    (lambda raw: raw if raw not in {"补充背景与上下文", "理解结果需要的背景", "理解这一页需要的背景"} else "")(
                        str((resource_section or {}).get("display_title") or (resource_section or {}).get("title") or "").strip()
                    )
                    or "读到这里再补背景"
                ),
                "summary": str((resource_section or {}).get("display_summary") or (resource_section or {}).get("summary") or "").strip(),
                "display_summary": str((resource_section or {}).get("display_summary") or (resource_section or {}).get("summary") or "").strip(),
                "reader_goal": section_reader_goal(resource_section or {}, "补充高价值的外部背景或延伸材料。"),
                "continuity_note": "",
                "target_ids": [str(item).strip() for item in list((resource_section or {}).get("target_ids") or []) if str(item).strip()],
                "tool_objectives": section_tool_objectives(resource_section or {}, ["why_it_matters", "external_comparison"]),
                "block_stack": [resource_blocks[resource_index]],
                "drop_notes": section_drop_notes(resource_section or {}),
                "importance": importance,
                "meta": {"source_section_id": str((resource_section or {}).get("section_id") or "").strip()},
            })
            importance += 1
            resource_index += 1

        if question_section and (list(question_section.get("target_ids") or []) or question_blocks):
            question_title = str(question_section.get("display_title") or question_section.get("title") or "").strip() or "接下来值得追问的问题"
            question_summary = str(question_section.get("display_summary") or question_section.get("summary") or "").strip()
            guided_beats.append({
                "beat_id": str(dict(question_section.get("meta") or {}).get("planner_beat_id") or "guided_checkpoint").strip() or "guided_checkpoint",
                "beat_type": "checkpoint",
                "section_type_hint": "question_lab",
                "title": question_title,
                "display_title": question_title,
                "summary": question_summary,
                "display_summary": question_summary,
                "reader_goal": section_reader_goal(question_section, "这些问题会把当前页的结论转成可继续验证的理解线索。"),
                "continuity_note": section_continuity(question_section, "如果这一页还没有完全消化，这里应该帮助读者停下来整理理解，而不是继续堆内容。"),
                "target_ids": [str(item).strip() for item in list(question_section.get("target_ids") or []) if str(item).strip()],
                "tool_objectives": section_tool_objectives(question_section, []),
                "block_stack": question_blocks,
                "drop_notes": section_drop_notes(question_section),
                "importance": importance,
                "meta": {"source_section_id": str(question_section.get("section_id") or "").strip()},
            })
        return guided_beats

    def _normalize_experience_guided_beats(
        self,
        *,
        guided_beats: Sequence[Mapping[str, Any]],
        hero: Mapping[str, Any],
        sections: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        fallback_beats = self._build_guided_beats_from_sections(
            hero=hero,
            main_sections=sections,
        )
        current_rows = [dict(row) for row in list(guided_beats or []) if isinstance(row, Mapping)]
        if not current_rows:
            return fallback_beats
        normalized: List[Dict[str, Any]] = []
        for index, row in enumerate(current_rows, start=1):
            beat_id = str(row.get("beat_id") or f"guided_beat_{index}").strip() or f"guided_beat_{index}"
            title = str(row.get("display_title") or row.get("title") or "").strip()
            summary = str(row.get("display_summary") or row.get("summary") or "").strip()
            normalized.append({
                "beat_id": beat_id,
                "beat_type": str(row.get("beat_type") or self._infer_guided_beat_type_from_section(str(row.get("section_type_hint") or ""))).strip() or "supporting_beat",
                "section_type_hint": str(row.get("section_type_hint") or "").strip(),
                "title": str(row.get("title") or title).strip(),
                "display_title": title,
                "summary": str(row.get("summary") or summary).strip(),
                "display_summary": summary,
                "reader_goal": str(row.get("reader_goal") or "").strip(),
                "continuity_note": str(row.get("continuity_note") or "").strip(),
                "target_ids": self._dedupe_strings([str(item).strip() for item in list(row.get("target_ids") or []) if str(item).strip()]),
                "tool_objectives": self._dedupe_strings([str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()]),
                "block_stack": [dict(item) for item in list(row.get("block_stack") or []) if isinstance(item, Mapping)],
                "drop_notes": self._dedupe_strings([str(item).strip() for item in list(row.get("drop_notes") or []) if str(item).strip()], limit=6),
                "importance": max(0, int(row.get("importance") or index)),
                "meta": dict(row.get("meta") or {}),
            })
        return normalized or fallback_beats

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
                answer = hero_angle or primary_claim or "这一页围绕主图及其支撑正文展开，主图给出核心比较，正文把它解释成结论。"
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
                answer = primary_claim or hero_angle or "这一页围绕焦点图里的关键差异展开，周围正文会继续解释这些变化为什么重要。"
            answers.append(
                {
                    "question": question,
                    "answer": answer,
                    "confidence": "guided",
                }
            )
        return answers

    @classmethod
    def _normalize_followup_questions(
        cls,
        *,
        questions: Sequence[Any],
        title: str = "",
        summary: str = "",
        limit: int = 4,
    ) -> List[str]:
        blocked = {
            re.sub(r"[\s？?。.!！]+", "", str(value or "").strip().lower())
            for value in (title, summary)
            if str(value or "").strip()
        }
        normalized: List[str] = []
        seen: set[str] = set()
        for raw in list(questions or []):
            question = cls._sanitize_reader_facing_text(raw, limit=140)
            if not question:
                continue
            if cls._needs_display_localization(question):
                continue
            if cls._looks_like_heading_only(question) or cls._is_low_signal_reader_excerpt(question):
                continue
            dedupe_key = re.sub(r"[\s？?。.!！]+", "", question.lower())
            if not dedupe_key or dedupe_key in blocked or dedupe_key in seen:
                continue
            if question[-1] not in {"？", "?"}:
                question = f"{question.rstrip('。.!！?？')}？"
            seen.add(dedupe_key)
            normalized.append(question)
            if len(normalized) >= limit:
                break
        return normalized

    @classmethod
    def _normalize_followup_qa_pairs(
        cls,
        *,
        qa_pairs: Sequence[Mapping[str, Any]],
        limit: int = 4,
    ) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        seen: set[str] = set()
        for row in list(qa_pairs or []):
            if not isinstance(row, Mapping):
                continue
            question = cls._sanitize_reader_facing_text(row.get("question"), limit=140)
            answer = cls._sanitize_reader_facing_text(row.get("answer"), limit=220)
            if not question or not answer:
                continue
            if cls._needs_display_localization(question) or cls._needs_display_localization(answer):
                continue
            if cls._looks_like_heading_only(question) or cls._is_low_signal_reader_excerpt(question):
                continue
            dedupe_key = re.sub(r"[\s？?。.!！]+", "", question.lower())
            if not dedupe_key or dedupe_key in seen:
                continue
            if question[-1] not in {"？", "?"}:
                question = f"{question.rstrip('。.!！?？')}？"
            seen.add(dedupe_key)
            normalized.append(
                {
                    "question": question,
                    "answer": answer,
                    "confidence": str(row.get("confidence") or "guided").strip() or "guided",
                }
            )
            if len(normalized) >= limit:
                break
        return normalized

    def _normalize_question_panel_module(
        self,
        *,
        module: Mapping[str, Any],
        page_brief: Mapping[str, Any],
        story_substrate: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        item = dict(module or {})
        props = dict(item.get("props") or {})
        questions = self._normalize_followup_questions(
            questions=list(props.get("questions") or []),
            title=str(item.get("display_title") or item.get("title") or "").strip(),
            summary=str(item.get("display_summary") or item.get("summary") or "").strip(),
        )
        qa_pairs = self._normalize_followup_qa_pairs(
            qa_pairs=[row for row in list(props.get("qa_pairs") or []) if isinstance(row, Mapping)],
        )
        if questions and not qa_pairs:
            qa_pairs = self._build_question_answer_pairs(
                questions=questions,
                page_brief=page_brief,
                story_substrate=story_substrate,
            )
        if qa_pairs and not questions:
            questions = [str(row.get("question") or "").strip() for row in qa_pairs if str(row.get("question") or "").strip()][:4]
        raw_state = str(
            item.get("state")
            or ((item.get("meta") or {}) if isinstance(item.get("meta"), Mapping) else {}).get("state")
            or ""
        ).strip().lower()
        if not questions and not qa_pairs and raw_state not in {"loading", "partial", "error"}:
            return None
        props["questions"] = questions
        if qa_pairs:
            props["qa_pairs"] = qa_pairs
        else:
            props.pop("qa_pairs", None)
        item["props"] = props
        return item

    @classmethod
    def _align_primary_experience_copy(
        cls,
        *,
        item: Mapping[str, Any],
        section_type: str,
        title_limit: int,
        summary_limit: int,
    ) -> Dict[str, Any]:
        aligned = dict(item or {})
        display_title = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(aligned.get("display_title") or aligned.get("title"), limit=title_limit),
            limit=title_limit,
        )
        raw_title = cls._clean_excerpt(str(aligned.get("title") or "").strip(), limit=title_limit)
        if display_title and (
            not raw_title
            or cls._is_generic_module_title(raw_title)
            or cls._needs_display_localization(raw_title, short_form=True)
        ):
            aligned["title"] = display_title
        if display_title:
            aligned["display_title"] = display_title

        display_summary = cls._clean_excerpt(
            cls._sanitize_reader_facing_text(aligned.get("display_summary") or aligned.get("summary"), limit=summary_limit),
            limit=summary_limit,
        )
        raw_summary = cls._clean_excerpt(str(aligned.get("summary") or "").strip(), limit=summary_limit)
        if display_summary:
            aligned["display_summary"] = display_summary
            if cls._should_use_display_copy_as_primary(
                raw_value=raw_summary,
                display_value=display_summary,
                section_type=section_type,
            ):
                meta = dict(aligned.get("meta") or {})
                if raw_summary and raw_summary != display_summary:
                    meta["secondary_evidence_summary"] = raw_summary
                aligned["summary"] = display_summary
                aligned["meta"] = meta

        if str(aligned.get("widget_type") or "").strip() == "figure-focus-accordion":
            props = dict(aligned.get("props") or {})
            panels: List[Dict[str, Any]] = []
            for row in list(props.get("panels") or []):
                if not isinstance(row, Mapping):
                    continue
                panel = dict(row)
                display_label = cls._clean_excerpt(
                    cls._sanitize_reader_facing_text(panel.get("display_label") or panel.get("label"), limit=80),
                    limit=80,
                )
                raw_label = cls._clean_excerpt(str(panel.get("label") or "").strip(), limit=80)
                if display_label and (
                    not raw_label
                    or cls._needs_display_localization(raw_label, short_form=True)
                    or raw_label.lower().startswith("panel ")
                ):
                    panel["label"] = display_label
                if display_label:
                    panel["display_label"] = display_label
                display_panel_summary = cls._clean_excerpt(
                    cls._sanitize_reader_facing_text(
                        panel.get("display_summary")
                        or panel.get("summary")
                        or cls._describe_figure_focus(str(panel.get("focus") or "").strip(), str(panel.get("label") or "").strip()),
                        limit=220,
                    ),
                    limit=220,
                )
                raw_panel_summary = cls._clean_excerpt(str(panel.get("summary") or "").strip(), limit=220)
                if display_panel_summary:
                    panel["display_summary"] = display_panel_summary
                    if cls._should_use_display_copy_as_primary(
                        raw_value=raw_panel_summary,
                        display_value=display_panel_summary,
                        section_type="focus_stage",
                    ):
                        if raw_panel_summary and raw_panel_summary != display_panel_summary:
                            panel["source_evidence_summary"] = raw_panel_summary
                        panel["summary"] = display_panel_summary
                panels.append(panel)
            if panels:
                props["panels"] = panels
                aligned["props"] = props

        return aligned

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
            summary = cls._sanitize_experience_meta_text(item.get("summary"), limit=220)
            if summary and not cls._needs_display_localization(summary):
                item["summary"] = summary
            else:
                item["summary"] = ""
            links = cls._normalize_public_links(list(item.get("links") or []), limit=3)
            if links:
                item["links"] = links
            else:
                item["links"] = []
            normalized.append(item)
        return normalized

    @classmethod
    def _is_reader_worthy_resource_link(cls, href: str) -> bool:
        token = str(href or "").strip()
        if not token:
            return False
        if cls._resource_domain_score(token) < 60:
            return False
        host = cls._extract_hostname(token)
        path = str(urlparse(token).path or "").strip().lower()
        if host == "doi.org" and re.search(r"\.(?:g|f|t)\d+(?:$|[/?#])", path):
            return False
        return True

    @classmethod
    def _is_generic_support_resource_title(cls, title: str) -> bool:
        token = str(title or "").strip().lower()
        return token in {
            "",
            "related resources",
            "background reference",
            "resource context",
            "延伸资源",
            "读到这里再补的背景",
            "补充背景与上下文",
            "背景补充",
            "理解这一页需要的背景",
        }

    @classmethod
    def _sanitize_supporting_resources_for_reader(
        cls,
        modules: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for row in list(modules or []):
            if not isinstance(row, Mapping):
                continue
            current = dict(row)
            module_id = str(current.get("module_id") or "").strip()
            if not module_id:
                continue
            module_type = str(current.get("module_type") or "").strip()
            raw_links = [dict(link) for link in list(current.get("links") or []) if isinstance(link, Mapping)]
            reader_worthy_links = [
                link for link in raw_links
                if cls._is_reader_worthy_resource_link(str(link.get("href") or ""))
            ]
            current["links"] = reader_worthy_links[:3] if raw_links else []

            if module_type == "FigureSourceCard" and not current["links"]:
                continue

            if module_type == "RelatedResourceCard":
                if raw_links and not reader_worthy_links:
                    continue
                if not current["links"]:
                    source = str(current.get("source") or "").strip().lower()
                    source_quality = str(dict(current.get("meta") or {}).get("source_quality") or "").strip().lower()
                    title = str(current.get("display_title") or current.get("title") or "").strip()
                    summary = cls._clean_excerpt(
                        cls._sanitize_reader_facing_text(
                            current.get("display_summary") or current.get("summary"),
                            limit=220,
                        ),
                        limit=220,
                    )
                    if source_quality == "none" and (
                        source in {"fallback", "seed", "derived_seed"}
                        or cls._is_generic_support_resource_title(title)
                    ) and (
                        not summary
                        or cls._is_generic_narrative_summary(summary)
                        or cls._looks_like_generic_helper_summary(summary)
                        or cls._looks_like_internal_planner_copy(summary)
                    ):
                        continue
                    if source in {"fallback", "seed", "derived_seed"} and cls._is_generic_support_resource_title(title):
                        continue
                    if (
                        not summary
                        or cls._is_generic_narrative_summary(summary)
                        or cls._looks_like_generic_helper_summary(summary)
                        or cls._looks_like_internal_planner_copy(summary)
                    ):
                        continue
            sanitized.append(current)
        return sanitized

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
            reader_worthy_links = [
                row for row in links
                if self._is_reader_worthy_resource_link(str((row or {}).get("href") or ""))
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
                    if reader_worthy_links:
                        trusted_domains = [
                            self._extract_hostname(str((row or {}).get("href") or ""))
                            for row in reader_worthy_links
                        ]
                        first_domain = trusted_domains[0] if trusted_domains else ""
                        if any(domain.endswith("usmle.org") for domain in trusted_domains):
                            item["title"] = "USMLE 官方背景"
                        elif first_domain:
                            item["title"] = "理解结果需要的外部背景"
                        elif section_label:
                            item["title"] = f"{section_label} 的背景补充"
                        else:
                            item["title"] = "可靠背景"
                    elif links:
                        first_domain = self._extract_hostname(str((links[0] or {}).get("href") or ""))
                        if first_domain.endswith("usmle.org"):
                            item["title"] = "USMLE 官方背景"
                        elif first_domain:
                            item["title"] = "理解结果需要的背景"
                        elif section_label:
                            item["title"] = f"{section_label} 需要的背景"
                        else:
                            item["title"] = "理解这一页需要的背景"
                    elif section_label:
                        item["title"] = f"帮助读懂 {section_label} 的背景"
                    else:
                        item["title"] = "理解这一页需要的背景"
                if not summary or summary.lower().startswith("attach a small set"):
                    if reader_worthy_links and resource_strategy:
                        item["summary"] = f"这里只补真正需要的外部背景：{resource_strategy}"
                    elif reader_worthy_links:
                        item["summary"] = "这里只放一到两个可靠来源，补足理解这一页结果所需的背景。"
                    elif archetype == "figure_explainer":
                        item["summary"] = "这组背景资料只负责帮你读懂图里的比较对象和现实含义，不替代正文。"
                    else:
                        item["summary"] = "这组背景资料只负责补一层解释，帮助你把刚读过的内容放回上下文。"

                meta = dict(item.get("meta") or {})
                if reader_worthy_links:
                    meta["source_quality"] = "trusted"
                    item["links"] = reader_worthy_links[:2]
                elif links:
                    continue
                else:
                    meta["source_quality"] = "none"
                item["meta"] = meta
                if not item.get("links") and not str(item.get("summary") or "").strip():
                    continue
            elif module_type == "FigureSourceCard":
                if reader_worthy_links:
                    item["links"] = reader_worthy_links[:2]
                elif links:
                    continue
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
            summary = self._sanitize_experience_meta_text(item.get("display_summary") or item.get("summary"), limit=180)
            if summary and not self._needs_display_localization(summary):
                item["display_summary"] = summary
            else:
                item["display_summary"] = ""
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
        planner_output: Optional[Mapping[str, Any]] = None,
        tool_enrichment_packet: Optional[Mapping[str, Any]] = None,
        planning_brief: Optional[Mapping[str, Any]] = None,
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
        page_dossier: Optional[Mapping[str, Any]] = None,
        runtime_stage_trace: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        packet = dict(tool_enrichment_packet or {})
        links = self._normalize_public_links(
            [dict(item) for item in list(packet.get("public_links") or []) if isinstance(item, Mapping)],
            limit=4,
        ) or self._extract_public_links_from_tool_trace(tool_trace)
        if not links and not list(used_tools or []):
            return None

        recovered = self._build_fallback_plan(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        recovered["status"] = "done"
        if isinstance(planning_brief, Mapping):
            page_brief = dict(recovered.get("page_brief") or {})
            body_flow = [
                str(item).strip()
                for item in list(planning_brief.get("body_flow_target_ids") or [])
                if str(item).strip()
            ]
            if body_flow:
                page_brief["body_flow_target_ids"] = self._dedupe_strings(body_flow)
            beat_seed = [
                dict(item)
                for item in list(planning_brief.get("guided_beat_seed") or [])
                if isinstance(item, Mapping)
            ]
            if beat_seed:
                page_brief["storyboard"] = beat_seed
            recovered["page_brief"] = page_brief

        if isinstance(planner_output, Mapping):
            guided_beats = [
                dict(item)
                for item in list(planner_output.get("guided_beats") or [])
                if isinstance(item, Mapping)
            ]
            if guided_beats:
                page_brief = dict(recovered.get("page_brief") or {})
                page_brief["storyboard"] = guided_beats
                recovered["page_brief"] = page_brief

        beat_packets = [dict(item) for item in list(packet.get("beat_packets") or []) if isinstance(item, Mapping)]
        figure_packet = next(
            (
                row
                for row in beat_packets
                if "figure_context" in {str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()}
            ),
            None,
        )
        context_packet = next(
            (
                row
                for row in beat_packets
                if {str(item).strip() for item in list(row.get("tool_objectives") or []) if str(item).strip()}.intersection(
                    {"why_it_matters", "external_comparison", "method_background", "term_explain"}
                )
            ),
            None,
        )
        figure_summary = self._first_successful_tool_excerpt(
            list((figure_packet or {}).get("tool_findings") or []),
            preferred_tools=["paper_read", "knowledge_search", "web_search"],
            limit=220,
        )
        context_summary = self._first_successful_tool_excerpt(
            list((context_packet or {}).get("tool_findings") or []),
            preferred_tools=["knowledge_search", "web_search", "paper_read"],
            limit=220,
        )
        modules = [row for row in list(recovered.get("resource_modules") or []) if isinstance(row, dict)]
        if modules:
            first_module = modules[0]
            if figure_summary and str(first_module.get("module_type") or "").strip() == "FigureExplainPanel":
                first_module["summary"] = figure_summary
            first_module["links"] = links[:2]
            first_module["source"] = "web" if links else ("tool_trace" if figure_summary else "fallback")
            meta = dict(modules[0].get("meta") or {})
            meta.setdefault("notes", "Recovered from tool trace after agent timeout.")
            modules[0]["meta"] = meta
        if len(modules) > 1:
            second_module = modules[1]
            second_module["links"] = links[2:4]
            if context_summary:
                second_module["summary"] = context_summary
            second_module["source"] = "web" if len(links) > 2 else ("tool_trace" if context_summary else "fallback")
        recovered["resource_modules"] = self._normalize_resource_modules(modules)
        recovered_meta = dict(recovered.get("meta") or {})
        if isinstance(planner_output, Mapping):
            recovered_meta["planner_output"] = dict(planner_output)
        if isinstance(packet, Mapping):
            recovered_meta["tool_enrichment_packet"] = dict(packet)
        recovered["meta"] = recovered_meta
        return self._finalize_plan(
            parsed=recovered,
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
            used_tools=used_tools,
            tool_trace=tool_trace,
            adjacent_page_context=adjacent_page_context,
            page_dossier=page_dossier,
            planning_brief=planning_brief,
            runtime_stage_trace=runtime_stage_trace,
        )

    def _build_agent_prompt(
        self,
        *,
        page: int,
        user_intent: str,
        enrichment_bundle: Mapping[str, Any],
        compose_payload: Mapping[str, Any],
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]] = None,
        page_dossier: Optional[Mapping[str, Any]] = None,
        planning_brief: Optional[Mapping[str, Any]] = None,
        planner_output: Optional[Mapping[str, Any]] = None,
        tool_enrichment_packet: Optional[Mapping[str, Any]] = None,
        allow_tool_choice: bool = True,
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
                "summary": self._clean_excerpt(str(item.get("summary") or "").strip(), limit=400),
                "body_text": self._clean_excerpt(str(item.get("body_text") or "").strip(), limit=1200),
                "figures": [
                    {
                        "label": str(row.get("label") or "").strip(),
                        "description": self._clean_excerpt(str(row.get("description") or "").strip(), limit=240),
                    }
                    for row in list(item.get("figures") or [])
                    if isinstance(row, Mapping)
                ][:4],
                "tables": [
                    {
                        "label": str(row.get("label") or "").strip(),
                        "description": self._clean_excerpt(str(row.get("description") or "").strip(), limit=240),
                    }
                    for row in list(item.get("tables") or [])
                    if isinstance(row, Mapping)
                ][:4],
                "equations": [
                    {
                        "label": str(row.get("label") or "").strip(),
                        "description": self._clean_excerpt(str(row.get("description") or "").strip(), limit=240),
                    }
                    for row in list(item.get("equations") or [])
                    if isinstance(row, Mapping)
                ][:4],
                "continuation_hints": [
                    self._clean_excerpt(str(row or "").strip(), limit=160)
                    for row in list(item.get("continuation_hints") or [])
                    if str(row or "").strip()
                ][:6],
            }
            for item in list(adjacent_page_context or [])
            if isinstance(item, Mapping)
            and (
                str(item.get("summary") or "").strip()
                or str(item.get("body_text") or "").strip()
                or list(item.get("figures") or [])
                or list(item.get("tables") or [])
                or list(item.get("equations") or [])
                or list(item.get("continuation_hints") or [])
            )
        ]
        if allow_tool_choice:
            dossier_payload = dict(page_dossier or {})
            planning_payload = dict(planning_brief or {})
            planner_stage_payload = dict(planner_output or {})
            tool_packet_payload = dict(tool_enrichment_packet or {})
        else:
            adjacent_refs = [
                {
                    "page": int(item.get("page") or 0),
                    "relation": str(item.get("relation") or "").strip(),
                    "summary": self._clean_excerpt(str(item.get("summary") or "").strip(), limit=220),
                    "body_text": self._clean_excerpt(str(item.get("body_text") or "").strip(), limit=420),
                    "figures": list(item.get("figures") or [])[:2],
                    "tables": list(item.get("tables") or [])[:2],
                    "equations": list(item.get("equations") or [])[:2],
                    "continuation_hints": [
                        self._clean_excerpt(str(row or "").strip(), limit=120)
                        for row in list(item.get("continuation_hints") or [])
                        if str(row or "").strip()
                    ][:4],
                }
                for item in adjacent_refs[:2]
            ]
            dossier_payload = self._compact_page_dossier_for_generation(dict(page_dossier or {}))
            planning_payload = self._compact_planning_brief_for_generation(dict(planning_brief or {}))
            planner_stage_payload = self._compact_planner_output_for_generation(dict(planner_output or {}))
            tool_packet_payload = self._compact_tool_enrichment_packet_for_generation(dict(tool_enrichment_packet or {}))
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
                    {
                        "beat_id": "beat_hero",
                        "role": "orient",
                        "section_type": "hero",
                        "title": "开场",
                        "purpose": "Establish the reading goal first.",
                        "reader_goal": "Let the reader know why this page matters before details.",
                        "continuity_note": "Do not overload this opening with every detail.",
                        "target_ids": ["p7:figure_1"],
                        "tool_objectives": [],
                        "block_stack": [],
                        "drop_notes": [],
                        "priority": 1
                    },
                    {
                        "beat_id": "beat_focus",
                        "role": "focus_evidence",
                        "section_type": "focus_stage",
                        "title": "拆解这张图",
                        "purpose": "Use the strongest evidence as the anchor.",
                        "reader_goal": "Explain how to look at the most important figure first.",
                        "continuity_note": "This visual anchor should prepare the reader for the body flow.",
                        "target_ids": ["p7:figure_1"],
                        "tool_objectives": ["figure_context"],
                        "block_stack": ["figure_walkthrough"],
                        "drop_notes": [],
                        "priority": 2
                    },
                    {
                        "beat_id": "beat_read",
                        "role": "read_support",
                        "section_type": "reading_flow",
                        "title": "完整阅读本页内容",
                        "purpose": "Preserve the current-page body flow as the backbone, then enrich around it.",
                        "reader_goal": "Keep the original page body flow intact as the reading spine.",
                        "continuity_note": "Inline explanations should attach to this flow instead of replacing it.",
                        "target_ids": ["p7:paragraph_1"],
                        "tool_objectives": ["continuation_bridge"],
                        "block_stack": [],
                        "drop_notes": [],
                        "priority": 3
                    }
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
        if not allow_tool_choice:
            output_schema = {
                "version": "v1",
                "status": "done",
                "story_substrate": {
                    "main_claims": [{"text": "用中文概括这一页最重要的结论", "source_target_ids": ["p7:g3"]}],
                    "evidence_units": [{"kind": "figure", "title": "Fig 3", "source_target_ids": ["p7:g1"]}],
                    "terms_to_explain": [{"term": "USMLE", "reason": "reader may not know it", "source_target_ids": ["p7:g2"]}],
                },
                "page_brief": {
                    "page_goal": "解释这一页最重要的结果和它背后的含义",
                    "hero_angle": "主图给出了这一页的关键比较，正文再解释这些差异为什么重要",
                    "storyboard": [
                        {
                            "beat_id": "beat_focus",
                            "section_type": "focus_stage",
                            "title": "关键比较",
                            "reader_goal": "图里承载了这一页最关键的比较结果。",
                            "continuity_note": "正文会继续解释这些差异为什么足以支撑结论。",
                            "target_ids": ["p7:g1"],
                            "tool_objectives": ["figure_context"],
                        }
                    ],
                },
                "resource_modules": [
                    {
                        "module_type": "RelatedResourceCard",
                        "title": "外部背景",
                        "summary": "只放真正有帮助的背景说明",
                        "links": [{"label": "USMLE 官方说明", "href": "https://www.usmle.org/"}],
                    }
                ],
                "interaction_modules": [
                    {
                        "module_type": "GlossaryPanel",
                        "title": "关键术语",
                        "props": {"terms": [{"term": "Concordance", "definition": "用中文解释"}]},
                    }
                ],
                "js_widgets": [],
            }
        base_rules = (
            "Design a generative reader enhancement plan for one paper page.\n"
            "Return JSON only.\n"
            "Your job is to infer the page story and author reader-facing display content that can be shown directly in `/experience`.\n"
            "The current page is the main anchor; adjacent pages can only provide bridge cues, and public resources are optional supporting context.\n"
            "The primary reader-facing artifact must stay content-first: complete, displayable teaching units anchored on the current page, not a fragment picker, target patch list, or rigid manuscript.\n"
            "Hard rules:\n"
            "1) You may author or rewrite reader-facing display copy directly, but it must stay faithful to the supplied page evidence and must not invent facts.\n"
            "2) Keep the current page as the primary anchor. Use target ids for grounding/provenance, not as a limit on what kind of reader-facing copy can be written.\n"
            "3) Infer a coherent story_substrate and page_brief before deciding modules. Think in terms of page experience, not a pile of cards.\n"
            "4) Adjacent pages are bridge-only context. Use them to connect the current page naturally, never to replace the current-page narrative.\n"
            "5) Prefer modules that clarify the page: glossary, related public resources, figure explainer, methods explainer, contrast module, question starter.\n"
            "6) Keep metadata/DOI/header/footer out of the plan unless they materially support a resource module.\n"
            "7) If you use tool output, summarize it conservatively and do not invent facts.\n"
            "8) JS widgets should be lightweight interactive modules, not full-page rewrites.\n"
            "9) Focus on reading enhancement, not decoration.\n"
            "10) The final answer must be valid JSON matching the schema example.\n"
        )
        tool_stage_rules = (
            "11) Tools are optional, but the reading experience is the goal: call them whenever they materially improve understanding.\n"
            "12) Use tools intentionally rather than mechanically; do not skip a useful tool just to keep the page minimal.\n"
            "13) Reader-native tools ground the page, while web_search/web_scrape and MCP-backed public-web tools should actively supply authoritative context, explanations, and extension resources for the authored experience.\n"
            "14) If the page references systems such as exams, benchmarks, institutions, training pathways, or evaluation frameworks, strongly consider attaching 1-3 authoritative public resources that help an unfamiliar reader understand the page.\n"
            "15) Prefer official or primary sources for public resources; avoid generic low-value links.\n"
            "16) If you keep an external/public URL in a resource module, use web_scrape when it materially improves confidence in the summary.\n"
            "17) If scrape is unavailable or unnecessary, keep the link but say the summary is search-derived in meta.notes rather than pretending it was deeply read.\n"
            "18) JS widget panels must include reader-facing detail; do not leave figure-focus accordion panels without summaries.\n"
            "19) Keep enrichment proportional, but prioritize completeness and comprehension over compactness; never reduce the page to a few thin cards.\n"
            "20) Do not call the same tool repeatedly for near-duplicate queries; stop once the top targets are grounded enough to draft the JSON.\n"
        )
        page_generation_rules = (
            "11) This is the page-generation stage. No live tools are available now.\n"
            "12) Use planner_output and tool_enrichment_packet as the only enrichment inputs beyond the compose/dossier context.\n"
            "13) Do not invent extra tool calls, URLs, or evidence beyond the supplied inputs.\n"
            "14) Treat planner_output, guided beats, and beat packets as scaffolding for sequencing and emphasis, not as a rigid script.\n"
            "15) Guided beats can suggest structure, but you may consolidate, expand, or rewrite them into better reader-facing units when that improves the page.\n"
            "16) Author reader-facing sections and modules directly for `/experience`: current page first, adjacent bridges only where helpful, external resources only when they materially improve understanding.\n"
            "17) Use tool findings or public links as support inside the authored copy; do not paste raw snippets, planner notes, or source dumps.\n"
            "18) The page should feel like a guided lesson, but the UI can stay modular: let each content unit do one clear job, then connect units naturally.\n"
            "19) JS widget panels must include reader-facing detail; do not leave figure-focus accordion panels without summaries.\n"
            "20) Do not force every explanation into opening/focus/body rhetoric. Reader-facing units can be modular, but titles and summaries must still feel natural and teachable.\n"
            "21) Reader-facing titles, summaries, labels, teaching_text, and adjacent bridges must never expose target_ids, node_ids, slot ids, or planner/debug labels.\n"
            "22) Keep public resources few and authoritative; never turn the page into a link dump.\n"
            "23) Use as few raw evidence slots as necessary. Do not repeat the same figure/body excerpt across multiple beats when one anchor is enough.\n"
            "24) Prefer natural explanatory copy over reading instructions. In primary reader-visible summaries, explain what the page shows, means, or clarifies instead of telling the reader what to look at first, when to return to the body text, or how the UI behaves.\n"
            "25) Treat reader_goal, continuity_note, teaching_manuscript, and other planner/debug scaffolding as inspect-only support for `/workbench`, not wording templates for `/experience`.\n"
        )
        shared_rules = (
            "26) Keep internal reasoning, tool usage, and schema handling in English if helpful, but generate all user-facing copy in Simplified Chinese.\n"
            "27) For reader-visible fields such as titles, summaries, labels, questions, answers, hooks, and explanatory text, reply in Simplified Chinese.\n"
            "28) If adjacent_page_context/page_dossier is provided, use it for continuity, carry-over, and figure/table explanation, but keep the current page as the main narrative anchor.\n"
            "29) When adjacent pages contain figure/table/equation descriptions, turn them into concise bridge cues instead of a second main storyline.\n"
            "30) Treat planning_brief as grounding and sequencing context, not a rigid script. Refine it when better display content is possible.\n"
            "31) Completeness and readability matter more than compactness: preserve the current-page reasoning surface, then layer explanations and resources around it.\n"
            "32) In page_brief.storyboard, populate reader_goal, continuity_note, and tool_objectives for each beat whenever possible.\n"
            "33) Think in guided reading beats as scaffolding, but make the final copy read like authored page content rather than planner output.\n"
            "34) Prioritize comprehension and guidance over compactness: the page should feel like a rich companion lesson, not a compressed summary.\n"
            "35) MCP/tools are not the goal by themselves; use them only as means to make the page richer, clearer, and more teachable.\n"
        )
        prompt_header = base_rules + (tool_stage_rules if allow_tool_choice else page_generation_rules) + shared_rules
        return (
            prompt_header
            + f"user_intent={json.dumps(str(user_intent or '').strip(), ensure_ascii=False)}\n"
            + f"page={int(page)}\n"
            + f"scheme_choice={json.dumps(scheme_choice, ensure_ascii=False)}\n"
            + f"quality_report={json.dumps({'overall': quality_report.get('overall'), 'layout_monotony': quality_report.get('layout_monotony')}, ensure_ascii=False)}\n"
            + f"page_assets={json.dumps(page_assets, ensure_ascii=False)}\n"
            + f"enrichment_targets={json.dumps(compact_targets, ensure_ascii=False)}\n"
            + f"reader_grounding_hints={json.dumps(grounding_hints, ensure_ascii=False)}\n"
            + f"adjacent_page_context={json.dumps(adjacent_refs, ensure_ascii=False)}\n"
            + f"page_dossier={json.dumps(dossier_payload, ensure_ascii=False)}\n"
            + f"planning_brief={json.dumps(planning_payload, ensure_ascii=False)}\n"
            + f"planner_output={json.dumps(planner_stage_payload, ensure_ascii=False)}\n"
            + f"tool_enrichment_packet={json.dumps(tool_packet_payload, ensure_ascii=False)}\n"
            + f"output_schema_example={json.dumps(output_schema, ensure_ascii=False)}\n"
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
        page_dossier: Optional[Mapping[str, Any]] = None,
        planning_brief: Optional[Mapping[str, Any]] = None,
        runtime_stage_trace: Optional[Sequence[Mapping[str, Any]]] = None,
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
        target_map = self._build_current_page_target_map(
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
        )
        parsed["story_substrate"] = self._prefer_concrete_target_ids_for_story_substrate(
            story_substrate=parsed.get("story_substrate") or {},
            target_map=target_map,
        )
        parsed["page_brief"] = self._prefer_concrete_target_ids_for_page_brief(
            page_brief=parsed.get("page_brief") or {},
            target_map=target_map,
        )
        story_substrate = parsed.get("story_substrate") or {}
        page_brief = parsed.get("page_brief") or {}
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
        meta.setdefault("target_count", len(target_map))
        compact_tool_trace = self._compact_tool_trace_for_recovery(tool_trace, limit=8)
        normalized_used_tools = list(dict.fromkeys([str(item).strip() for item in list(used_tools or []) if str(item).strip()]))
        if not compact_tool_trace and normalized_used_tools:
            compact_tool_trace = [{"type": "summary", "tool": tool_name} for tool_name in normalized_used_tools]
        meta.setdefault("used_tools", normalized_used_tools)
        meta.setdefault("tool_trace_summary", compact_tool_trace)
        used_tool_set = set(normalized_used_tools)
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
        resource_strategy = str(page_brief.get("resource_strategy") or "").strip()
        if resource_strategy:
            meta.setdefault("resource_strategy", resource_strategy)
        adjacent_refs = [
            {
                "page": int(item.get("page") or 0),
                "relation": str(item.get("relation") or "").strip(),
                "reference_only": bool(item.get("reference_only")),
                "source": str(item.get("source") or "").strip(),
                "summary": self._sanitize_experience_meta_text(item.get("summary"), limit=180),
                "continuation_hints": self._dedupe_strings(
                    [
                        self._sanitize_experience_meta_text(hint, limit=140)
                        for hint in list(item.get("continuation_hints") or [])
                        if self._sanitize_experience_meta_text(hint, limit=140)
                    ],
                    limit=3,
                ),
                "figure_hints": self._dedupe_strings(
                    [
                        self._sanitize_experience_meta_text(
                            (
                                f"{str(row.get('label') or '').strip()}：{str(row.get('description') or '').strip()}"
                                if str(row.get("label") or "").strip() and str(row.get("description") or "").strip()
                                else str(row.get("description") or "").strip()
                            ),
                            limit=140,
                        )
                        for row in list(item.get("figures") or [])
                        if isinstance(row, Mapping)
                    ],
                    limit=2,
                ),
                "table_hints": self._dedupe_strings(
                    [
                        self._sanitize_experience_meta_text(
                            (
                                f"{str(row.get('label') or '').strip()}：{str(row.get('description') or '').strip()}"
                                if str(row.get("label") or "").strip() and str(row.get("description") or "").strip()
                                else str(row.get("description") or "").strip()
                            ),
                            limit=140,
                        )
                        for row in list(item.get("tables") or [])
                        if isinstance(row, Mapping)
                    ],
                    limit=2,
                ),
                "equation_hints": self._dedupe_strings(
                    [
                        self._sanitize_experience_meta_text(
                            (
                                f"{str(row.get('label') or '').strip()}：{str(row.get('description') or '').strip()}"
                                if str(row.get("label") or "").strip() and str(row.get("description") or "").strip()
                                else str(row.get("description") or "").strip()
                            ),
                            limit=140,
                        )
                        for row in list(item.get("equations") or [])
                        if isinstance(row, Mapping)
                    ],
                    limit=2,
                ),
                "figure_count": len([row for row in list(item.get("figures") or []) if isinstance(row, Mapping)]),
                "table_count": len([row for row in list(item.get("tables") or []) if isinstance(row, Mapping)]),
                "equation_count": len([row for row in list(item.get("equations") or []) if isinstance(row, Mapping)]),
            }
            for item in list(adjacent_page_context or [])
            if isinstance(item, Mapping) and int(item.get("page") or 0) > 0
        ]
        if adjacent_refs:
            meta["adjacent_page_context"] = adjacent_refs
        meta["page_dossier"] = self._compose_page_dossier_from_current_page_targets(
            page=int(page),
            enrichment_bundle=enrichment_bundle,
            compose_payload=compose_payload,
            existing_page_dossier=page_dossier,
        )
        if isinstance(planning_brief, Mapping) and planning_brief:
            meta["planning_brief"] = dict(planning_brief)
            if isinstance(planning_brief.get("tool_budget"), Mapping):
                meta["tool_budget"] = dict(planning_brief.get("tool_budget") or {})
        if runtime_stage_trace:
            meta["runtime_stage_trace"] = [dict(item) for item in list(runtime_stage_trace or []) if isinstance(item, Mapping)]
        meta.setdefault("display_copy_contract", "display_copy_v3")
        if not self._has_meaningful_modules(parsed):
            meta.setdefault("fallback_reason", "empty_module_plan")
        parsed["meta"] = meta
        parsed = self._apply_beat_native_guidance_to_plan(parsed=parsed)
        return self._validate_generative_plan_contract(
            parsed=parsed,
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )

    async def _build_plan_with_agent_core(
        self,
        *,
        user_id: int,
        llm: Any,
        registry: Any,
        allowed_tools: Sequence[str],
        page: int,
        user_intent: str,
        compose_payload: Mapping[str, Any],
        enrichment_bundle: Mapping[str, Any],
        fallback_plan: Dict[str, Any],
        adjacent_page_context: Optional[Sequence[Mapping[str, Any]]],
        page_dossier: Mapping[str, Any],
        planning_brief: Mapping[str, Any],
        runtime_stage_trace: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
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
            page_dossier=page_dossier,
            planning_brief=planning_brief,
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
                runtime_stage_trace.append(
                    self._build_runtime_stage_row(
                        stage_id="agent_generation",
                        stage_kind="agent",
                        status="timeout_recovered",
                        summary="Legacy agent timed out; recovered plan from tool trace.",
                        meta={"used_tools": list(dict.fromkeys(used_tools)), "tool_events": len(tool_trace)},
                    )
                )
                recovered = self._finalize_plan(
                    parsed=recovered,
                    page=int(page),
                    user_intent=user_intent,
                    enrichment_bundle=enrichment_bundle,
                    compose_payload=compose_payload,
                    used_tools=used_tools,
                    tool_trace=tool_trace,
                    adjacent_page_context=adjacent_page_context,
                    page_dossier=page_dossier,
                    planning_brief=planning_brief,
                    runtime_stage_trace=runtime_stage_trace,
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
                adjacent_page_context=adjacent_page_context,
                page_dossier=page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )
            if isinstance(deterministic, dict):
                runtime_stage_trace.append(
                    self._build_runtime_stage_row(
                        stage_id="agent_generation",
                        stage_kind="agent",
                        status="timeout_deterministic_fallback",
                        summary="Legacy agent timed out; deterministic recovery plan used.",
                        meta={"used_tools": list(dict.fromkeys(used_tools)), "tool_events": len(tool_trace)},
                    )
                )
                deterministic_meta = dict(deterministic.get("meta") or {})
                deterministic_meta.pop("fallback_reason", None)
                deterministic_meta.setdefault("recovered_from", "agent_timeout_deterministic")
                deterministic["meta"] = deterministic_meta
                return deterministic
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="agent_generation",
                    stage_kind="agent",
                    status="timeout_fallback",
                    summary="Legacy agent timed out; fallback plan used.",
                    meta={"used_tools": list(dict.fromkeys(used_tools)), "tool_events": len(tool_trace)},
                )
            )
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
                page_dossier=page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[GenerativeReaderAgentRuntime] legacy build_plan failed: {exc}")
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="agent_generation",
                    stage_kind="agent",
                    status="exception_fallback",
                    summary="Legacy agent execution failed; fallback plan used.",
                    meta={"error": str(exc)[:240], "tool_events": len(tool_trace)},
                )
            )
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
                page_dossier=page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        if agent_error_message and not str(answer_text or "").strip():
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="agent_generation",
                    stage_kind="agent",
                    status="error_fallback",
                    summary="Legacy agent returned an error event without a usable plan.",
                    meta={"agent_error_message": agent_error_message[:240], "tool_events": len(tool_trace)},
                )
            )
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
                page_dossier=page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        parsed = self._extract_json_dict(answer_text)
        if not isinstance(parsed, dict):
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="agent_generation",
                    stage_kind="agent",
                    status="invalid_json_fallback",
                    summary="Legacy agent finished without a valid JSON plan; fallback plan used.",
                    meta={"tool_events": len(tool_trace)},
                )
            )
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
                adjacent_page_context=adjacent_page_context,
                page_dossier=page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        runtime_stage_trace.append(
            self._build_runtime_stage_row(
                stage_id="agent_generation",
                stage_kind="agent",
                status="done",
                summary="Legacy agent completed page generation planning.",
                meta={
                    "used_tools": list(dict.fromkeys(used_tools)),
                    "tool_events": len(tool_trace),
                    "has_tool_trace": bool(tool_trace),
                },
            )
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
            page_dossier=page_dossier,
            planning_brief=planning_brief,
            runtime_stage_trace=runtime_stage_trace,
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
        page_dossier: Optional[Sequence[Mapping[str, Any]] | Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        runtime_started_at = asyncio.get_running_loop().time()
        overall_runtime_budget = max(
            75.0,
            min(float(getattr(settings, "generative_reader_agent_timeout_seconds", 120) or 120), 180.0),
        )
        planner_timeout_target = max(
            24.0,
            min(float(getattr(settings, "generative_reader_planner_timeout_seconds", 45) or 45), overall_runtime_budget),
        )
        page_generation_timeout_target = max(
            30.0,
            min(float(getattr(settings, "generative_reader_page_generation_timeout_seconds", 75) or 75), overall_runtime_budget),
        )

        def remaining_runtime_budget(minimum: float, hard_cap: float) -> float:
            elapsed = max(0.0, asyncio.get_running_loop().time() - runtime_started_at)
            remaining = max(minimum, overall_runtime_budget - elapsed)
            return max(minimum, min(remaining, hard_cap))

        enrichment_bundle = dict((compose_payload or {}).get("enrichment_bundle") or {})
        fallback_plan = self._build_fallback_plan(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
        )
        normalized_page_dossier = page_dossier if isinstance(page_dossier, Mapping) else {}
        planning_brief = self._build_planning_brief(
            page=int(page),
            user_intent=user_intent,
            enrichment_bundle=enrichment_bundle,
            page_dossier=normalized_page_dossier,
            adjacent_page_context=adjacent_page_context,
        )
        runtime_stage_trace: List[Dict[str, Any]] = [
            self._build_runtime_stage_row(
                stage_id="dossier_input",
                stage_kind="input",
                status="ready",
                summary="Current page compose payload, dossier, and adjacent-page context are available.",
                meta={
                    "focus_page": int(page),
                    "has_adjacent_page_context": bool(list(adjacent_page_context or [])),
                    "target_count": len(list(enrichment_bundle.get("targets") or [])),
                },
            ),
            self._build_runtime_stage_row(
                stage_id="planning_brief",
                stage_kind="planner_seed",
                status="ready",
                summary=str(planning_brief.get("summary") or "").strip() or "Planning brief prepared.",
                meta={
                    "page_archetype_hint": str(planning_brief.get("page_archetype_hint") or "").strip(),
                    "continuity_mode": str(planning_brief.get("continuity_mode") or "").strip(),
                    "recommended_sections": list(planning_brief.get("recommended_sections") or []),
                    "tool_hints": list(planning_brief.get("tool_hints") or []),
                    "max_tool_requests": int(dict(planning_brief.get("tool_budget") or {}).get("max_tool_requests") or 0),
                    "max_reader_native_requests": int(dict(planning_brief.get("tool_budget") or {}).get("max_reader_native_requests") or 0),
                    "max_public_web_requests": int(dict(planning_brief.get("tool_budget") or {}).get("max_public_web_requests") or 0),
                },
            ),
        ]

        allowed_tools = {str(item).strip() for item in list(allowed_tool_names or []) if str(item).strip()}
        if not allowed_tools:
            allowed_tools = resolve_generative_reader_agent_tool_whitelist()
        if not allowed_tools:
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="planner",
                    stage_kind="planner",
                    status="skipped",
                    summary="No allowed tools resolved; fallback generative plan used.",
                    meta={"reason": "no_allowed_tools"},
                )
            )
            return self._finalize_plan(
                parsed=fallback_plan,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                used_tools=[],
                tool_trace=[],
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        llm = await self._build_llm()
        registry = tool_registry or build_generative_reader_tool_registry(
            user_id=int(user_id),
            allowed_tool_names=sorted(list(allowed_tools)),
        )
        if not self._supports_staged_runtime(llm=llm, registry=registry):
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="planner",
                    stage_kind="planner",
                    status="legacy_bypass",
                    summary="Falling back to legacy agent flow because staged runtime dependencies are unavailable.",
                    meta={
                        "llm_supports_chat": bool(callable(getattr(llm, "chat", None))),
                        "registry_supports_execute": bool(callable(getattr(registry, "execute", None))),
                    },
                )
            )
            return await self._build_plan_with_agent_core(
                user_id=int(user_id),
                llm=llm,
                registry=registry,
                allowed_tools=sorted(list(allowed_tools)),
                page=int(page),
                user_intent=user_intent,
                compose_payload=compose_payload,
                enrichment_bundle=enrichment_bundle,
                fallback_plan=fallback_plan,
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        planner_output: Dict[str, Any]
        planner_timeout = remaining_runtime_budget(min(24.0, planner_timeout_target), planner_timeout_target)
        try:
            planner_prompt = self._build_planner_prompt(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                allowed_tools=sorted(list(allowed_tools)),
            )
            planner_parsed, _planner_resp = await self._run_json_stage(
                llm=llm,
                prompt=planner_prompt,
                system_prompt="You are the planner stage of a production generative-ui runtime. Return valid JSON only.",
                timeout_seconds=planner_timeout,
                max_tokens=1600,
            )
            planner_output = self._normalize_planner_output(
                raw=planner_parsed,
                planning_brief=planning_brief,
                enrichment_bundle=enrichment_bundle,
                allowed_tools=sorted(list(allowed_tools)),
            )
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="planner",
                    stage_kind="planner",
                    status="done" if planner_parsed else "deterministic_repair",
                    summary="Planner stage selected page strategy and tool requests.",
                    meta={
                        "tool_request_count": len(list(planner_output.get("tool_requests") or [])),
                        "section_strategy": list(planner_output.get("section_strategy") or []),
                        "max_tool_requests": int(dict(planner_output.get("tool_budget") or {}).get("max_tool_requests") or 0),
                    },
                )
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning(
                f"[GenerativeReaderAgentRuntime] planner stage failed: {exc.__class__.__name__}: {exc}"
            )
            planner_output = self._normalize_planner_output(
                raw={},
                planning_brief=planning_brief,
                enrichment_bundle=enrichment_bundle,
                allowed_tools=sorted(list(allowed_tools)),
            )
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="planner",
                    stage_kind="planner",
                    status="timeout_fallback",
                    summary="Planner stage timed out; terminal fallback plan used to avoid leaving the page in background refresh.",
                    meta={
                        "error": str(exc)[:240],
                        "tool_request_count": len(list(planner_output.get("tool_requests") or [])),
                    },
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"[GenerativeReaderAgentRuntime] planner stage failed: {exc.__class__.__name__}: {exc}"
            )
            planner_output = self._normalize_planner_output(
                raw={},
                planning_brief=planning_brief,
                enrichment_bundle=enrichment_bundle,
                allowed_tools=sorted(list(allowed_tools)),
            )
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="planner",
                    stage_kind="planner",
                    status="exception_repaired",
                    summary="Planner stage failed; deterministic planner output used.",
                    meta={
                        "error": str(exc)[:240],
                        "tool_request_count": len(list(planner_output.get("tool_requests") or [])),
                    },
                )
            )

        used_tools, tool_trace, tool_enrichment_packet = await self._execute_planner_tool_requests(
            registry=registry,
            planner_output=planner_output,
            allowed_tools=sorted(list(allowed_tools)),
        )
        tool_observation_rows = [
            row for row in list(tool_trace or [])
            if str(row.get("type") or "").strip() == "observation"
        ]
        tool_failures = sum(1 for row in tool_observation_rows if not bool(dict(row.get("data") or {}).get("success")))
        runtime_stage_trace.append(
            self._build_runtime_stage_row(
                stage_id="tool_enricher",
                stage_kind="tool_enricher",
                status="done" if tool_observation_rows and tool_failures == 0 else ("partial" if tool_observation_rows else "skipped"),
                summary="Planner-selected tool requests were executed and compacted for page generation.",
                meta={
                    "requested_tools": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("requested_tool_count") or 0),
                    "planner_requested_tools": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("planner_requested_tool_count") or 0),
                    "backfill_requested_tools": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("backfill_request_count") or 0),
                    "followup_requested_tools": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("followup_request_count") or 0),
                    "total_requested_tools": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("total_requested_tool_count") or 0),
                    "executed_tools": list(dict.fromkeys(used_tools)),
                    "executed_tool_count": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("executed_tool_count") or 0),
                    "executed_requested_tool_count": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("executed_requested_tool_count") or 0),
                    "tool_events": len(tool_trace),
                    "tool_failures": tool_failures,
                    "suppressed_requests": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("suppressed_request_count") or 0),
                    "timeout_count": int(dict(tool_enrichment_packet.get("budget_summary") or {}).get("timeout_count") or 0),
                },
            )
        )

        page_generation_timeout = remaining_runtime_budget(
            min(30.0, page_generation_timeout_target),
            page_generation_timeout_target,
        )
        try:
            generation_prompt = self._build_agent_prompt(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                planner_output=planner_output,
                tool_enrichment_packet=tool_enrichment_packet,
                allow_tool_choice=False,
            )
            parsed, _generation_resp = await self._run_json_stage(
                llm=llm,
                prompt=generation_prompt,
                system_prompt="You are the page-generation stage of a production generative-ui runtime. Return valid JSON only.",
                timeout_seconds=page_generation_timeout,
                max_tokens=min(int(getattr(settings, "llm_max_tokens", 4096) or 4096), 2600),
            )
        except asyncio.TimeoutError:
            recovered = await self._recover_plan_from_tool_trace(
                llm=llm,
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                tool_trace=tool_trace,
            )
            if isinstance(recovered, dict):
                recovered_meta = dict(recovered.get("meta") or {})
                recovered_meta["planner_output"] = planner_output
                recovered_meta["tool_enrichment_packet"] = tool_enrichment_packet
                recovered["meta"] = recovered_meta
                runtime_stage_trace.append(
                    self._build_runtime_stage_row(
                        stage_id="page_generation",
                        stage_kind="page_generation",
                        status="timeout_recovered",
                        summary="Page generation timed out; recovered plan from tool trace.",
                        meta={"used_tools": list(dict.fromkeys(used_tools)), "tool_events": len(tool_trace)},
                    )
                )
                recovered = self._finalize_plan(
                    parsed=recovered,
                    page=int(page),
                    user_intent=user_intent,
                    enrichment_bundle=enrichment_bundle,
                    compose_payload=compose_payload,
                    used_tools=used_tools,
                    tool_trace=tool_trace,
                    adjacent_page_context=adjacent_page_context,
                    page_dossier=normalized_page_dossier,
                    planning_brief=planning_brief,
                    runtime_stage_trace=runtime_stage_trace,
                )
                recovered_meta = dict(recovered.get("meta") or {})
                recovered_meta.pop("fallback_reason", None)
                recovered_meta.setdefault("recovered_from", "page_generation_timeout")
                recovered["meta"] = recovered_meta
                return recovered
            deterministic = self._recover_plan_deterministically(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                tool_trace=tool_trace,
                used_tools=used_tools,
                planner_output=planner_output,
                tool_enrichment_packet=tool_enrichment_packet,
                planning_brief=planning_brief,
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                runtime_stage_trace=runtime_stage_trace,
            )
            if isinstance(deterministic, dict):
                runtime_stage_trace.append(
                    self._build_runtime_stage_row(
                        stage_id="page_generation",
                        stage_kind="page_generation",
                        status="timeout_deterministic_recovery",
                        summary="Page generation timed out; deterministic beat-aware recovery plan used.",
                        meta={"used_tools": list(dict.fromkeys(used_tools)), "tool_events": len(tool_trace)},
                    )
                )
                deterministic_meta = dict(deterministic.get("meta") or {})
                deterministic_meta.pop("fallback_reason", None)
                deterministic_meta.setdefault("recovered_from", "page_generation_timeout_deterministic")
                deterministic["meta"] = deterministic_meta
                return deterministic
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="page_generation",
                    stage_kind="page_generation",
                    status="timeout_fallback",
                    summary="Page generation timed out; fallback plan used.",
                    meta={"used_tools": list(dict.fromkeys(used_tools)), "tool_events": len(tool_trace)},
                )
            )
            fallback_meta = dict(fallback_plan.get("meta") or {})
            fallback_meta["fallback_reason"] = "page_generation_timeout"
            fallback_meta["planner_output"] = planner_output
            fallback_meta["tool_enrichment_packet"] = tool_enrichment_packet
            fallback_plan["meta"] = fallback_meta
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
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[GenerativeReaderAgentRuntime] page generation failed: {exc}")
            deterministic = self._recover_plan_deterministically(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                tool_trace=tool_trace,
                used_tools=used_tools,
                planner_output=planner_output,
                tool_enrichment_packet=tool_enrichment_packet,
                planning_brief=planning_brief,
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                runtime_stage_trace=runtime_stage_trace,
            )
            if isinstance(deterministic, dict):
                runtime_stage_trace.append(
                    self._build_runtime_stage_row(
                        stage_id="page_generation",
                        stage_kind="page_generation",
                        status="exception_deterministic_recovery",
                        summary="Page generation failed; deterministic beat-aware recovery plan used.",
                        meta={"error": str(exc)[:240], "tool_events": len(tool_trace)},
                    )
                )
                deterministic_meta = dict(deterministic.get("meta") or {})
                deterministic_meta.pop("fallback_reason", None)
                deterministic_meta.setdefault("recovered_from", "page_generation_exception_deterministic")
                deterministic["meta"] = deterministic_meta
                return deterministic
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="page_generation",
                    stage_kind="page_generation",
                    status="exception_fallback",
                    summary="Page generation failed; fallback plan used.",
                    meta={"error": str(exc)[:240], "tool_events": len(tool_trace)},
                )
            )
            fallback_meta = dict(fallback_plan.get("meta") or {})
            fallback_meta["fallback_reason"] = "page_generation_exception"
            fallback_meta["planner_output"] = planner_output
            fallback_meta["tool_enrichment_packet"] = tool_enrichment_packet
            fallback_plan["meta"] = fallback_meta
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
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        if not isinstance(parsed, dict):
            deterministic = self._recover_plan_deterministically(
                page=int(page),
                user_intent=user_intent,
                enrichment_bundle=enrichment_bundle,
                compose_payload=compose_payload,
                tool_trace=tool_trace,
                used_tools=used_tools,
                planner_output=planner_output,
                tool_enrichment_packet=tool_enrichment_packet,
                planning_brief=planning_brief,
                adjacent_page_context=adjacent_page_context,
                page_dossier=normalized_page_dossier,
                runtime_stage_trace=runtime_stage_trace,
            )
            if isinstance(deterministic, dict):
                runtime_stage_trace.append(
                    self._build_runtime_stage_row(
                        stage_id="page_generation",
                        stage_kind="page_generation",
                        status="invalid_json_deterministic_recovery",
                        summary="Page generation returned invalid JSON; deterministic beat-aware recovery plan used.",
                        meta={"tool_events": len(tool_trace)},
                    )
                )
                deterministic_meta = dict(deterministic.get("meta") or {})
                deterministic_meta.pop("fallback_reason", None)
                deterministic_meta.setdefault("recovered_from", "page_generation_invalid_json_deterministic")
                deterministic["meta"] = deterministic_meta
                return deterministic
            runtime_stage_trace.append(
                self._build_runtime_stage_row(
                    stage_id="page_generation",
                    stage_kind="page_generation",
                    status="invalid_json_fallback",
                    summary="Page generation finished without valid JSON; fallback plan used.",
                    meta={"tool_events": len(tool_trace)},
                )
            )
            fallback_meta = dict(fallback_plan.get("meta") or {})
            fallback_meta["fallback_reason"] = "page_generation_not_json"
            fallback_meta["planner_output"] = planner_output
            fallback_meta["tool_enrichment_packet"] = tool_enrichment_packet
            fallback_plan["meta"] = fallback_meta
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
                page_dossier=normalized_page_dossier,
                planning_brief=planning_brief,
                runtime_stage_trace=runtime_stage_trace,
            )

        parsed_meta = dict(parsed.get("meta") or {})
        parsed_meta["planner_output"] = planner_output
        parsed_meta["tool_enrichment_packet"] = tool_enrichment_packet
        parsed["meta"] = parsed_meta
        runtime_stage_trace.append(
            self._build_runtime_stage_row(
                stage_id="page_generation",
                stage_kind="page_generation",
                status="done",
                summary="Page generation completed using planner output and enriched tool findings.",
                meta={
                    "used_tools": list(dict.fromkeys(used_tools)),
                    "tool_events": len(tool_trace),
                    "has_public_links": bool(list(tool_enrichment_packet.get("public_links") or [])),
                },
            )
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
            page_dossier=normalized_page_dossier,
            planning_brief=planning_brief,
            runtime_stage_trace=runtime_stage_trace,
        )


_runtime: Optional[GenerativeReaderAgentRuntime] = None


def get_generative_reader_agent_runtime() -> GenerativeReaderAgentRuntime:
    global _runtime
    if _runtime is None:
        _runtime = GenerativeReaderAgentRuntime()
    return _runtime
