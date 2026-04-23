from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.llm_service import LLMService
from app.services.reader_single_agent_controller import parse_json_dict_from_model_text


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _score_follow_candidate(*, href: str, text: str, expected_kind: str) -> float:
    href_norm = _normalize_text(href).lower()
    text_norm = _normalize_text(text).lower()
    if not href_norm:
        return -1.0

    score = 0.0
    if href_norm.startswith("#"):
        score -= 2.0
    if href_norm.startswith(("mailto:", "javascript:", "tel:")):
        score -= 4.0
    if any(token in href_norm or token in text_norm for token in ["download", "dataset", "data", "corpus", "weights"]):
        score += 3.5
    if any(token in href_norm or token in text_norm for token in ["docs", "documentation", "readme", "guide", "tutorial"]):
        score += 1.0
    if any(token in href_norm or token in text_norm for token in ["sign in", "login", "register", "account"]):
        score -= 2.5
    if any(token in href_norm or token in text_norm for token in ["license", "terms", "privacy", "contact"]):
        score -= 1.0
    if expected_kind in {"file", "hdf5", "zip", "json", "text"}:
        if re.search(r"\.(zip|gz|tgz|tar|tar\.gz|json|txt|csv|tsv|h5|hdf5|bin|pth|ckpt)(?:[?#].*)?$", href_norm):
            score += 5.0
    if href_norm.startswith(("http://", "https://", "/")):
        score += 0.5
    return score


def extract_html_page_semantics(
    html: str,
    *,
    url: str,
    final_url: str,
    content_type: str,
) -> Dict[str, Any]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    title = _normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    headings = [
        _normalize_text(node.get_text(" ", strip=True))
        for node in soup.find_all(["h1", "h2", "h3"], limit=6)
        if _normalize_text(node.get_text(" ", strip=True))
    ]
    buttons = [
        _normalize_text(node.get_text(" ", strip=True) or node.get("value") or "")
        for node in soup.find_all(["button", "input"], limit=20)
        if str(node.name).lower() == "button"
        or str(node.get("type") or "").lower() in {"submit", "button"}
    ]
    links = []
    for node in soup.find_all("a", limit=20):
        text = _normalize_text(node.get_text(" ", strip=True))
        href = _normalize_text(node.get("href") or "")
        if text or href:
            links.append({"text": text, "href": href})
    forms = []
    for form in soup.find_all("form", limit=5):
        hidden_names: List[str] = []
        hidden_fields: Dict[str, str] = {}
        for field in form.find_all("input", limit=20):
            name = _normalize_text(field.get("name") or "")
            field_type = _normalize_text(field.get("type") or "").lower()
            value = _normalize_text(field.get("value") or "")
            if not name:
                continue
            if field_type == "hidden":
                hidden_names.append(name)
                hidden_fields[name] = value
        forms.append(
            {
                "id": _normalize_text(form.get("id") or ""),
                "action": _normalize_text(form.get("action") or ""),
                "method": _normalize_text(form.get("method") or ""),
                "hidden_field_names": hidden_names,
                "hidden_fields": hidden_fields,
            }
        )

    main_text = _normalize_text(soup.get_text(" ", strip=True))
    text_excerpt = main_text[:1200]
    lowered = f"{title}\n{' '.join(headings)}\n{text_excerpt}".lower()
    signals: List[str] = []
    for needle, signal in [
        ("virus scan warning", "virus_scan_warning"),
        ("can't scan this file", "cant_scan_file"),
        ("too large for google", "too_large_for_google_scan"),
        ("download anyway", "download_anyway"),
        ("sign in", "sign_in"),
        ("login", "login"),
        ("quota exceeded", "quota_exceeded"),
        ("too many users", "quota_too_many_users"),
        ("file not found", "file_not_found"),
        ("does not exist", "file_not_found"),
        ("access denied", "access_denied"),
        ("permission", "permission_gate"),
    ]:
        if needle in lowered:
            signals.append(signal)

    return {
        "url": str(url or ""),
        "final_url": str(final_url or ""),
        "content_type": str(content_type or ""),
        "title": title,
        "headings": headings,
        "buttons": [item for item in buttons if item][:12],
        "links": links[:12],
        "forms": forms[:6],
        "text_excerpt": text_excerpt[:1600],
        "analysis_text_excerpt": main_text[:4000],
        "signals": list(dict.fromkeys(signals)),
    }


