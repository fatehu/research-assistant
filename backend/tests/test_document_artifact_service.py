from app.services.document_artifact_service import DocumentArtifactService


def test_normalize_schema_preserves_editable_blocks(tmp_path):
    service = DocumentArtifactService(upload_root=tmp_path)

    artifact = service.normalize_schema(
        {
            "title": "面上项目正文",
            "global_constraints": ["使用科研项目申请书语气", "所有章节保留证据口径"],
            "blocks": [
                {
                    "block_id": "basis",
                    "title": "立项依据",
                    "heading_path": ["正文", "立项依据"],
                    "target_words": "1200",
                    "block_constraints": ["说明科学问题", "引用前期基础"],
                    "markdown": "## 立项依据\n",
                }
            ],
        },
        template_id="nsfc-general",
        artifact_id="artifact-1",
    )

    assert artifact["schema_version"] == "document_artifact.v1"
    assert artifact["artifact_id"] == "artifact-1"
    assert artifact["template_id"] == "nsfc-general"
    assert "科研项目申请书" in artifact["global_constraints"]
    assert artifact["blocks"][0]["block_id"] == "basis"
    assert artifact["blocks"][0]["target_words"] == 1200
    assert "科学问题" in artifact["blocks"][0]["block_constraints"]


def test_normalize_schema_adds_body_block_when_llm_returns_empty(tmp_path):
    service = DocumentArtifactService(upload_root=tmp_path)

    artifact = service.normalize_schema(
        {"title": "文档", "global_constraints": "整体约束"},
        template_id="template-a",
        artifact_id="artifact-2",
    )

    assert len(artifact["blocks"]) == 1
    assert artifact["blocks"][0]["block_id"] == "body"
    assert artifact["blocks"][0]["title"] == "正文"
    assert artifact["blocks"][0]["block_constraints"] == "整体约束"
