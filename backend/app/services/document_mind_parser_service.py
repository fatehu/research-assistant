"""
Alibaba Cloud Document Mind parser adapter.

This adapter is optional and must fail-open:
- if credentials are missing
- if SDK is unavailable
- if remote parsing fails
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from loguru import logger

from app.config import settings


class DocumentMindParserService:
    def __init__(self) -> None:
        self._client: Any = None

    @staticmethod
    def _normalize_spaces(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    @staticmethod
    def _dict_get_ci(data: Any, key: str, default: Any = None) -> Any:
        if not isinstance(data, dict):
            return default
        target = str(key or "").strip().lower()
        for raw_key, value in data.items():
            if str(raw_key or "").strip().lower() == target:
                return value
        return default

    @staticmethod
    def _is_http_url(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(re.match(r"^https?://", text, flags=re.IGNORECASE))

    @staticmethod
    def _allowlist() -> set[int]:
        raw = str(getattr(settings, "reader_document_mind_allowlist", "") or "")
        output: set[int] = set()
        for token in re.split(r"[,\s;]+", raw):
            token = str(token or "").strip()
            if not token:
                continue
            try:
                value = int(token)
            except Exception:
                continue
            if value > 0:
                output.add(value)
        return output

    def _enabled_for_paper(self, *, paper_id: Optional[int]) -> bool:
        if not bool(getattr(settings, "reader_document_mind_enabled", False)):
            return False
        allowlist = self._allowlist()
        if not allowlist:
            return True
        if not isinstance(paper_id, int) or paper_id <= 0:
            return False
        return int(paper_id) in allowlist

    def _build_client(self) -> Optional[Any]:
        if self._client is not None:
            return self._client
        access_key_id = str(getattr(settings, "document_mind_access_key_id", "") or "").strip()
        access_key_secret = str(getattr(settings, "document_mind_access_key_secret", "") or "").strip()
        endpoint = str(getattr(settings, "document_mind_endpoint", "") or "").strip()
        region_id = str(getattr(settings, "document_mind_region_id", "cn-hangzhou") or "cn-hangzhou").strip()
        if not access_key_id or not access_key_secret or not endpoint:
            return None
        try:
            from alibabacloud_docmind_api20220711.client import Client as DocMindClient
            from alibabacloud_tea_openapi import models as open_api_models
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[DocMind] sdk import failed: {exc}")
            return None
        try:
            config = open_api_models.Config(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                endpoint=endpoint,
                region_id=region_id,
            )
            self._client = DocMindClient(config)
            return self._client
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[DocMind] client init failed: {exc}")
            self._client = None
            return None

    @staticmethod
    def _extract_job_id(submit_resp: Any) -> str:
        payload = DocumentMindParserService._to_plain_dict(submit_resp)
        if payload:
            candidates: List[Any] = [
                payload.get("id"),
                payload.get("Id"),
                (payload.get("data") or {}).get("id") if isinstance(payload.get("data"), dict) else None,
                (payload.get("data") or {}).get("Id") if isinstance(payload.get("data"), dict) else None,
                (payload.get("Data") or {}).get("id") if isinstance(payload.get("Data"), dict) else None,
                (payload.get("Data") or {}).get("Id") if isinstance(payload.get("Data"), dict) else None,
                (((payload.get("body") or {}).get("data") or {}).get("id") if isinstance((payload.get("body") or {}).get("data"), dict) else None),
                (((payload.get("body") or {}).get("data") or {}).get("Id") if isinstance((payload.get("body") or {}).get("data"), dict) else None),
                (((payload.get("body") or {}).get("Data") or {}).get("id") if isinstance((payload.get("body") or {}).get("Data"), dict) else None),
                (((payload.get("body") or {}).get("Data") or {}).get("Id") if isinstance((payload.get("body") or {}).get("Data"), dict) else None),
            ]
            for candidate in candidates:
                job_id = str(candidate or "").strip()
                if job_id:
                    return job_id
        try:
            body = getattr(submit_resp, "body", None)
            data = getattr(body, "data", None)
            job_id = str(getattr(data, "id", "") or getattr(data, "Id", "") or "").strip()
            if job_id:
                return job_id
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_status(status_resp: Any) -> str:
        payload = DocumentMindParserService._to_plain_dict(status_resp)
        if payload:
            candidates: List[Any] = [
                payload.get("status"),
                payload.get("Status"),
                payload.get("code"),
                payload.get("Code"),
                (payload.get("data") or {}).get("status") if isinstance(payload.get("data"), dict) else None,
                (payload.get("data") or {}).get("Status") if isinstance(payload.get("data"), dict) else None,
                (payload.get("Data") or {}).get("status") if isinstance(payload.get("Data"), dict) else None,
                (payload.get("Data") or {}).get("Status") if isinstance(payload.get("Data"), dict) else None,
                (((payload.get("body") or {}).get("data") or {}).get("status") if isinstance((payload.get("body") or {}).get("data"), dict) else None),
                (((payload.get("body") or {}).get("Data") or {}).get("Status") if isinstance((payload.get("body") or {}).get("Data"), dict) else None),
            ]
            for candidate in candidates:
                value = str(candidate or "").strip().lower()
                if value:
                    return value
        try:
            body = getattr(status_resp, "body", None)
            data = getattr(body, "data", None)
            value = str(getattr(data, "status", "") or getattr(data, "Status", "") or "").strip().lower()
            if value:
                return value
        except Exception:
            pass
        return ""

    @staticmethod
    def _to_plain_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if value is None:
            return {}
        try:
            to_map = getattr(value, "to_map", None)
            if callable(to_map):
                mapped = to_map() or {}
                if isinstance(mapped, dict):
                    return dict(mapped)
        except Exception:
            pass
        output: Dict[str, Any] = {}
        for attr in ("body", "data", "Data", "id", "Id", "status", "Status", "code", "Code"):
            try:
                attr_value = getattr(value, attr, None)
            except Exception:
                attr_value = None
            if attr_value is None:
                continue
            if isinstance(attr_value, (str, int, float, bool)):
                output[attr] = attr_value
            elif isinstance(attr_value, dict):
                output[attr] = dict(attr_value)
            else:
                nested = DocumentMindParserService._to_plain_dict(attr_value)
                if nested:
                    output[attr] = nested
        return output

    @staticmethod
    def _normalize_status(value: str) -> str:
        status = str(value or "").strip().lower()
        if status in {"succeeded", "success", "done", "finished", "complete", "completed"}:
            return "success"
        if status in {"failed", "error", "stopped", "cancelled"}:
            return "failed"
        if status in {"queued", "running", "processing", "pending"}:
            return "running"
        return status or "unknown"

    @classmethod
    def _extract_text_from_result_payload(cls, payload: Any) -> str:
        text_keys = {
            "markdown",
            "md",
            "text",
            "content",
            "result_markdown",
            "result_text",
            "full_text",
        }
        candidates: List[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                for raw_key, value in node.items():
                    key = str(raw_key or "").strip().lower()
                    if isinstance(value, str):
                        normalized = cls._normalize_spaces(value)
                        if len(normalized) >= 20 and (key in text_keys or "markdown" in key or key.endswith("_text")):
                            candidates.append(normalized)
                    elif isinstance(value, (dict, list)):
                        _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(payload)
        if not candidates:
            return ""
        candidates.sort(key=len, reverse=True)
        return candidates[0]

    @classmethod
    def _extract_docmind_data_dict(cls, payload: Any) -> Dict[str, Any]:
        """
        Normalize various SDK/HTTP wrapper shapes into a plain DocMind Data dict.
        Expected keys include: layouts/styles/logics/docInfo/version.
        """
        if not isinstance(payload, dict):
            return {}
        direct_keys = {"layouts", "styles", "logics", "docInfo", "version"}
        if any(key in payload for key in direct_keys):
            return dict(payload)

        for key in ("data", "Data", "result", "Result"):
            value = payload.get(key)
            if isinstance(value, dict) and any(k in value for k in direct_keys):
                return dict(value)

        body = payload.get("body")
        if isinstance(body, dict):
            nested = cls._extract_docmind_data_dict(body)
            if nested:
                return nested
        return {}

    @classmethod
    def _extract_text_from_docmind_structure(cls, structure: Dict[str, Any]) -> str:
        layouts = [row for row in list((structure or {}).get("layouts") or []) if isinstance(row, dict)]
        lines: List[str] = []
        for layout in layouts:
            blocks = [row for row in list(layout.get("blocks") or []) if isinstance(row, dict)]
            if blocks:
                for block in blocks:
                    text = cls._normalize_spaces(str(block.get("text") or ""))
                    if text:
                        lines.append(text)
                continue
            text = cls._normalize_spaces(str(layout.get("text") or ""))
            if text:
                lines.append(text)
        merged = "\n".join(lines).strip()
        if merged:
            return merged
        return cls._extract_text_from_result_payload(dict(structure or {}).get("raw_result") or structure)

    async def _run_parse_job(
        self,
        *,
        paper_id: Optional[int],
        page: int,
        file_url: str,
        file_name: Optional[str] = None,
        local_pdf_path: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        if not self._enabled_for_paper(paper_id=paper_id):
            return None, {"used": False, "reason": "disabled_or_not_allowlisted"}
        client = self._build_client()
        if client is None:
            return None, {"used": False, "reason": "client_unavailable"}

        option = str(getattr(settings, "document_mind_option", "docStructure") or "docStructure").strip() or "docStructure"
        option_lower = option.lower()

        # DocStructure main path (with local file upload) when requested.
        if option_lower in {"docstructure", "doc_structure"}:
            data, meta = await self._run_doc_structure_job(
                client=client,
                paper_id=paper_id,
                page=page,
                local_pdf_path=local_pdf_path,
                file_name=file_name,
            )
            if isinstance(data, dict):
                return data, meta
            # Keep parser fallback fail-open for availability.
            if not self._is_http_url(file_url):
                return None, meta
            parser_data, parser_meta = await self._run_doc_parser_job(
                client=client,
                page=page,
                file_url=file_url,
                file_name=file_name,
                option="",
            )
            if isinstance(parser_data, dict):
                parser_meta = dict(parser_meta or {})
                parser_meta["fallback_from"] = "doc_structure"
                parser_meta["fallback_reason"] = str((meta or {}).get("reason") or "")
                return parser_data, parser_meta
            merged = dict(parser_meta or {})
            merged["fallback_from"] = "doc_structure"
            merged["fallback_reason"] = str((meta or {}).get("reason") or "")
            return None, merged

        if not self._is_http_url(file_url):
            return None, {"used": False, "reason": "invalid_or_missing_file_url"}
        return await self._run_doc_parser_job(
            client=client,
            page=page,
            file_url=file_url,
            file_name=file_name,
            option=option,
        )

    @staticmethod
    def _build_single_page_pdf_bytes(*, pdf_path: Path, page: int) -> Optional[bytes]:
        """Extract one page into an in-memory PDF stream for page-scoped DocStructure parsing."""
        try:
            from pypdf import PdfReader, PdfWriter
        except Exception:
            return None
        try:
            reader = PdfReader(str(pdf_path))
            page_index = max(0, int(page) - 1)
            if page_index >= len(reader.pages):
                return None
            writer = PdfWriter()
            writer.add_page(reader.pages[page_index])
            buf = io.BytesIO()
            writer.write(buf)
            return buf.getvalue()
        except Exception:
            return None

    async def _run_doc_parser_job(
        self,
        *,
        client: Any,
        page: int,
        file_url: str,
        file_name: Optional[str],
        option: str,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        try:
            from alibabacloud_docmind_api20220711 import models as docmind_models
        except Exception:
            return None, {"used": False, "reason": "sdk_models_unavailable"}

        safe_file_name = str(file_name or "").strip()
        if not safe_file_name:
            parsed = urlparse(file_url)
            safe_file_name = os.path.basename(parsed.path) or "paper.pdf"
        submit_req = docmind_models.SubmitDocParserJobRequest(
            file_url=str(file_url).strip(),
            file_name=safe_file_name[:200],
            option=str(option or "").strip(),
        )
        try:
            submit_resp = await asyncio.to_thread(client.submit_doc_parser_job, submit_req)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[DocMind] submit job failed: {exc}")
            return None, {"used": False, "reason": "submit_failed"}

        job_id = self._extract_job_id(submit_resp)
        if not job_id:
            submit_payload = self._to_plain_dict(submit_resp)
            body = self._dict_get_ci(submit_payload, "body", {}) or {}
            code = str(self._dict_get_ci(body, "Code", "") or self._dict_get_ci(body, "code", "") or "").strip()
            message = str(self._dict_get_ci(body, "Message", "") or self._dict_get_ci(body, "message", "") or "").strip()
            return None, {
                "used": False,
                "reason": "missing_job_id",
                "submit_code": code or None,
                "submit_message": message or None,
            }

        deadline = time.monotonic() + max(
            10.0,
            float(getattr(settings, "document_mind_timeout_seconds", 90) or 90),
        )
        poll_interval = max(
            0.5,
            float(getattr(settings, "document_mind_poll_interval_seconds", 1.5) or 1.5),
        )
        final_status = "running"
        while time.monotonic() < deadline:
            status_req = docmind_models.QueryDocParserStatusRequest(id=job_id)
            try:
                status_resp = await asyncio.to_thread(client.query_doc_parser_status, status_req)
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[DocMind] query status failed job={job_id}: {exc}")
                return None, {"used": False, "reason": "query_status_failed", "job_id": job_id}
            final_status = self._normalize_status(self._extract_status(status_resp))
            if final_status in {"success", "failed"}:
                break
            await asyncio.sleep(poll_interval)

        if final_status != "success":
            reason = "job_timeout" if final_status in {"running", "unknown"} else "job_failed"
            return None, {"used": False, "reason": reason, "job_id": job_id, "status": final_status}

        result_req = docmind_models.GetDocParserResultRequest(id=job_id)
        try:
            result_resp = await asyncio.to_thread(client.get_doc_parser_result, result_req)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[DocMind] get result failed job={job_id}: {exc}")
            return None, {"used": False, "reason": "get_result_failed", "job_id": job_id}

        raw_payload: Dict[str, Any] = {}
        try:
            body = getattr(result_resp, "body", None)
            raw_data = getattr(body, "data", None)
            if isinstance(raw_data, dict):
                raw_payload = dict(raw_data)
            elif hasattr(raw_data, "to_map"):
                raw_payload = dict(raw_data.to_map() or {})
        except Exception:
            raw_payload = {}

        data = self._extract_docmind_data_dict(raw_payload)
        if not data:
            return None, {"used": False, "reason": "empty_result_data", "job_id": job_id}

        return data, {
            "used": True,
            "reason": "applied",
            "job_id": job_id,
            "status": "success",
            "option": option,
            "api": "doc_parser",
        }

    async def _run_doc_structure_job(
        self,
        *,
        client: Any,
        paper_id: Optional[int],
        page: int,
        local_pdf_path: Optional[str],
        file_name: Optional[str],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        try:
            from alibabacloud_docmind_api20220711 import models as docmind_models
            from alibabacloud_tea_util import models as util_models
        except Exception:
            return None, {"used": False, "reason": "sdk_models_unavailable"}

        path_text = str(local_pdf_path or "").strip()
        local_path = Path(path_text) if path_text else None
        if local_path is None or (not local_path.exists()) or (not local_path.is_file()):
            return None, {"used": False, "reason": "local_pdf_not_found"}

        safe_file_name = str(file_name or "").strip() or local_path.name or f"paper_{int(paper_id or 0)}.pdf"
        submit_req = docmind_models.SubmitDocStructureJobAdvanceRequest()
        submit_req.file_name = safe_file_name[:200]
        suffix = local_path.suffix.lstrip(".")
        if suffix:
            submit_req.file_name_extension = suffix

        runtime = util_models.RuntimeOptions()
        page_pdf_bytes = await asyncio.to_thread(
            self._build_single_page_pdf_bytes,
            pdf_path=local_path,
            page=int(page),
        )
        if not isinstance(page_pdf_bytes, (bytes, bytearray)) or len(page_pdf_bytes) <= 0:
            return None, {
                "used": False,
                "reason": "single_page_pdf_extract_failed",
                "page": int(page),
            }
        file_stream = io.BytesIO(bytes(page_pdf_bytes))
        submit_req.file_name = f"{Path(submit_req.file_name).stem}_p{int(page)}.pdf"[:200]
        submit_req.file_name_extension = "pdf"
        upload_scope = "single_page_pdf_stream"
        try:
            submit_req.file_url_object = file_stream
            submit_resp = await asyncio.to_thread(client.submit_doc_structure_job_advance, submit_req, runtime)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[DocMind] submit doc structure job failed: {exc}")
            return None, {"used": False, "reason": "submit_doc_structure_failed"}

        job_id = self._extract_job_id(submit_resp)
        if not job_id:
            submit_payload = self._to_plain_dict(submit_resp)
            body = self._dict_get_ci(submit_payload, "body", {}) or {}
            code = str(self._dict_get_ci(body, "Code", "") or "").strip()
            message = str(self._dict_get_ci(body, "Message", "") or "").strip()
            return None, {
                "used": False,
                "reason": "missing_job_id",
                "submit_code": code or None,
                "submit_message": message or None,
            }

        deadline = time.monotonic() + max(
            10.0,
            float(getattr(settings, "document_mind_timeout_seconds", 90) or 90),
        )
        poll_interval = max(
            0.5,
            float(getattr(settings, "document_mind_poll_interval_seconds", 1.5) or 1.5),
        )

        processing_codes = {"DocProcessing", "Processing", "PROCESSING"}
        while time.monotonic() < deadline:
            req = docmind_models.GetDocStructureResultRequest()
            req.id = job_id
            try:
                result_resp = await asyncio.to_thread(client.get_doc_structure_result, req)
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[DocMind] get doc structure result failed job={job_id}: {exc}")
                return None, {"used": False, "reason": "get_doc_structure_result_failed", "job_id": job_id}

            payload = self._to_plain_dict(result_resp)
            body = self._dict_get_ci(payload, "body", {}) or {}
            code = str(self._dict_get_ci(body, "Code", "") or "").strip()
            status = str(self._dict_get_ci(body, "Status", "") or "").strip()
            completed = bool(self._dict_get_ci(body, "Completed", False))
            data = self._dict_get_ci(body, "Data", {})
            if completed and isinstance(data, dict) and data:
                filtered_data = self._filter_doc_structure_to_page(data=data, page=page)
                return filtered_data, {
                    "used": True,
                    "reason": "applied",
                    "job_id": job_id,
                    "status": "success",
                    "option": "docStructure",
                    "api": "doc_structure",
                    "upload_scope": upload_scope,
                }

            if code and code not in processing_codes:
                message = str(self._dict_get_ci(body, "Message", "") or "").strip()
                return None, {
                    "used": False,
                    "reason": "doc_structure_failed",
                    "job_id": job_id,
                    "status": status or "",
                    "submit_code": code,
                    "submit_message": message or None,
                }
            await asyncio.sleep(poll_interval)

        return None, {"used": False, "reason": "job_timeout", "job_id": job_id, "api": "doc_structure"}

    def _filter_doc_structure_to_page(self, *, data: Dict[str, Any], page: int) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        page_index_zero = max(0, int(page) - 1)
        page_index_one = max(1, int(page))
        layouts = [row for row in list(self._dict_get_ci(data, "layouts", []) or []) if isinstance(row, dict)]
        filtered_layouts: List[Dict[str, Any]] = []
        for layout in layouts:
            page_num = layout.get("pageNum")
            if isinstance(page_num, list):
                page_values = [int(v) for v in page_num if isinstance(v, (int, float, str)) and str(v).strip().isdigit()]
            elif isinstance(page_num, (int, float, str)) and str(page_num).strip().isdigit():
                page_values = [int(page_num)]
            else:
                page_values = []
            if not page_values:
                filtered_layouts.append(layout)
                continue
            if page_index_zero in page_values or page_index_one in page_values:
                filtered_layouts.append(layout)

        output = dict(data)
        output["layouts"] = filtered_layouts if filtered_layouts else layouts
        return output

    async def parse_page_structure(
        self,
        *,
        paper_id: Optional[int],
        page: int,
        file_url: str,
        file_name: Optional[str] = None,
        local_pdf_path: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        data, meta = await self._run_parse_job(
            paper_id=paper_id,
            page=page,
            file_url=file_url,
            file_name=file_name,
            local_pdf_path=local_pdf_path,
        )
        if not isinstance(data, dict):
            return None, meta

        logics = dict(data.get("logics") or {})
        doc_info = dict(data.get("docInfo") or {})
        pages = [row for row in list(doc_info.get("pages") or []) if isinstance(row, dict)]
        page_row = None
        if pages:
            page_index = max(0, int(page) - 1)
            if page_index < len(pages):
                page_row = pages[page_index]
            if not isinstance(page_row, dict):
                page_row = pages[0]
        image_url = ""
        image_width = 0
        image_height = 0
        image_path = ""
        if isinstance(page_row, dict):
            image_url = str(page_row.get("imageUrl") or page_row.get("image_url") or "").strip()
            try:
                image_width = int(page_row.get("imageWidth") or page_row.get("pageWidth") or 0)
            except Exception:
                image_width = 0
            try:
                image_height = int(page_row.get("imageHeight") or page_row.get("pageHeight") or 0)
            except Exception:
                image_height = 0
            image_path = str(page_row.get("sourceImagePath") or page_row.get("source_image_path") or "").strip()

        structure = {
            "layouts": [row for row in list(data.get("layouts") or []) if isinstance(row, dict)],
            "styles": [row for row in list(data.get("styles") or []) if isinstance(row, dict)],
            "doc_tree": [row for row in list(logics.get("docTree") or []) if isinstance(row, dict)],
            "paragraph_kvs": [row for row in list(logics.get("paragraphKVs") or []) if isinstance(row, dict)],
            "doc_info": doc_info,
            "version": str(data.get("version") or ""),
            "page_image_url": image_url,
            "page_image_path": image_path,
            "page_image_width": image_width,
            "page_image_height": image_height,
            "raw_result": data,
        }
        return structure, meta

    async def parse_page_text(
        self,
        *,
        paper_id: Optional[int],
        page: int,
        file_url: str,
        file_name: Optional[str] = None,
        local_pdf_path: Optional[str] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        structure, meta = await self.parse_page_structure(
            paper_id=paper_id,
            page=page,
            file_url=file_url,
            file_name=file_name,
            local_pdf_path=local_pdf_path,
        )
        if not isinstance(structure, dict):
            return None, meta
        text = self._extract_text_from_docmind_structure(structure)
        if not text:
            return None, {
                "used": False,
                "reason": "empty_result_text",
                "job_id": str((meta or {}).get("job_id") or ""),
            }
        return text, meta


_document_mind_parser_service: Optional[DocumentMindParserService] = None


def get_document_mind_parser_service() -> DocumentMindParserService:
    global _document_mind_parser_service
    if _document_mind_parser_service is None:
        _document_mind_parser_service = DocumentMindParserService()
    return _document_mind_parser_service