def classify_html_page_semantics(semantics: Dict[str, Any]) -> Dict[str, Any]:
    title = _normalize_text(semantics.get("title"))
    text_excerpt = _normalize_text(semantics.get("text_excerpt"))
    buttons = [_normalize_text(item) for item in list(semantics.get("buttons") or [])]
    signals = list(semantics.get("signals") or [])
    lowered = f"{title}\n{text_excerpt}\n{' '.join(buttons)}".lower()
    forms = list(semantics.get("forms") or [])
    hidden_field_names = {
        str(name)
        for form in forms
        for name in list(form.get("hidden_field_names") or [])
        if str(name or "").strip()
    }

    def _result(
        page_kind: str,
        *,
        diagnosis: str,
        suggested_next_action: str,
        confidence: float,
        rationale: str,
    ) -> Dict[str, Any]:
        return {
            "page_kind": page_kind,
            "diagnosis": diagnosis,
            "suggested_next_action": suggested_next_action,
            "confidence": float(confidence),
            "rationale": rationale,
            "signals": signals,
        }

    if "virus_scan_warning" in signals or "download_anyway" in signals:
        if "confirm" in hidden_field_names or any("download-form" == str(form.get("id") or "") for form in forms):
            return _result(
                "download_gate",
                diagnosis="gdrive_confirm_required",
                suggested_next_action="download_with_confirm_cookie_helper",
                confidence=0.99,
                rationale="页面表现为 Google Drive 病毒扫描/下载确认页，包含可继续下载的表单信号。",
            )
    if "quota_exceeded" in signals or "quota_too_many_users" in signals:
        return _result(
            "quota_limited",
            diagnosis="quota_limited",
            suggested_next_action="try_mirror_or_wait",
            confidence=0.98,
            rationale="页面文本明确提示配额或访问次数限制。",
        )
    if "file_not_found" in signals:
        return _result(
            "not_found",
            diagnosis="not_found",
            suggested_next_action="diagnose_official_source_failure",
            confidence=0.98,
            rationale="页面文本明确提示文件不存在。",
        )
    if "access_denied" in signals or "permission_gate" in signals:
        return _result(
            "access_denied",
            diagnosis="access_denied",
            suggested_next_action="diagnose_official_source_failure",
            confidence=0.95,
            rationale="页面文本体现访问受限或权限门禁。",
        )
    if "sign in" in lowered or "login" in lowered:
        return _result(
            "login_required",
            diagnosis="login_required",
            suggested_next_action="requires_authenticated_session",
            confidence=0.95,
            rationale="页面文本体现需要登录或认证后访问。",
        )
    if "documentation" in lowered or "read the docs" in lowered:
        return _result(
            "reference_page",
            diagnosis="html_page",
            suggested_next_action="use_as_reference_page",
            confidence=0.8,
            rationale="页面更像文档/说明页面，不是下载工件。",
        )
    return _result(
        "unknown",
        diagnosis="html_page",
        suggested_next_action="inspect_before_execute",
        confidence=0.2,
        rationale="规则层无法可靠判断该 HTML 页的语义。",
    )


async def classify_html_page_semantics_with_llm(
    semantics: Dict[str, Any],
    *,
    source: str,
) -> Dict[str, Any]:
    llm = LLMService()
    system_prompt = (
        "你是一个下载链接网页分类器。"
        "请根据给定页面摘要，判断该 HTML 页属于哪一种："
        "download_gate, login_required, quota_limited, not_found, access_denied, reference_page, unknown。"
        "必须输出 JSON 对象，字段包括：page_kind, diagnosis, suggested_next_action, confidence, rationale。"
    )
    user_prompt = (
        "请分类下面这个网页摘要：\n"
        f"url: {semantics.get('url')}\n"
        f"final_url: {semantics.get('final_url')}\n"
        f"title: {semantics.get('title')}\n"
        f"headings: {semantics.get('headings')}\n"
        f"buttons: {semantics.get('buttons')}\n"
        f"forms: {semantics.get('forms')}\n"
        f"signals: {semantics.get('signals')}\n"
        f"text_excerpt: {semantics.get('text_excerpt')}\n"
    )
    try:
        response = await llm.chat(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=300,
            source=source,
            extra_body={"reasoning": {"effort": "none"}},
        )
    except Exception:
        return {}
    parsed = await parse_json_dict_from_model_text(str(response.get("content", "") or ""))
    if not isinstance(parsed, dict):
        return {}
    page_kind = _normalize_text(parsed.get("page_kind")).lower()
    diagnosis = _normalize_text(parsed.get("diagnosis")).lower()
    suggested_next_action = _normalize_text(parsed.get("suggested_next_action")).lower()
    rationale = _normalize_text(parsed.get("rationale"))
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not page_kind or not diagnosis or not suggested_next_action:
        return {}
    return {
        "page_kind": page_kind,
        "diagnosis": diagnosis,
        "suggested_next_action": suggested_next_action,
        "confidence": confidence,
        "rationale": rationale,
        "signals": list(semantics.get("signals") or []),
    }


