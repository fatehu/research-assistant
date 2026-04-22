from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup


_GOOGLE_DRIVE_HOSTS = {
    "drive.google.com",
    "docs.google.com",
}


def is_google_drive_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower()
    return host in _GOOGLE_DRIVE_HOSTS or host.endswith(".googleusercontent.com")


def extract_google_drive_file_id(url: str) -> Optional[str]:
    parsed = urlparse(str(url or "").strip())
    path = parsed.path or ""
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    file_id = str(query.get("id") or "").strip()
    if file_id:
        return file_id
    match = re.search(r"/file/d/([^/]+)", path)
    if match:
        return match.group(1).strip() or None
    match = re.search(r"/uc\b", path)
    if match:
        file_id = str(query.get("id") or "").strip()
        if file_id:
            return file_id
    return None


def extract_google_drive_confirm_token(
    html: str,
    *,
    cookies: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    cookie_items = dict(cookies or {})
    for key, value in cookie_items.items():
        if str(key).startswith("download_warning") and str(value or "").strip():
            return str(value).strip()

    text = str(html or "")
    patterns = [
        r'confirm=([0-9A-Za-z_-]+)',
        r'name="confirm"\s+value="([0-9A-Za-z_-]+)"',
        r'"confirm"\s*:\s*"([0-9A-Za-z_-]+)"',
        r"confirm=([0-9A-Za-z_-]+)&amp;",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            token = str(match.group(1) or "").strip()
            if token:
                return token
    return None


def extract_google_drive_download_form(html: str) -> tuple[Optional[str], dict[str, str]]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    form = soup.find("form", id="download-form")
    if form is None:
        form = soup.find("form", action=re.compile(r"drive\.usercontent\.google\.com/download"))
    if form is None:
        return None, {}
    action = str(form.get("action") or "").strip() or None
    fields: dict[str, str] = {}
    for field in form.find_all("input", limit=30):
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        fields[name] = str(field.get("value") or "").strip()
    return action, fields


def build_google_drive_confirm_url(url: str, token: str) -> str:
    parsed = urlparse(str(url or "").strip())
    file_id = extract_google_drive_file_id(url)
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_items["confirm"] = str(token or "").strip()
    if file_id:
        query_items["id"] = file_id
        query_items["export"] = query_items.get("export") or "download"
        return urlunparse(
            (
                parsed.scheme or "https",
                "drive.google.com",
                "/uc",
                "",
                urlencode(query_items, doseq=True),
                "",
            )
        )
    return urlunparse(parsed._replace(query=urlencode(query_items, doseq=True)))


async def probe_google_drive_confirm_download(
    *,
    client: httpx.AsyncClient,
    url: str,
    read_bytes: int,
) -> Optional[dict[str, Any]]:
    if not is_google_drive_url(url):
        return None

    try:
        page_response = await client.get(url)
    except Exception:  # noqa: BLE001
        return None

    html = page_response.text or ""
    form_action, form_fields = extract_google_drive_download_form(html)
    token = extract_google_drive_confirm_token(
        html,
        cookies={str(key): str(value) for key, value in client.cookies.items()},
    )
    if not token:
        token = extract_google_drive_confirm_token(
            html,
            cookies={str(key): str(value) for key, value in page_response.cookies.items()},
        )
    if not token:
        token = str(form_fields.get("confirm") or "").strip() or None
    if not token:
        return None

    if form_action:
        parsed_action = urlparse(form_action)
        if parsed_action.scheme and parsed_action.netloc:
            base_url = form_action
        else:
            current = urlparse(str(page_response.url))
            base_url = urlunparse(
                (
                    current.scheme or "https",
                    current.netloc,
                    parsed_action.path or current.path,
                    "",
                    "",
                    "",
                )
            )
        query_items = dict(parse_qsl(urlparse(base_url).query, keep_blank_values=True))
        query_items.update({key: value for key, value in form_fields.items() if str(key or "").strip()})
        query_items["confirm"] = token
        parsed_base = urlparse(base_url)
        confirm_url = urlunparse(
            (
                parsed_base.scheme or "https",
                parsed_base.netloc,
                parsed_base.path or "/download",
                "",
                urlencode(query_items, doseq=True),
                "",
            )
        )
    else:
        confirm_url = build_google_drive_confirm_url(str(page_response.url), token)
    head_bytes = b""
    async with client.stream("GET", confirm_url, headers={"Range": f"bytes=0-{max(0, int(read_bytes) - 1)}"}) as response:
        raw_content_length = str(response.headers.get("content-length") or "").strip()
        content_length = int(raw_content_length) if raw_content_length.isdigit() else None
        async for chunk in response.aiter_bytes():
            if chunk:
                remaining = max(0, int(read_bytes) - len(head_bytes))
                head_bytes += chunk[:remaining]
            if len(head_bytes) >= int(read_bytes):
                break

        return {
            "status_code": int(response.status_code),
            "final_url": str(response.url),
            "content_type": str(response.headers.get("content-type") or ""),
            "content_length": content_length,
            "head_bytes": head_bytes,
            "confirm_url": confirm_url,
            "confirm_token_present": True,
            "form_action": form_action,
        }
