from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from app.services.llm_service import LLMService
from app.services.reader_single_agent_controller import parse_json_dict_from_model_text


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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
        "buttons": [item for item in buttons if item][:8],
        "links": links[:8],
        "forms": forms[:4],
        "text_excerpt": text_excerpt,
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