async def analyze_html_page_semantics(
    html: str,
    *,
    url: str,
    final_url: str,
    content_type: str,
    source: str,
) -> Dict[str, Any]:
    semantics = extract_html_page_semantics(
        html,
        url=url,
        final_url=final_url,
        content_type=content_type,
    )
    classification = classify_html_page_semantics(semantics)
    classification_source = "heuristic"
    if str(classification.get("page_kind") or "") == "unknown":
        llm_classification = await classify_html_page_semantics_with_llm(
            semantics,
            source=source,
        )
        if llm_classification:
            classification = llm_classification
            classification_source = "llm"
    return {
        **semantics,
        **classification,
        "classification_source": classification_source,
    }


def _fallback_probe_resolution(
    semantics: Dict[str, Any],
    *,
    expected_kind: str,
) -> Dict[str, Any]:
    normalized_expected = _normalize_text(expected_kind).lower() or "auto"
    page_kind = _normalize_text(semantics.get("page_kind")).lower()
    diagnosis = _normalize_text(semantics.get("diagnosis")).lower()
    suggested_next_action = _normalize_text(semantics.get("suggested_next_action")).lower()
    rationale = _normalize_text(semantics.get("rationale"))
    links = [item for item in list(semantics.get("links") or []) if isinstance(item, dict)]

    if page_kind in {"not_found"}:
        return {
            "resolution": "dead",
            "selected_link_index": None,
            "selected_href": "",
            "selected_text": "",
            "diagnosis": diagnosis or "not_found",
            "suggested_next_action": suggested_next_action or "diagnose_official_source_failure",
            "reason": rationale or "页面文本明确提示资源不存在。",
            "confidence": 0.98,
        }
    if page_kind in {"download_gate", "login_required", "quota_limited", "access_denied"}:
        return {
            "resolution": "blocked",
            "selected_link_index": None,
            "selected_href": "",
            "selected_text": "",
            "diagnosis": diagnosis or page_kind,
            "suggested_next_action": suggested_next_action or "diagnose_official_source_failure",
            "reason": rationale or "页面表现为门页或访问受限页面，需要额外恢复动作。",
            "confidence": 0.9,
        }

    best_index = None
    best_score = 0.0
    for index, item in enumerate(links):
        score = _score_follow_candidate(
            href=str(item.get("href") or ""),
            text=str(item.get("text") or ""),
            expected_kind=str(expected_kind or "auto").strip().lower() or "auto",
        )
        if score > best_score:
            best_score = score
            best_index = index

    if best_index is not None and best_score >= 3.0:
        selected = links[best_index]
        return {
            "resolution": "follow_link",
            "selected_link_index": int(best_index),
            "selected_href": str(selected.get("href") or ""),
            "selected_text": str(selected.get("text") or ""),
            "diagnosis": diagnosis or "follow_candidate_found",
            "suggested_next_action": "follow_selected_link",
            "reason": rationale or "页面包含高置信度下载/数据集/资源链接，适合继续探测。",
            "confidence": 0.72,
        }

    allow_reference_page = page_kind == "reference_page" and normalized_expected in {"auto", "html", "text"}
    return {
        "resolution": "reference_page_ok" if allow_reference_page else "blocked",
        "selected_link_index": None,
        "selected_href": "",
        "selected_text": "",
        "diagnosis": "reference_page_ok" if allow_reference_page else (diagnosis or "html_page"),
        "suggested_next_action": "use_as_reference_page" if allow_reference_page else (suggested_next_action or "inspect_before_execute"),
        "reason": rationale or (
            "页面更适合作为参考页使用。"
            if allow_reference_page
            else "未找到足够明确的后续下载/资源线索。"
        ),
        "confidence": 0.6 if allow_reference_page else 0.35,
    }


