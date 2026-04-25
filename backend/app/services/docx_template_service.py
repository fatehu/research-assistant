from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.docx import DocxGenerationJob, DocxTemplate, DocxTemplateFile


DOCX_TEMPLATE_FILE_ROLES = {
    "sample_template": "成品/样例模板",
    "writing_guide": "撰写说明/填报指南",
    "reference": "普通参考附件",
}

DEFAULT_DOCX_STYLE_PROMPT = """# 平台默认 DOCX 样式与结构要求

除非模板 DOCX 约束明确覆盖，本段要求始终生效。目标不是生成“看起来像 Word 的静态文本”，而是生成具备 Word 原生结构的可编辑文档。

1. 标题与导航
- 正文各级标题必须使用 Word 原生 Heading 样式/outline level，例如 docx-js 的 HeadingLevel.HEADING_1/2/3，不能只用加粗或字号模拟标题。
- 标题层级必须和 source.md 的 Markdown 层级对应，确保 Word 导航窗格可以按章节跳转。

2. 目录
- 如果要求生成目录，必须使用 Word 原生 TOC 字段或 docx-js TableOfContents，并开启目录超链接。
- 目录项必须来自 Heading 样式/outline level，不能手写静态目录文本，不能硬编码页码。
- 目录建议覆盖 1-3 级标题；如果模板另有说明，以模板为准。

3. 页码与页眉页脚
- 页码必须使用 Word 原生 PAGE/NUMPAGES 字段或 docx-js PageNumber，不能写死数字。
- 默认封面不显示页码；如存在目录页，可单独成节；正文页码从正文第一页开始连续编号。模板有明确要求时按模板执行。
- 页眉页脚应使用 Word header/footer，不要把页眉页脚内容写进正文段落。

4. 交叉引用与跳转
- 如果文中出现“见第 X 节/见图 X/见表 X”等交叉引用，优先使用 bookmark + internal hyperlink；需要页码引用时使用 REF/PAGEREF 字段。
- 图、表、公式编号应使用稳定编号，不要在正文多处手动复制容易失配的编号。

5. 生成与校验
- 优先使用官方 document-skills/docx 工作流。生成后至少解包检查 document.xml/styles.xml/header/footer，确认存在 Heading 样式、TOC/页码字段或等价 OOXML 结构。
- 如果无法实现动态目录、页码或交叉引用，必须在最终 notes 中明确说明，不要假装成功。"""

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w(tag: str) -> str:
    return f"{_W_NS}{tag}"


