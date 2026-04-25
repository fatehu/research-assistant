from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.services.docx_template_service import DocxTemplateService
from app.services.llm_service import LLMService


class DocumentArtifactService:
    """File-backed active document artifact bound to one conversation."""

    METADATA_KEY = "active_document_artifact"
    SCHEMA_VERSION = "document_artifact.v1"

    def __init__(self, upload_root: Optional[Path] = None) -> None:
        self.upload_root = upload_root or self._default_upload_root()
        self.artifacts_root = self.upload_root / "docx" / "artifacts"

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
    def safe_slug(value: Any, *, fallback: str = "artifact") -> str:
        text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
        return (text or fallback)[:120]

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload or {}) if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
        candidate = match.group(1) if match else raw
        if not candidate.startswith("{"):
            start = candidate.find("{")
            end = candidate.rfind("}")
            candidate = candidate[start : end + 1] if start >= 0 and end > start else candidate
        try:
            parsed = json.loads(candidate)
        except Exception:
            return {}
        return dict(parsed or {}) if isinstance(parsed, dict) else {}

    def _artifact_dir(self, conversation_id: int, artifact_id: str) -> Path:
        safe_artifact_id = self.safe_slug(artifact_id, fallback="artifact")
        return self.artifacts_root / str(int(conversation_id)) / safe_artifact_id

    def _artifact_path(self, conversation_id: int, artifact_id: str) -> Path:
        return self._artifact_dir(conversation_id, artifact_id) / "artifact.json"

    async def _load_conversation(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        conversation_id: int,
    ) -> Conversation:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == int(conversation_id),
                Conversation.user_id == int(user_id),
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise ValueError("对话不存在")
        return conversation

    def _write_artifact_file(self, conversation_id: int, artifact: Dict[str, Any]) -> Path:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if not artifact_id:
            raise ValueError("artifact_id 不能为空")
        target = self._artifact_path(int(conversation_id), artifact_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def _metadata_pointer(self, *, artifact: Dict[str, Any], path: Path) -> Dict[str, Any]:
        return {
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "template_id": str(artifact.get("template_id") or ""),
            "title": str(artifact.get("title") or ""),
            "path": str(path),
            "updated_at": str(artifact.get("updated_at") or self._now()),
        }

    def _active_path_from_conversation(self, conversation: Conversation) -> Optional[Path]:
        metadata = dict(conversation.metadata_ or {}) if isinstance(conversation.metadata_, dict) else {}
        pointer = metadata.get(self.METADATA_KEY)
        if not isinstance(pointer, dict):
            return None
        raw_path = str(pointer.get("path") or "").strip()
        artifact_id = str(pointer.get("artifact_id") or "").strip()
        if raw_path:
            return Path(raw_path)
        if artifact_id:
            return self._artifact_path(int(conversation.id), artifact_id)
        return None

    def _normalize_block(self, raw: Any, *, index: int) -> Dict[str, Any]:
        item = dict(raw or {}) if isinstance(raw, dict) else {}
        block_id = self.safe_slug(item.get("block_id") or item.get("id") or f"block-{index + 1}", fallback=f"block-{index + 1}")
        title = str(item.get("title") or item.get("name") or f"章节 {index + 1}").strip()
        constraints = item.get("block_constraints")
        if isinstance(constraints, list):
            constraints_text = "\n".join(f"- {str(value).strip()}" for value in constraints if str(value).strip())
        else:
            constraints_text = str(constraints or item.get("constraints") or "").strip()
        try:
            target_words = int(item.get("target_words") or 0)
        except Exception:
            target_words = 0
        raw_heading_path = item.get("heading_path")
        heading_path = (
            [str(part).strip() for part in list(raw_heading_path) if str(part).strip()]
            if isinstance(raw_heading_path, list)
            else [title]
        )
        return {
            "block_id": block_id,
            "index": index,
            "title": title,
            "heading_path": heading_path or [title],
            "required": bool(item.get("required", True)),
            "target_words": max(target_words, 0),
            "block_constraints": constraints_text,
            "markdown": str(item.get("markdown") or "").strip(),
            "status": str(item.get("status") or "empty").strip() or "empty",
            "updated_at": str(item.get("updated_at") or self._now()),
        }

    def normalize_schema(
        self,
        schema: Dict[str, Any],
        *,
        template_id: str,
        title: str = "",
        artifact_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        raw = dict(schema or {})
        now = self._now()
        normalized_artifact_id = self.safe_slug(
            artifact_id or raw.get("artifact_id") or f"docart-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
            fallback="artifact",
        )
        global_constraints = raw.get("global_constraints")
        if isinstance(global_constraints, list):
            global_constraints_text = "\n".join(
                f"- {str(value).strip()}" for value in global_constraints if str(value).strip()
            )
        else:
            global_constraints_text = str(global_constraints or raw.get("constraints") or "").strip()

        blocks = [
            self._normalize_block(item, index=index)
            for index, item in enumerate(list(raw.get("blocks") or []))
            if isinstance(item, dict)
        ]
        if not blocks:
            blocks = [
                self._normalize_block(
                    {
                        "block_id": "body",
                        "title": "正文",
                        "required": True,
                        "block_constraints": global_constraints_text,
                        "markdown": "",
                    },
                    index=0,
                )
            ]

        return {
            "schema_version": self.SCHEMA_VERSION,
            "artifact_id": normalized_artifact_id,
            "template_id": str(template_id or raw.get("template_id") or "").strip(),
            "title": str(title or raw.get("title") or "文档草稿").strip() or "文档草稿",
            "global_constraints": global_constraints_text,
            "blocks": blocks,
            "created_at": str(raw.get("created_at") or now),
            "updated_at": now,
        }

    def _fallback_schema(self, *, template: Dict[str, Any], title: str, user_notes: str = "") -> Dict[str, Any]:
        md_constraints = str(template.get("md_constraints") or "").strip()
        block_title = str(title or template.get("name") or "正文").strip() or "正文"
        constraints = "\n".join(item for item in [md_constraints, str(user_notes or "").strip()] if item)
        return self.normalize_schema(
            {
                "title": block_title,
                "global_constraints": constraints,
                "blocks": [
                    {
                        "block_id": "body",
                        "title": "正文",
                        "required": True,
                        "block_constraints": constraints,
                        "markdown": "",
                    }
                ],
            },
            template_id=str(template.get("template_id") or ""),
            title=block_title,
        )

    async def generate_schema_from_template(
        self,
        *,
        template_id: str,
        title: str = "",
        user_notes: str = "",
    ) -> Dict[str, Any]:
        template_service = DocxTemplateService(upload_root=self.upload_root)
        template = template_service.get_template(template_id)
        if template is None:
            raise ValueError("模板不存在")

        md_constraints = str(template.get("md_constraints") or "").strip()
        if not md_constraints:
            return self._fallback_schema(template=template, title=title, user_notes=user_notes)

        system_prompt = (
            "你是科研文档结构规划器。根据模板的 Markdown 生成约束，抽取细粒度、可编辑的 section/block schema。"
            "只输出 JSON，不要输出解释。"
        )
        user_prompt = "\n".join(
            [
                "请把模板约束转换为文档 artifact schema。",
                "",
                "输出 JSON 格式：",
                json.dumps(
                    {
                        "title": title or template.get("name") or "文档草稿",
                        "global_constraints": "整体写作约束、证据要求、统一口径。",
                        "blocks": [
                            {
                                "block_id": "section-id",
                                "title": "章节标题",
                                "heading_path": ["一级标题", "二级标题"],
                                "required": True,
                                "target_words": 800,
                                "block_constraints": "本章节的写作要求、内容要点、禁止事项。",
                                "markdown": "可选：给用户编辑的初始 Markdown 骨架。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "",
                "要求：",
                "- blocks 必须尽量细粒度拆分，不要只按大章节拆。凡是用户可能单独阅读、修改、生成或重写的内容，都拆成独立 block。",
                "- 对科研项目模板，一级章节、二级小节、三级条目、表格、预算说明、参与者信息、附件清单、AI 使用说明、风险应对、年度计划、创新点等都应尽量拆开。",
                "- 如果一个章节包含多个问题或括号提示，按问题拆成多个 block；如果包含编号列表，优先按编号项拆 block。",
                "- 每个 block 应保持单一写作目的，目标长度通常不超过 300-800 字；长章节拆成多个连续 block。",
                "- blocks 必须覆盖模板要求中的所有主要章节、表单区域和说明性条目。",
                "- block_id 使用稳定英文/拼音 slug，不要用中文或空格。",
                "- title 使用用户可理解的中文短标题；heading_path 保留其在原模板中的层级位置。",
                "- global_constraints 放整体约束；每个 block_constraints 只放该区域相关约束。",
                "- markdown 只写骨架和占位，不要替用户生成正文。",
                "- 如果约束里有字数、编号、必填项，要放入对应 block。",
                "- 不要为了减少 blocks 合并不同写作任务；宁可多拆，也不要过粗。",
                f"- 用户本次补充说明：{user_notes or '无'}",
                "",
                "模板信息：",
                json.dumps(
                    {
                        "template_id": template.get("template_id"),
                        "name": template.get("name"),
                        "description": template.get("description"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "",
                "MD 生成约束：",
                md_constraints[:60000],
            ]
        )
        response = await LLMService().chat(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=6000,
            source="document_artifact.schema",
        )
        content = str(response.get("content") or "")
        parsed = self._extract_json_object(content)
        if not parsed:
            return self._fallback_schema(template=template, title=title, user_notes=user_notes)
        return self.normalize_schema(
            parsed,
            template_id=str(template.get("template_id") or template_id),
            title=title or str(parsed.get("title") or template.get("name") or "文档草稿"),
        )

    async def get_active_artifact(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        conversation_id: int,
    ) -> Optional[Dict[str, Any]]:
        conversation = await self._load_conversation(db, user_id=user_id, conversation_id=conversation_id)
        path = self._active_path_from_conversation(conversation)
        if path is None or not path.is_file():
            return None
        artifact = self._read_json(path)
        return artifact or None

    async def create_active_artifact(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        conversation_id: int,
        template_id: str,
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        conversation = await self._load_conversation(db, user_id=user_id, conversation_id=conversation_id)
        artifact = self.normalize_schema(
            schema,
            template_id=template_id,
            title=str(schema.get("title") or ""),
        )
        path = self._write_artifact_file(int(conversation.id), artifact)

        metadata = dict(conversation.metadata_ or {}) if isinstance(conversation.metadata_, dict) else {}
        metadata[self.METADATA_KEY] = self._metadata_pointer(artifact=artifact, path=path)
        conversation.metadata_ = metadata
        await db.commit()
        await db.refresh(conversation)
        return artifact

    async def clone_active_artifact_to_conversation(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        source_conversation_id: int,
        target_conversation: Conversation,
    ) -> Optional[Dict[str, Any]]:
        source_artifact = await self.get_active_artifact(
            db,
            user_id=int(user_id),
            conversation_id=int(source_conversation_id),
        )
        if source_artifact is None:
            return None

        now = self._now()
        cloned = json.loads(json.dumps(source_artifact, ensure_ascii=False))
        cloned["artifact_id"] = self.safe_slug(
            f"{source_artifact.get('artifact_id') or 'artifact'}-branch-{int(target_conversation.id)}",
            fallback=f"artifact-branch-{int(target_conversation.id)}",
        )
        cloned["created_at"] = now
        cloned["updated_at"] = now
        for block in list(cloned.get("blocks") or []):
            if isinstance(block, dict):
                block["updated_at"] = now

        path = self._write_artifact_file(int(target_conversation.id), cloned)
        metadata = dict(target_conversation.metadata_ or {}) if isinstance(target_conversation.metadata_, dict) else {}
        metadata[self.METADATA_KEY] = self._metadata_pointer(artifact=cloned, path=path)
        target_conversation.metadata_ = metadata
        return cloned

    async def update_block(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        conversation_id: int,
        block_id: str,
        markdown: Optional[str] = None,
        title: Optional[str] = None,
        block_constraints: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        conversation = await self._load_conversation(db, user_id=user_id, conversation_id=conversation_id)
        path = self._active_path_from_conversation(conversation)
        if path is None or not path.is_file():
            raise ValueError("当前对话没有 active document artifact")
        artifact = self._read_json(path)
        target_id = str(block_id or "").strip()
        if not target_id:
            raise ValueError("block_id 不能为空")

        updated = False
        now = self._now()
        for block in list(artifact.get("blocks") or []):
            if not isinstance(block, dict) or str(block.get("block_id") or "") != target_id:
                continue
            if markdown is not None:
                block["markdown"] = str(markdown)
            if title is not None:
                block["title"] = str(title).strip() or str(block.get("title") or target_id)
            if block_constraints is not None:
                block["block_constraints"] = str(block_constraints)
            if status is not None:
                block["status"] = str(status).strip() or block.get("status") or "draft"
            elif markdown is not None:
                block["status"] = "draft"
            block["updated_at"] = now
            updated = True
            break

        if not updated:
            raise ValueError(f"未找到 block: {target_id}")

        artifact["updated_at"] = now
        self._write_artifact_file(int(conversation.id), artifact)
        metadata = dict(conversation.metadata_ or {}) if isinstance(conversation.metadata_, dict) else {}
        metadata[self.METADATA_KEY] = self._metadata_pointer(artifact=artifact, path=path)
        conversation.metadata_ = metadata
        await db.commit()
        return artifact

    async def read_blocks_for_tool(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        conversation_id: int,
        block_ids: Optional[List[str]] = None,
        include_constraints: bool = True,
        include_markdown: bool = True,
    ) -> Dict[str, Any]:
        artifact = await self.get_active_artifact(db, user_id=user_id, conversation_id=conversation_id)
        if artifact is None:
            raise ValueError("当前对话没有 active document artifact")
        selected = {str(item).strip() for item in list(block_ids or []) if str(item).strip()}
        blocks: List[Dict[str, Any]] = []
        for block in list(artifact.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            if selected and str(block.get("block_id") or "") not in selected:
                continue
            item = {
                "block_id": block.get("block_id"),
                "index": block.get("index"),
                "title": block.get("title"),
                "heading_path": block.get("heading_path"),
                "required": block.get("required"),
                "target_words": block.get("target_words"),
                "status": block.get("status"),
            }
            if include_constraints:
                item["block_constraints"] = block.get("block_constraints")
            if include_markdown:
                item["markdown"] = block.get("markdown")
            blocks.append(item)
        return {
            "artifact_id": artifact.get("artifact_id"),
            "template_id": artifact.get("template_id"),
            "title": artifact.get("title"),
            "global_constraints": artifact.get("global_constraints") if include_constraints else "",
            "blocks": blocks,
            "updated_at": artifact.get("updated_at"),
        }