async def resolve_html_probe_plan_with_llm(
    html: str,
    *,
    url: str,
    final_url: str,
    content_type: str,
    expected_kind: str,
    source: str,
    semantics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    page_semantics = dict(semantics or {})
    if not page_semantics:
        page_semantics = await analyze_html_page_semantics(
            html,
            url=url,
            final_url=final_url,
            content_type=content_type,
            source=source,
        )
    fallback = _fallback_probe_resolution(page_semantics, expected_kind=expected_kind)
    links = [item for item in list(page_semantics.get("links") or []) if isinstance(item, dict)]
    link_lines = []
    for index, item in enumerate(links):
        link_lines.append(
            {
                "index": index,
                "text": _normalize_text(item.get("text")),
                "href": _normalize_text(item.get("href")),
                "absolute_url": urljoin(str(final_url or url or ""), _normalize_text(item.get("href"))),
            }
        )

    llm = LLMService()
    system_prompt = (
        "你是一个论文复现工作流里的外链探活决策器。"
        "当前页面已经被判定为 HTML，不是直接文件流。"
        "请根据页面摘要决定最终动作："
        "reference_page_ok, follow_link, blocked, dead。"
        "只有在页面明显提供后续下载/数据/仓库入口时才选择 follow_link。"
        "如果 follow_link，必须从给定 links 中选择 selected_link_index。"
        "必须输出 JSON 对象，字段包括：resolution, selected_link_index, diagnosis, suggested_next_action, reason, confidence。"
    )
    user_prompt = (
        "请为下面的 HTML 探活页面做决策。\n"
        f"expected_kind: {expected_kind}\n"
        f"url: {url}\n"
        f"final_url: {final_url}\n"
        f"heuristic_page_kind: {page_semantics.get('page_kind')}\n"
        f"heuristic_diagnosis: {page_semantics.get('diagnosis')}\n"
        f"title: {page_semantics.get('title')}\n"
        f"headings: {page_semantics.get('headings')}\n"
        f"buttons: {page_semantics.get('buttons')}\n"
        f"forms: {page_semantics.get('forms')}\n"
        f"signals: {page_semantics.get('signals')}\n"
        f"text_excerpt: {page_semantics.get('analysis_text_excerpt') or page_semantics.get('text_excerpt')}\n"
        f"links: {link_lines}\n"
        "如果页面本身就是有价值的文档/参考页，可选 reference_page_ok。"
        "如果页面已经明确死掉，选 dead。"
        "如果页面需要登录、确认、配额恢复或其它额外动作，选 blocked。"
    )
    try:
        response = await llm.chat(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=400,
            source=source,
            extra_body={"reasoning": {"effort": "none"}},
        )
    except Exception:
        return {
            **fallback,
            "links": link_lines,
            "source": "fallback",
            "page_semantics": page_semantics,
        }

    parsed = await parse_json_dict_from_model_text(str(response.get("content", "") or ""))
    if not isinstance(parsed, dict):
        return {
            **fallback,
            "links": link_lines,
            "source": "fallback",
            "page_semantics": page_semantics,
        }

    resolution = _normalize_text(parsed.get("resolution")).lower()
    try:
        selected_link_index = int(parsed.get("selected_link_index")) if parsed.get("selected_link_index") is not None else None
    except (TypeError, ValueError):
        selected_link_index = None
    diagnosis = _normalize_text(parsed.get("diagnosis")).lower() or str(fallback.get("diagnosis") or "")
    suggested_next_action = _normalize_text(parsed.get("suggested_next_action")).lower() or str(
        fallback.get("suggested_next_action") or ""
    )
    reason = _normalize_text(parsed.get("reason")) or str(fallback.get("reason") or "")
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if resolution not in {"reference_page_ok", "follow_link", "blocked", "dead"}:
        return {
            **fallback,
            "links": link_lines,
            "source": "fallback",
            "page_semantics": page_semantics,
        }
    if resolution == "follow_link":
        if selected_link_index is None or not (0 <= selected_link_index < len(link_lines)):
            return {
                **fallback,
                "links": link_lines,
                "source": "fallback",
                "page_semantics": page_semantics,
            }
        selected = link_lines[selected_link_index]
        return {
            "resolution": resolution,
            "selected_link_index": selected_link_index,
            "selected_href": str(selected.get("href") or ""),
            "selected_text": str(selected.get("text") or ""),
            "selected_absolute_url": str(selected.get("absolute_url") or ""),
            "diagnosis": diagnosis or "follow_candidate_found",
            "suggested_next_action": suggested_next_action or "follow_selected_link",
            "reason": reason or "模型判定页面包含明确的后续资源链接。",
            "confidence": confidence,
            "links": link_lines,
            "source": "llm",
            "page_semantics": page_semantics,
        }
    return {
        "resolution": resolution,
        "selected_link_index": None,
        "selected_href": "",
        "selected_text": "",
        "selected_absolute_url": "",
        "diagnosis": diagnosis or str(fallback.get("diagnosis") or ""),
        "suggested_next_action": suggested_next_action or str(fallback.get("suggested_next_action") or ""),
        "reason": reason or str(fallback.get("reason") or ""),
        "confidence": confidence,
        "links": link_lines,
        "source": "llm",
        "page_semantics": page_semantics,
    }