class DocxTemplateService:
    """File-backed template and generated DOCX workspace manager."""

    def __init__(self, upload_root: Optional[Path] = None) -> None:
        self.upload_root = upload_root or self._default_upload_root()
        self.docx_root = self.upload_root / "docx"
        self.templates_root = self.docx_root / "templates"

    @staticmethod
    def _default_upload_root() -> Path:
        configured = str(os.getenv("UPLOAD_DIR") or "").strip()
        if configured:
            return Path(os.path.abspath(configured))
        mounted = Path("/app/uploads")
        if mounted.exists():
            return mounted.resolve()
        return Path(os.path.abspath("./uploads"))

    @staticmethod
    def safe_slug(value: Any, *, fallback: str = "template") -> str:
        text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
        return (text or fallback)[:120]

    @staticmethod
    def safe_filename(value: Any, *, fallback: str = "file") -> str:
        name = Path(str(value or "").replace("\\", "/")).name
        suffix = Path(name).suffix
        stem = Path(name).stem if suffix else name
        safe_stem = re.sub(r"[^a-zA-Z0-9_.() -]+", "-", stem).strip(" .-_")
        safe_suffix = re.sub(r"[^a-zA-Z0-9.]+", "", suffix)
        if safe_suffix and not safe_suffix.startswith("."):
            safe_suffix = f".{safe_suffix}"
        text = f"{safe_stem or fallback}{safe_suffix}"
        return (text or fallback)[:180]

    @staticmethod
    def _unique_path(directory: Path, filename: str) -> Path:
        candidate = directory / filename
        if not candidate.exists():
            return candidate
        suffix = candidate.suffix
        stem = candidate.stem or "file"
        for index in range(2, 1000):
            next_candidate = directory / f"{stem}-{index}{suffix}"
            if not next_candidate.exists():
                return next_candidate
        return directory / f"{stem}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{suffix}"

    @staticmethod
    def _original_filename(value: Any, *, fallback: str = "file") -> str:
        name = Path(str(value or "").replace("\\", "/")).name.strip()
        name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip(" .")
        return name or fallback

    def ensure_roots(self) -> None:
        self.docx_root.mkdir(parents=True, exist_ok=True)
        self.templates_root.mkdir(parents=True, exist_ok=True)
        prompt_path = self.default_docx_style_prompt_path()
        if not prompt_path.exists():
            prompt_path.write_text(DEFAULT_DOCX_STYLE_PROMPT, encoding="utf-8")

    def _template_dir(self, template_id: str) -> Path:
        return self.templates_root / self.safe_slug(template_id, fallback="template")

    def _manifest_path(self, template_id: str) -> Path:
        return self._template_dir(template_id) / "manifest.json"

    def default_docx_style_prompt_path(self) -> Path:
        return self.docx_root / "default_docx_style_prompt.md"

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _parse_iso_datetime(value: Any) -> datetime:
        raw = str(value or "").strip()
        if not raw:
            return datetime.utcnow()
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return datetime.utcnow()

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload or {}) if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def get_default_docx_style_prompt(self) -> str:
        self.ensure_roots()
        text = self._read_text(self.default_docx_style_prompt_path()).strip()
        return text or DEFAULT_DOCX_STYLE_PROMPT

    def update_default_docx_style_prompt(self, prompt: str) -> Dict[str, Any]:
        self.ensure_roots()
        text = str(prompt or "").strip() or DEFAULT_DOCX_STYLE_PROMPT
        self.default_docx_style_prompt_path().write_text(text, encoding="utf-8")
        return {"default_docx_style_prompt": text}

    @staticmethod
    def _template_db_payload(template: Dict[str, Any], *, user_id: Optional[int]) -> Dict[str, Any]:
        return {
            "template_id": str(template.get("template_id") or ""),
            "user_id": user_id,
            "name": str(template.get("name") or ""),
            "description": str(template.get("description") or ""),
            "root_path": str(template.get("root_path") or ""),
            "files_path": str(template.get("files_path") or ""),
            "md_constraints": str(template.get("md_constraints") or ""),
            "docx_constraints": str(template.get("docx_constraints") or ""),
            "created_at": DocxTemplateService._parse_iso_datetime(template.get("created_at")),
            "updated_at": DocxTemplateService._parse_iso_datetime(template.get("updated_at")),
        }

    @staticmethod
    def _file_db_payload(file_payload: Dict[str, Any], *, template_id: str, user_id: Optional[int]) -> Dict[str, Any]:
        return {
            "template_id": template_id,
            "user_id": user_id,
            "original_filename": str(file_payload.get("original_filename") or file_payload.get("name") or ""),
            "stored_filename": str(file_payload.get("stored_name") or file_payload.get("name") or ""),
            "file_role": DocxTemplateService.normalize_file_role(file_payload.get("file_role")),
            "media_type": str(file_payload.get("media_type") or ""),
            "size": int(file_payload.get("size") or 0),
            "relative_path": str(file_payload.get("relative_path") or ""),
            "path": str(file_payload.get("path") or ""),
            "updated_at": DocxTemplateService._parse_iso_datetime(file_payload.get("modified_at")),
        }

    async def sync_template_to_db(
        self,
        db: AsyncSession,
        *,
        user_id: Optional[int],
        template: Dict[str, Any],
    ) -> None:
        template_id = str(template.get("template_id") or "").strip()
        if not template_id:
            return
        payload = self._template_db_payload(template, user_id=user_id)
        result = await db.execute(select(DocxTemplate).where(DocxTemplate.template_id == template_id))
        row = result.scalar_one_or_none()
        if row is None:
            row = DocxTemplate(**payload)
            db.add(row)
        else:
            for key, value in payload.items():
                if key == "template_id":
                    continue
                setattr(row, key, value)
        for file_payload in list(template.get("files") or []):
            await self.sync_template_file_to_db(
                db,
                user_id=user_id,
                template_id=template_id,
                file_payload=dict(file_payload or {}),
            )

    async def sync_template_file_to_db(
        self,
        db: AsyncSession,
        *,
        user_id: Optional[int],
        template_id: str,
        file_payload: Dict[str, Any],
        parse_status: Optional[str] = None,
        parse_warnings: Optional[List[str]] = None,
        analysis_artifacts: Optional[Dict[str, Any]] = None,
    ) -> None:
        stored_filename = str(file_payload.get("stored_name") or file_payload.get("name") or "").strip()
        if not template_id or not stored_filename:
            return
        payload = self._file_db_payload(file_payload, template_id=template_id, user_id=user_id)
        if parse_status:
            payload["parse_status"] = parse_status
        result = await db.execute(
            select(DocxTemplateFile).where(
                DocxTemplateFile.template_id == template_id,
                DocxTemplateFile.stored_filename == stored_filename,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DocxTemplateFile(
                **payload,
                parse_status=parse_status or "pending",
                parse_warnings=parse_warnings or [],
                analysis_artifacts=analysis_artifacts or {},
            )
            db.add(row)
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            if parse_status:
                row.parse_status = parse_status
            if parse_warnings is not None:
                row.parse_warnings = parse_warnings
            if analysis_artifacts is not None:
                row.analysis_artifacts = analysis_artifacts

    async def delete_template_file_from_db(
        self,
        db: AsyncSession,
        *,
        template_id: str,
        stored_filename: str,
    ) -> None:
        result = await db.execute(
            select(DocxTemplateFile).where(
                DocxTemplateFile.template_id == template_id,
                DocxTemplateFile.stored_filename == stored_filename,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await db.delete(row)

    async def sync_template_analysis_to_db(
        self,
        db: AsyncSession,
        *,
        template_id: str,
        analysis: Dict[str, Any],
    ) -> None:
        warnings = [str(item) for item in list(analysis.get("warnings") or []) if str(item).strip()]
        for item in list(analysis.get("files") or []):
            if not isinstance(item, dict):
                continue
            stored_filename = str(item.get("stored_file") or item.get("file") or "").strip()
            if not stored_filename:
                continue
            result = await db.execute(
                select(DocxTemplateFile).where(
                    DocxTemplateFile.template_id == template_id,
                    DocxTemplateFile.stored_filename == stored_filename,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                continue
            file_warnings = [
                value for value in warnings
                if str(item.get("file") or stored_filename) in value or stored_filename in value
            ]
            row.parse_status = "warning" if file_warnings else "parsed"
            row.parse_warnings = file_warnings
            row.analysis_artifacts = {
                "kind": item.get("kind"),
                "detected_suffix": item.get("detected_suffix"),
                "converted_from_doc": item.get("converted_from_doc"),
                "pandoc_markdown_path": item.get("pandoc_markdown_path"),
                "text_chars": item.get("text_chars") or item.get("pandoc_markdown_chars") or item.get("text_sample_chars"),
            }

    async def upsert_generation_job(
        self,
        db: AsyncSession,
        *,
        user_id: Optional[int],
        job: Dict[str, Any],
    ) -> None:
        docx_id = str(job.get("docx_id") or "").strip()
        if not docx_id:
            return
        status_value = str(job.get("status") or "running").strip() or "running"
        now = datetime.utcnow()
        result = await db.execute(select(DocxGenerationJob).where(DocxGenerationJob.docx_id == docx_id))
        row = result.scalar_one_or_none()
        payload = {
            "template_id": str(job.get("template_id") or "").strip() or None,
            "template_name": str(job.get("template_name") or "").strip() or None,
            "artifact_id": str(job.get("artifact_id") or "").strip() or None,
            "conversation_id": self._safe_int(job.get("conversation_id")),
            "user_id": user_id,
            "workspace_path": str(job.get("workspace_dir") or job.get("workspace_path") or ""),
            "source_path": str(job.get("source_path") or ""),
            "requirements_path": str(job.get("requirements_path") or ""),
            "output_basename": str(job.get("output_basename") or ""),
            "docx_path": str(job.get("docx_path") or ""),
            "pdf_path": str(job.get("pdf_path") or ""),
            "status": status_value,
            "validation_status": str(job.get("validation_status") or ""),
            "claude_session_id": str(job.get("session_id") or job.get("claude_session_id") or ""),
            "error_message": str(job.get("error") or job.get("error_message") or ""),
            "files": list(job.get("files") or []),
            "metadata_": dict(job.get("metadata") or {}),
            "updated_at": now,
            "completed_at": now if status_value in {"completed", "failed", "cancelled"} else None,
        }
        if row is None:
            row = DocxGenerationJob(docx_id=docx_id, created_at=now, **payload)
            db.add(row)
        else:
            for key, value in payload.items():
                setattr(row, key, value)

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            raw = str(value or "").strip()
            return int(raw) if raw else None
        except Exception:
            return None

    @staticmethod
    def normalize_file_role(value: Any) -> str:
        role = str(value or "").strip()
        return role if role in DOCX_TEMPLATE_FILE_ROLES else "reference"

    def _load_manifest(self, template_id: str) -> Dict[str, Any]:
        path = self._manifest_path(template_id)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload or {}) if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def file_payload(
        self,
        path: Path,
        *,
        base: Optional[Path] = None,
        file_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved = path.resolve()
        base_path = (base or self.docx_root).resolve()
        try:
            relative_path = resolved.relative_to(base_path).as_posix()
        except Exception:
            relative_path = resolved.name
        stat = resolved.stat()
        meta = dict(file_meta or {})
        original_filename = str(meta.get("original_filename") or resolved.name).strip() or resolved.name
        if not Path(original_filename).suffix:
            detected_suffix = self._detect_document_suffix(resolved, file_meta=meta)
            if detected_suffix in {".doc", ".docx"}:
                original_filename = f"{original_filename}{detected_suffix}"
        media_type = (
            str(meta.get("media_type") or "").strip()
            or mimetypes.guess_type(original_filename)[0]
            or mimetypes.guess_type(resolved.name)[0]
            or "application/octet-stream"
        )
        role = self.normalize_file_role((file_meta or {}).get("file_role"))
        return {
            "name": original_filename,
            "stored_name": resolved.name,
            "original_filename": original_filename,
            "relative_path": relative_path,
            "path": str(resolved),
            "size": stat.st_size,
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
            "media_type": media_type,
            "download_path": relative_path,
            "file_role": role,
            "file_role_label": DOCX_TEMPLATE_FILE_ROLES.get(role, "普通参考附件"),
        }

    def _list_files(
        self,
        root: Path,
        *,
        base: Optional[Path] = None,
        file_manifest: Optional[Dict[str, Any]] = None,
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        if not root.exists():
            return []
        files: List[Dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            files.append(
                self.file_payload(
                    path,
                    base=base or self.docx_root,
                    file_meta=dict((file_manifest or {}).get(path.name) or {}),
                )
            )
            if len(files) >= limit:
                break
        return files

    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        self.ensure_roots()
        safe_id = self.safe_slug(template_id, fallback="")
        if not safe_id:
            return None
        root = self._template_dir(safe_id)
        if not root.is_dir():
            return None
        manifest = self._load_manifest(safe_id)
        files_dir = root / "files"
        file_manifest = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
        return {
            "template_id": safe_id,
            "name": str(manifest.get("name") or safe_id),
            "description": str(manifest.get("description") or ""),
            "created_at": str(manifest.get("created_at") or ""),
            "updated_at": str(manifest.get("updated_at") or ""),
            "created_by": manifest.get("created_by"),
            "root_path": str(root),
            "files_path": str(files_dir),
            "md_constraints": self._read_text(root / "md_constraints.md"),
            "docx_constraints": self._read_text(root / "docx_constraints.md"),
            "files": self._list_files(files_dir, file_manifest=file_manifest),
        }

    def upsert_template(
        self,
        *,
        template_id: Optional[str],
        name: str,
        description: str = "",
        md_constraints: str = "",
        docx_constraints: str = "",
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.ensure_roots()
        safe_id = self.safe_slug(template_id or name or "", fallback="")
        if not safe_id:
            safe_id = f"template-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        root = self._template_dir(safe_id)
        files_dir = root / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        previous = self._load_manifest(safe_id)
        now = datetime.utcnow().isoformat()
        manifest = {
            "template_id": safe_id,
            "name": str(name or previous.get("name") or safe_id).strip() or safe_id,
            "description": str(description or ""),
            "created_at": str(previous.get("created_at") or now),
            "updated_at": now,
            "created_by": previous.get("created_by") or user_id,
            "files": dict(previous.get("files") or {}) if isinstance(previous.get("files"), dict) else {},
        }
        (root / "md_constraints.md").write_text(str(md_constraints or ""), encoding="utf-8")
        (root / "docx_constraints.md").write_text(str(docx_constraints or ""), encoding="utf-8")
        self._write_json(root / "manifest.json", manifest)
        return self.get_template(safe_id) or {}

    def save_template_file(
        self,
        *,
        template_id: str,
        filename: str,
        content: bytes,
        file_role: str = "reference",
    ) -> Dict[str, Any]:
        template = self.get_template(template_id)
        if template is None:
            raise ValueError("模板不存在")
        original_name = self._original_filename(filename, fallback="template-file")
        safe_name = self.safe_filename(original_name, fallback="template-file")
        files_dir = self._template_dir(template["template_id"]) / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        target = self._unique_path(files_dir, safe_name)
        safe_name = target.name
        target.write_bytes(content)
        manifest = self._load_manifest(template["template_id"])
        files = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
        files[safe_name] = {
            "file_role": self.normalize_file_role(file_role),
            "original_filename": original_name,
            "stored_filename": safe_name,
            "original_suffix": Path(original_name).suffix.lower(),
            "media_type": mimetypes.guess_type(original_name)[0] or "application/octet-stream",
            "uploaded_at": datetime.utcnow().isoformat(),
        }
        manifest["files"] = files
        manifest["updated_at"] = datetime.utcnow().isoformat()
        self._write_json(self._manifest_path(template["template_id"]), manifest)
        return self.file_payload(target, file_meta=files[safe_name])

    def _resolve_template_file(
        self,
        *,
        template_id: str,
        filename: str,
    ) -> Tuple[str, Path, Dict[str, Any], Dict[str, Any]]:
        safe_id = self.safe_slug(template_id, fallback="")
        manifest = self._load_manifest(safe_id)
        files = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
        raw_name = self._original_filename(filename, fallback="")
        candidates = [raw_name, self.safe_filename(raw_name, fallback="")]
        for candidate in candidates:
            if not candidate:
                continue
            target = self._template_dir(safe_id) / "files" / candidate
            if target.is_file():
                return candidate, target, dict(files.get(candidate) or {}), manifest
        for stored_name, meta in files.items():
            original_name = str(dict(meta or {}).get("original_filename") or "").strip()
            if raw_name and raw_name in {stored_name, original_name}:
                target = self._template_dir(safe_id) / "files" / stored_name
                if target.is_file():
                    return stored_name, target, dict(meta or {}), manifest
        raise FileNotFoundError("文件不存在")

    def update_template_file_role(self, *, template_id: str, filename: str, file_role: str) -> Dict[str, Any]:
        template = self.get_template(template_id)
        if template is None:
            raise ValueError("模板不存在")
        safe_name, target, meta, manifest = self._resolve_template_file(
            template_id=template["template_id"],
            filename=filename,
        )
        files = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
        meta["file_role"] = self.normalize_file_role(file_role)
        meta["updated_at"] = datetime.utcnow().isoformat()
        files[safe_name] = meta
        manifest["files"] = files
        manifest["updated_at"] = datetime.utcnow().isoformat()
        self._write_json(self._manifest_path(template["template_id"]), manifest)
        return self.file_payload(target, file_meta=meta)

    def delete_template_file(self, *, template_id: str, filename: str) -> Dict[str, Any]:
        template = self.get_template(template_id)
        if template is None:
            raise ValueError("模板不存在")
        safe_name, target, _, manifest = self._resolve_template_file(
            template_id=template["template_id"],
            filename=filename,
        )

        target.unlink()
        files = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
        files.pop(safe_name, None)
        manifest["files"] = files
        manifest["updated_at"] = datetime.utcnow().isoformat()
        self._write_json(self._manifest_path(template["template_id"]), manifest)
        return {
            "template_id": template["template_id"],
            "file_name": safe_name,
            "deleted": True,
        }

    def copy_template_files_to_workspace(self, *, template_id: str, workspace_dir: Path) -> Dict[str, Any]:
        template = self.get_template(template_id)
        if template is None:
            raise ValueError("模板不存在")
        target_dir = workspace_dir / "template_files"
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: List[str] = []
        source_root = self._template_dir(template["template_id"]) / "files"
        manifest = self._load_manifest(template["template_id"])
        files_meta = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
        if source_root.exists():
            for path in sorted(source_root.rglob("*")):
                if not path.is_file():
                    continue
                meta = dict(files_meta.get(path.name) or {})
                target_name = self._original_filename(meta.get("original_filename") or path.name, fallback=path.name)
                target = self._unique_path(target_dir, target_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                copied.append(str(target))
        return {
            "template": template,
            "template_files_dir": str(target_dir),
            "copied_files": copied,
        }

    @staticmethod
    def _clean_text(value: str, *, limit: int = 0) -> str:
        text = re.sub(r"[ \t\r\f\v]+", " ", str(value or ""))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:limit] if limit and len(text) > limit else text

    @classmethod
    def _element_text(cls, node: ET.Element) -> str:
        return "".join(item.text or "" for item in node.iter(_w("t")))

    @classmethod
    def _style_map_from_docx(cls, archive: zipfile.ZipFile) -> Dict[str, str]:
        try:
            root = ET.fromstring(archive.read("word/styles.xml"))
        except Exception:
            return {}
        style_map: Dict[str, str] = {}
        for style in root.findall(f".//{_w('style')}"):
            style_id = str(style.attrib.get(_w("styleId")) or "").strip()
            name_node = style.find(_w("name"))
            name = str(name_node.attrib.get(_w("val")) or "").strip() if name_node is not None else ""
            if style_id:
                style_map[style_id] = name or style_id
        return style_map

    @classmethod
    def _paragraph_style_id(cls, paragraph: ET.Element) -> str:
        p_pr = paragraph.find(_w("pPr"))
        if p_pr is None:
            return ""
        style = p_pr.find(_w("pStyle"))
        return str(style.attrib.get(_w("val")) or "").strip() if style is not None else ""

    @classmethod
    def _extract_docx_analysis(cls, path: Path, *, role: str) -> Dict[str, Any]:
        with zipfile.ZipFile(path) as archive:
            style_map = cls._style_map_from_docx(archive)
            root = ET.fromstring(archive.read("word/document.xml"))
            paragraphs: List[Dict[str, str]] = []
            headings: List[Dict[str, str]] = []
            for paragraph in root.findall(f".//{_w('p')}"):
                text = cls._clean_text(cls._element_text(paragraph), limit=800)
                if not text:
                    continue
                style_id = cls._paragraph_style_id(paragraph)
                style_name = style_map.get(style_id, style_id)
                item = {"style_id": style_id, "style_name": style_name, "text": text}
                paragraphs.append(item)
                lowered = f"{style_id} {style_name}".lower()
                if "heading" in lowered or "title" in lowered or "标题" in style_name:
                    headings.append(item)
                if len(paragraphs) >= 120:
                    break

            tables: List[Dict[str, Any]] = []
            for table in root.findall(f".//{_w('tbl')}")[:12]:
                rows: List[List[str]] = []
                for row in table.findall(_w("tr"))[:6]:
                    cells = [cls._clean_text(cls._element_text(cell), limit=240) for cell in row.findall(_w("tc"))]
                    rows.append([cell for cell in cells if cell])
                tables.append({"sample_rows": rows, "row_sample_count": len(rows)})

            header_footer: List[Dict[str, str]] = []
            for name in archive.namelist():
                if not re.match(r"word/(?:header|footer)\d+\.xml$", name):
                    continue
                try:
                    xml_root = ET.fromstring(archive.read(name))
                except Exception:
                    continue
                text = cls._clean_text(cls._element_text(xml_root), limit=800)
                if text:
                    header_footer.append({"part": name, "text": text})

            section = root.find(f".//{_w('sectPr')}")
            page_setup: Dict[str, Any] = {}
            if section is not None:
                pg_sz = section.find(_w("pgSz"))
                pg_mar = section.find(_w("pgMar"))
                if pg_sz is not None:
                    page_setup["page_size"] = dict(pg_sz.attrib)
                if pg_mar is not None:
                    page_setup["page_margin"] = dict(pg_mar.attrib)

            return {
                "file": path.name,
                "role": role,
                "kind": "docx_ooxml",
                "paragraph_count_sampled": len(paragraphs),
                "headings": headings[:40],
                "paragraphs": paragraphs[:80],
                "tables": tables,
                "header_footer": header_footer,
                "style_names": sorted(set(style_map.values()))[:120],
                "page_setup": page_setup,
            }

    @classmethod
    def _extract_plain_docx_text(cls, path: Path, *, limit: int = 16000) -> str:
        analysis = cls._extract_docx_analysis(path, role="writing_guide")
        parts = [str(item.get("text") or "") for item in list(analysis.get("paragraphs") or [])]
        for table in list(analysis.get("tables") or []):
            for row in list(table.get("sample_rows") or []):
                parts.append(" | ".join(str(cell) for cell in row))
        return cls._clean_text("\n".join(parts), limit=limit)

    @classmethod
    def _extract_legacy_doc_text(cls, path: Path, *, limit: int = 12000) -> str:
        data = path.read_bytes()[:2_000_000]
        candidates: List[str] = []
        for encoding in ("utf-16le", "gb18030", "utf-8", "latin1"):
            try:
                decoded = data.decode(encoding, errors="ignore")
            except Exception:
                continue
            chunks = re.findall(r"[\u4e00-\u9fffA-Za-z0-9，。；：、（）《》“”！？\s]{8,}", decoded)
            cleaned = cls._clean_text("\n".join(chunks), limit=limit)
            if cleaned:
                candidates.append(cleaned)
        return max(candidates, key=len, default="")

    @staticmethod
    def _which(*names: str) -> str:
        for name in names:
            path = shutil.which(name)
            if path:
                return path
        return ""

    @staticmethod
    def _detect_document_suffix(path: Path, *, file_meta: Optional[Dict[str, Any]] = None) -> str:
        meta = dict(file_meta or {})
        original_name = str(meta.get("original_filename") or "").strip()
        suffix = Path(original_name).suffix.lower() if original_name else ""
        if not suffix:
            suffix = path.suffix.lower()
        if suffix:
            return suffix
        try:
            header = path.read_bytes()[:8]
        except Exception:
            return ""
        if header.startswith(b"PK"):
            return ".docx"
        if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return ".doc"
        return ""

    @classmethod
    def _pandoc_to_markdown(cls, path: Path, *, source_format: str = "") -> Dict[str, Any]:
        pandoc = cls._which("pandoc")
        if not pandoc:
            return {"ok": False, "text": "", "error": "pandoc_not_installed"}
        command = [pandoc]
        if source_format:
            command.extend(["-f", source_format])
        command.extend([str(path), "-t", "gfm", "--wrap=none"])
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "text": "", "error": "pandoc_timeout", "command": command}
        except Exception as exc:
            return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}", "command": command}
        text = cls._clean_text(completed.stdout, limit=50000)
        return {
            "ok": completed.returncode == 0 and bool(text),
            "text": text,
            "stderr": cls._clean_text(completed.stderr, limit=4000),
            "returncode": completed.returncode,
            "command": command,
        }

    @classmethod
    def _convert_doc_to_docx(cls, path: Path, output_dir: Path) -> Dict[str, Any]:
        soffice = cls._which("soffice", "libreoffice")
        if not soffice:
            return {"ok": False, "path": "", "error": "libreoffice_not_installed"}
        command = [
            soffice,
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(output_dir),
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "path": "", "error": "libreoffice_timeout", "command": command}
        except Exception as exc:
            return {"ok": False, "path": "", "error": f"{type(exc).__name__}: {exc}", "command": command}
        candidates = sorted(output_dir.glob("*.docx"), key=lambda item: item.stat().st_mtime, reverse=True)
        converted = candidates[0] if candidates else output_dir / f"{path.stem}.docx"
        return {
            "ok": completed.returncode == 0 and converted.is_file(),
            "path": str(converted) if converted.is_file() else "",
            "stdout": cls._clean_text(completed.stdout, limit=2000),
            "stderr": cls._clean_text(completed.stderr, limit=4000),
            "returncode": completed.returncode,
            "command": command,
        }

    @classmethod
    def _extract_text_file(cls, path: Path, *, limit: int = 16000) -> str:
        if path.suffix.lower() == ".docx":
            return cls._extract_plain_docx_text(path, limit=limit)
        if path.suffix.lower() == ".doc":
            return cls._extract_legacy_doc_text(path, limit=limit)
        if path.suffix.lower() in {".txt", ".md", ".markdown", ".json", ".csv"}:
            return cls._clean_text(path.read_text(encoding="utf-8", errors="ignore"), limit=limit)
        return ""

    def build_template_analysis(self, template_id: str) -> Dict[str, Any]:
        template = self.get_template(template_id)
        if template is None:
            raise ValueError("模板不存在")
        root = self._template_dir(template["template_id"])
        files_dir = root / "files"
        analysis_dir = root / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analyzed_files: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for file_payload in list(template.get("files") or []):
            path = Path(str(file_payload.get("path") or ""))
            role = self.normalize_file_role(file_payload.get("file_role"))
            if not path.is_file():
                continue
            file_meta = {
                "original_filename": file_payload.get("original_filename") or file_payload.get("name"),
                "media_type": file_payload.get("media_type"),
            }
            suffix = self._detect_document_suffix(path, file_meta=file_meta)
            display_name = str(file_payload.get("name") or path.name)
            stored_name = str(file_payload.get("stored_name") or path.name)
            try:
                markdown_text = ""
                markdown_artifact = ""
                docx_source = path
                converted_from_doc = False
                if suffix == ".doc":
                    with tempfile.TemporaryDirectory(prefix="docx-template-") as tmp:
                        conversion = self._convert_doc_to_docx(path, Path(tmp))
                        if conversion.get("ok") and conversion.get("path"):
                            converted_from_doc = True
                            converted_name = self.safe_filename(
                                f"{Path(display_name).stem or stored_name}.converted.docx",
                                fallback="converted.docx",
                            )
                            converted_copy = self._unique_path(analysis_dir, converted_name)
                            shutil.copyfile(Path(str(conversion["path"])), converted_copy)
                            docx_source = converted_copy
                            suffix = ".docx"
                        else:
                            warnings.append(f"{display_name}: LibreOffice 转 docx 失败：{conversion.get('error') or conversion.get('stderr') or 'unknown'}")
                            legacy_text = self._extract_legacy_doc_text(path)
                            analyzed_files.append(
                                {
                                    "file": display_name,
                                    "stored_file": stored_name,
                                    "role": role,
                                    "kind": "legacy_doc_best_effort_text",
                                    "detected_suffix": ".doc",
                                    "text": legacy_text,
                                    "text_chars": len(legacy_text),
                                    "conversion": conversion,
                                }
                            )
                            continue
                        pandoc_result = self._pandoc_to_markdown(docx_source, source_format="docx")
                        if pandoc_result.get("ok"):
                            markdown_text = str(pandoc_result.get("text") or "")
                    if markdown_text:
                        artifact_name = self.safe_filename(f"{Path(display_name).stem or stored_name}.pandoc.md", fallback="extracted.md")
                        markdown_path = analysis_dir / artifact_name
                        markdown_path.write_text(markdown_text, encoding="utf-8")
                        markdown_artifact = str(markdown_path)
                elif suffix == ".docx":
                    pandoc_result = self._pandoc_to_markdown(path, source_format="docx")
                    if pandoc_result.get("ok"):
                        markdown_text = str(pandoc_result.get("text") or "")
                        artifact_name = self.safe_filename(f"{Path(display_name).stem or stored_name}.pandoc.md", fallback="extracted.md")
                        markdown_path = analysis_dir / artifact_name
                        markdown_path.write_text(markdown_text, encoding="utf-8")
                        markdown_artifact = str(markdown_path)
                    else:
                        warnings.append(f"{display_name}: Pandoc 转 Markdown 失败：{pandoc_result.get('error') or pandoc_result.get('stderr') or 'unknown'}")

                if role == "sample_template":
                    if suffix == ".docx":
                        ooxml_analysis = self._extract_docx_analysis(docx_source, role=role)
                        analyzed_files.append(
                            {
                                "file": display_name,
                                "stored_file": stored_name,
                                "role": role,
                                "kind": "sample_template_docx",
                                "detected_suffix": ".docx",
                                "converted_from_doc": converted_from_doc,
                                "pandoc_markdown": markdown_text[:30000],
                                "pandoc_markdown_chars": len(markdown_text),
                                "pandoc_markdown_path": markdown_artifact,
                                "ooxml": ooxml_analysis,
                            }
                        )
                    else:
                        analyzed_files.append(
                            {
                                "file": display_name,
                                "stored_file": stored_name,
                                "role": role,
                                "kind": "unsupported_template_format",
                                "detected_suffix": suffix,
                                "note": "该格式不能解析版式，只会作为 Claude 参考附件。",
                            }
                        )
                elif role == "writing_guide":
                    if suffix == ".docx":
                        text = markdown_text or self._extract_plain_docx_text(docx_source)
                    elif suffix in {".txt", ".md", ".markdown", ".json", ".csv"}:
                        text = self._extract_text_file(path)
                    else:
                        text = self._extract_text_file(path)
                    analyzed_files.append(
                        {
                            "file": display_name,
                            "stored_file": stored_name,
                            "role": role,
                            "kind": f"{suffix.lstrip('.') or 'file'}_text",
                            "detected_suffix": suffix,
                            "converted_from_doc": converted_from_doc,
                            "pandoc_markdown_path": markdown_artifact,
                            "text": text,
                            "text_chars": len(text),
                        }
                    )
                    if not text:
                        warnings.append(f"{display_name}: 没有提取到可用文本。")
                else:
                    text = ""
                    if suffix == ".docx":
                        text = markdown_text[:12000]
                    elif suffix in {".txt", ".md", ".markdown", ".json", ".csv"}:
                        text = self._extract_text_file(path, limit=12000)
                    analyzed_files.append(
                        {
                            "file": display_name,
                            "stored_file": stored_name,
                            "role": role,
                            "kind": "reference_only",
                            "detected_suffix": suffix,
                            "text_sample": text,
                            "text_sample_chars": len(text),
                            "pandoc_markdown_path": markdown_artifact,
                            "note": "普通参考附件不主动总结约束，生成 DOCX 时会交给 Claude 参考。",
                        }
                    )
            except Exception as exc:
                warnings.append(f"{display_name}: 解析失败 {type(exc).__name__}: {exc}")

        analysis = {
            "template_id": template["template_id"],
            "template_name": template.get("name") or template["template_id"],
            "template_description": template.get("description") or "",
            "files_dir": str(files_dir),
            "warnings": warnings,
            "files": analyzed_files,
        }
        self._write_json(analysis_dir / "docx_template_analysis.json", analysis)
        return analysis

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        candidate = match.group(1) if match else raw
        if not candidate.startswith("{"):
            start = candidate.find("{")
            end = candidate.rfind("}")
            candidate = candidate[start:end + 1] if start >= 0 and end > start else candidate
        try:
            payload = json.loads(candidate)
            return dict(payload or {}) if isinstance(payload, dict) else {}
        except Exception:
            return {}

    async def generate_constraints_with_llm(self, template_id: str, *, user_notes: str = "") -> Dict[str, Any]:
        from app.services.llm_service import LLMService

        analysis = self.build_template_analysis(template_id)
        system_prompt = (
            "你是科研文档模板分析器。根据用户上传的模板样例和撰写说明，生成两段边界清晰、可编辑的约束。"
            "只输出 JSON，不要输出额外解释。JSON 字段内必须使用可读 Markdown，不要输出单段长文本。"
        )
        user_prompt = "\n".join(
            [
                "请基于以下文件分析结果生成约束草稿。",
                "",
                "输出 JSON schema:",
                '{"md_constraints":"...","docx_constraints":"...","notes":"..."}',
                "",
                "全局格式要求：",
                "- md_constraints、docx_constraints、notes 三个字段的值都必须是 Markdown 字符串。",
                "- 必须使用二级/三级标题和项目符号分组，禁止输出没有换行的长段落。",
                "- 只记录模板或指南明确支持的信息；不确定的地方写“模板未明确要求”，不要编造。",
                "- 可以引用平台默认样式作为兜底，但必须标注为“平台默认”，不能写成“模板要求”。",
                "",
                "md_constraints 的边界：",
                "- 面向平台默认 LLM，用来生成结构化 Markdown 草稿。",
                "- 只能包含：文档组成部分、章节层级、字段/占位符、每节写作目标、内容口径、字数或篇幅要求、证据/引用/参考文献内容要求、需要用户补充的信息。",
                "- 禁止包含：字体、字号、磅数、行距、段前段后、页边距、纸张大小、页眉页脚、页码、目录样式、Word 样式名、OOXML 属性/单位、图表题注字体格式、公式排版格式。",
                "- 如果 Pandoc/text 中只有版式信息，没有内容写作要求，对应部分写“模板未明确内容要求”。",
                "",
                "docx_constraints 的边界：",
                "- 面向 Claude Code/document skill，用来把 Markdown 转成 DOCX。",
                "- 只能包含 Word/DOCX 生成相关要求：页面设置、分节、封面、目录、页眉页脚、页码、标题层级样式、正文段落样式、图表、公式、参考文献、自动目录、交叉引用、书签、模板文件使用方式。",
                "- docx_constraints 必须按这些模块组织：页面设置、封面与目录、分节与页码、页眉页脚、标题层级、正文段落、图表、公式、参考文献、自动结构、模板文件使用。",
                "- 每个模块只写明确抽取到的要求；如果模板未提供图表/公式/交叉引用等样式，该模块写“模板未明确要求；如生成内容包含该类元素，使用平台默认样式”。",
                "- OOXML 页面尺寸、边距等必须保留原单位名，例如 twips；不要擅自写成 EMU。",
                "",
                "文件角色解释：",
                "- 成品/样例模板优先影响 docx_constraints，也可抽取章节结构给 md_constraints。",
                "- 文件分析里的 pandoc_markdown/text 是内容结构和写作说明，ooxml 是 Word 样式、页眉页脚、表格和页面结构。",
                "- 撰写说明/填报指南优先从 text/pandoc_markdown 提取 md_constraints。",
                "- 普通参考附件只说明生成时可参考，不要强行提炼不存在的格式。",
                "- 如果 warnings 里提示 Pandoc 或 LibreOffice 不可用/失败，在 notes 中明确提醒。",
                "",
                "notes 要求：",
                "- 用 Markdown 列表输出。",
                "- 简要说明使用了哪些文件、是否有警告、哪些信息是模板明确要求、哪些是平台默认兜底。",
                f"- 用户补充说明：{user_notes or '无'}",
                "",
                "文件分析结果 JSON:",
                json.dumps(analysis, ensure_ascii=False, indent=2)[:50000],
            ]
        )
        response = await LLMService().chat(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.2,
            max_tokens=4096,
            source="docx_template.analyze",
        )
        content = str(response.get("content") or "")
        parsed = self._extract_json_object(content)
        md_constraints = str(parsed.get("md_constraints") or "").strip()
        docx_constraints = str(parsed.get("docx_constraints") or "").strip()
        notes = str(parsed.get("notes") or "").strip()
        root = self._template_dir(analysis["template_id"])
        analysis_dir = root / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "generated_md_constraints.md").write_text(md_constraints, encoding="utf-8")
        (analysis_dir / "generated_docx_constraints.md").write_text(docx_constraints, encoding="utf-8")
        (analysis_dir / "analysis_notes.md").write_text(notes or content, encoding="utf-8")
        return {
            "template_id": analysis["template_id"],
            "md_constraints": md_constraints,
            "docx_constraints": docx_constraints,
            "notes": notes,
            "raw_model_output": content,
            "analysis": analysis,
            "artifacts": {
                "analysis_json": str(analysis_dir / "docx_template_analysis.json"),
                "generated_md_constraints": str(analysis_dir / "generated_md_constraints.md"),
                "generated_docx_constraints": str(analysis_dir / "generated_docx_constraints.md"),
                "analysis_notes": str(analysis_dir / "analysis_notes.md"),
            },
        }

    def resolve_download_path(self, relative_path: str) -> Optional[Path]:
        raw = str(relative_path or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or "\x00" in raw:
            return None
        self.ensure_roots()
        try:
            candidate = (self.docx_root / raw).resolve()
            candidate.relative_to(self.docx_root.resolve())
        except Exception:
            return None
        return candidate if candidate.is_file() else None

    def _workspace_payload_from_path(self, path: Path) -> Dict[str, Any]:
        stat = path.stat()
        metadata = self._read_json(path / "docx_request.json")
        output_basename = str(metadata.get("output_basename") or "generated_document").strip() or "generated_document"
        docx_path = path / f"{output_basename}.docx"
        pdf_path = path / f"{output_basename}.pdf"
        return {
            "docx_id": str(metadata.get("docx_id") or path.name),
            "template_id": str(metadata.get("template_id") or ""),
            "template_name": str(metadata.get("template_name") or ""),
            "artifact_id": str(metadata.get("artifact_id") or ""),
            "conversation_id": metadata.get("conversation_id"),
            "user_id": metadata.get("user_id"),
            "path": str(path),
            "workspace_path": str(path),
            "source_path": str(metadata.get("source_file") or metadata.get("source_path") or ""),
            "requirements_path": str(metadata.get("requirements_file") or metadata.get("requirements_path") or ""),
            "output_basename": output_basename,
            "docx_path": str(docx_path) if docx_path.is_file() else str(metadata.get("docx_path") or ""),
            "pdf_path": str(pdf_path) if pdf_path.is_file() else str(metadata.get("pdf_path") or ""),
            "status": str(metadata.get("status") or "unknown"),
            "validation_status": str(metadata.get("validation_status") or ""),
            "session_id": str(metadata.get("session_id") or ""),
            "error": str(metadata.get("error") or ""),
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
            "files": self._list_files(path),
        }

    def _workspace_payload_from_job(self, job: DocxGenerationJob) -> Dict[str, Any]:
        workspace_path = Path(str(job.workspace_path or ""))
        files = self._list_files(workspace_path) if workspace_path.is_dir() else []
        return {
            "docx_id": str(job.docx_id or ""),
            "template_id": str(job.template_id or ""),
            "template_name": str(job.template_name or ""),
            "artifact_id": str(job.artifact_id or ""),
            "conversation_id": job.conversation_id,
            "user_id": job.user_id,
            "path": str(job.workspace_path or ""),
            "workspace_path": str(job.workspace_path or ""),
            "source_path": str(job.source_path or ""),
            "requirements_path": str(job.requirements_path or ""),
            "output_basename": str(job.output_basename or ""),
            "docx_path": str(job.docx_path or ""),
            "pdf_path": str(job.pdf_path or ""),
            "status": str(job.status or ""),
            "validation_status": str(job.validation_status or ""),
            "session_id": str(job.claude_session_id or ""),
            "error": str(job.error_message or ""),
            "modified_at": (job.updated_at or job.created_at or datetime.utcnow()).isoformat(),
            "files": files,
        }

    def _scanned_templates(self) -> List[Dict[str, Any]]:
        templates = []
        for path in sorted(self.templates_root.iterdir()) if self.templates_root.exists() else []:
            if path.is_dir():
                template = self.get_template(path.name)
                if template is not None:
                    templates.append(template)
        return templates

    def _scanned_workspaces(self) -> List[Dict[str, Any]]:
        workspaces = []
        for path in sorted(self.docx_root.iterdir()) if self.docx_root.exists() else []:
            if not path.is_dir() or path.name in {"templates", "artifacts"}:
                continue
            workspaces.append(self._workspace_payload_from_path(path))
        return workspaces

    async def list_overview_for_user(
        self,
        db: AsyncSession,
        *,
        user_id: Optional[int],
    ) -> Dict[str, Any]:
        self.ensure_roots()
        templates = self._scanned_templates()
        for template in templates:
            await self.sync_template_to_db(db, user_id=user_id, template=template)

        scanned_workspaces = self._scanned_workspaces()
        for workspace in scanned_workspaces:
            await self.upsert_generation_job(
                db,
                user_id=int(workspace["user_id"]) if str(workspace.get("user_id") or "").isdigit() else user_id,
                job={
                    **workspace,
                    "workspace_dir": workspace.get("workspace_path"),
                    "status": workspace.get("status") or "unknown",
                    "metadata": {"source": "filesystem_scan"},
                },
            )

        await db.commit()
        result = await db.execute(select(DocxGenerationJob).order_by(desc(DocxGenerationJob.updated_at)).limit(300))
        job_rows = list(result.scalars().all())
        workspaces = [self._workspace_payload_from_job(row) for row in job_rows]
        known_docx_ids = {str(item.get("docx_id") or "") for item in workspaces}
        for workspace in scanned_workspaces:
            if str(workspace.get("docx_id") or "") not in known_docx_ids:
                workspaces.append(workspace)
        return {
            "docx_root": str(self.docx_root),
            "templates_root": str(self.templates_root),
            "default_docx_style_prompt": self.get_default_docx_style_prompt(),
            "templates": templates,
            "workspaces": workspaces,
        }

    def resolve_download_file(self, relative_path: str) -> Optional[Dict[str, Any]]:
        path = self.resolve_download_path(relative_path)
        if path is None:
            return None
        filename = path.name
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            relative = path.resolve().relative_to(self.templates_root.resolve())
            parts = relative.parts
            if len(parts) >= 3 and parts[1] == "files":
                template_id = parts[0]
                stored_name = parts[-1]
                manifest = self._load_manifest(template_id)
                files = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
                meta = dict(files.get(stored_name) or {})
                filename = str(meta.get("original_filename") or filename).strip() or filename
                if not Path(filename).suffix:
                    detected_suffix = self._detect_document_suffix(path, file_meta=meta)
                    if detected_suffix in {".doc", ".docx"}:
                        filename = f"{filename}{detected_suffix}"
                media_type = (
                    str(meta.get("media_type") or "").strip()
                    or mimetypes.guess_type(filename)[0]
                    or mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream"
                )
        except Exception:
            pass
        return {
            "path": path,
            "filename": filename,
            "media_type": media_type,
        }

    def list_overview(self) -> Dict[str, Any]:
        self.ensure_roots()
        return {
            "docx_root": str(self.docx_root),
            "templates_root": str(self.templates_root),
            "default_docx_style_prompt": self.get_default_docx_style_prompt(),
            "templates": self._scanned_templates(),
            "workspaces": self._scanned_workspaces(),
        }
