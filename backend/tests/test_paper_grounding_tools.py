import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.agent_tools_impl import registry as agent_tools
from app.services import project_reference_builder_service as project_reference_builder_service
from app.services.docx_template_service import DocxTemplateService
from app.services.project_runtime_service import ProjectRuntimeService
from app.services.project_service import ProjectService


def _project_payload() -> dict:
    return {"id": 7, "paper_id": 113}


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(id=21, notebook_id="nb-1", status="ready", title="workspace")


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return _FakeScalarResult(self._rows)


def _init_git_repo(repo_dir):
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)


def _write_minimal_docx(path: Path) -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>一、立项依据</w:t></w:r></w:p>
    <w:p><w:r><w:t>正文使用小四宋体，说明研究背景。</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>指标</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>说明</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="标题 1"/></w:style>
</w:styles>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)


@pytest.mark.asyncio
async def test_paper_search_tool_returns_ranked_candidates():
    rows = [
        SimpleNamespace(
            id=5,
            title="Attention Is All You Need",
            abstract="The Transformer architecture is introduced for sequence modeling.",
            authors=[{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
            year=2017,
            venue="NeurIPS",
            journal=None,
            arxiv_id="1706.03762",
        ),
        SimpleNamespace(
            id=8,
            title="Transformers for Vision",
            abstract="A vision transformer variant.",
            authors=[{"name": "Alex Example"}],
            year=2021,
            venue="ICCV",
            journal=None,
            arxiv_id=None,
        ),
    ]

    class _FakeDb:
        def __init__(self, responses):
            self._responses = list(responses)

        async def execute(self, _stmt):
            return _FakeExecuteResult(self._responses.pop(0))

    tool = agent_tools.PaperSearchTool(db=_FakeDb([rows, rows]), user_id=1)

    result = await tool._execute(query="attention is all you need", max_results=5)

    assert result.success is True
    assert result.data["query"] == "attention is all you need"
    assert result.data["candidates"][0]["paper_id"] == 5
    assert result.data["candidates"][0]["title"] == "Attention Is All You Need"
    assert result.data["candidates"][0]["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert result.data["candidates"][0]["year"] == 2017
    assert result.data["candidates"][0]["venue"] == "NeurIPS"
    assert "paper_id=5" in result.output
    assert "Attention Is All You Need" in result.output


@pytest.mark.asyncio
async def test_project_tree_tool_returns_project_directory_tree(tmp_path, monkeypatch):
    tool = agent_tools.ProjectTreeTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    (tmp_path / "reference" / "paper").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reference" / "paper" / "paper_interpretation.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# notes\n", encoding="utf-8")

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(tool, "_project_dir_for", lambda _project_id: tmp_path)
    async def _load_project_tree_focus_context(_db, *, project_payload):
        return {"project_goal": "inspect repo structure", "recent_tool_calls": [{"tool_name": "paper_research_status"}]}

    async def _summarize_project_tree_for_agent(*, tree: str, focus_context):
        assert "reference/" in tree
        assert focus_context["project_goal"] == "inspect repo structure"
        return ".\n├── reference/\n│   └── paper/\n│       └── paper_interpretation.json\n└── notes.md", [
            "reference/paper/paper_interpretation.json",
            "notes.md",
        ]

    monkeypatch.setattr(tool, "_load_project_tree_focus_context", _load_project_tree_focus_context)
    monkeypatch.setattr(tool, "_summarize_project_tree_for_agent", _summarize_project_tree_for_agent)

    result = await tool._execute(project_id=7)

    assert result.success is True
    assert result.data == {
        "project_id": 7,
        "tree": result.data["tree"],
        "focused_tree": ".\n├── reference/\n│   └── paper/\n│       └── paper_interpretation.json\n└── notes.md",
        "important_paths": ["reference/paper/paper_interpretation.json", "notes.md"],
    }
    assert "." in result.data["tree"]
    assert "reference/" in result.data["tree"]
    assert "paper_interpretation.json" in result.data["tree"]
    assert "notes.md" in result.data["tree"]
    assert "Focused tree:" in result.output
    assert "Important paths:" in result.output


@pytest.mark.asyncio
async def test_project_read_and_write_file_tools_roundtrip(tmp_path, monkeypatch):
    write_tool = agent_tools.ProjectWriteFileTool(db=object(), user_id=1)
    read_tool = agent_tools.ProjectReadFileTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    monkeypatch.setattr(write_tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(write_tool, "_project_dir_for", lambda _project_id: tmp_path)
    monkeypatch.setattr(read_tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(read_tool, "_project_dir_for", lambda _project_id: tmp_path)

    write_result = await write_tool._execute(
        project_id=7,
        relative_path="reference/repo/readme_intake.json",
        content='{"status":"draft"}\n',
    )

    assert write_result.success is True
    assert write_result.data == {
        "project_id": 7,
        "relative_path": "reference/repo/readme_intake.json",
        "written": True,
    }
    assert (tmp_path / "reference" / "repo" / "readme_intake.json").read_text(encoding="utf-8") == '{"status":"draft"}\n'

    read_result = await read_tool._execute(project_id=7, relative_path="reference/repo/readme_intake.json")

    assert read_result.success is True
    assert read_result.data == {
        "project_id": 7,
        "relative_path": "reference/repo/readme_intake.json",
        "content": '{"status":"draft"}\n',
    }
    assert "Content:" in read_result.output
    assert '{"status":"draft"}' in read_result.output


@pytest.mark.asyncio
async def test_project_bash_tool_executes_in_project_root(tmp_path, monkeypatch):
    tool = agent_tools.ProjectBashTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(tool, "_project_dir_for", lambda _project_id: tmp_path)

    result = await tool._execute(
        project_id=7,
        command="pwd && printf 'hello-from-project-bash'",
    )

    assert result.success is True
    assert result.data["project_id"] == 7
    assert result.data["command"] == "pwd && printf 'hello-from-project-bash'"
    assert result.data["exit_code"] == 0
    assert str(tmp_path) in result.data["stdout"]
    assert "hello-from-project-bash" in result.data["stdout"]
    assert result.data["stderr"] == ""
    assert "已执行 Project bash 命令。" in result.output
    assert f"- Project: /projects/7" in result.output
    assert "- Exit code: 0" in result.output


@pytest.mark.asyncio
async def test_project_bash_tool_uses_runtime_worker_when_enabled(tmp_path, monkeypatch):
    tool = agent_tools.ProjectBashTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    class _FakeWorkerClient:
        @staticmethod
        def enabled():
            return True

        async def bash(self, *, project_id: int, workspace_dir, command: str):
            assert project_id == 7
            assert workspace_dir == tmp_path
            assert command == "pwd"
            return {
                "project_id": 7,
                "workspace_dir": str(tmp_path),
                "command": "pwd",
                "exit_code": 0,
                "stdout": "/app/uploads/projects/7\n",
                "stderr": "",
                "success": True,
                "error": None,
                "worker": "runtime-worker",
            }

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(tool, "_project_dir_for", lambda _project_id: tmp_path)
    monkeypatch.setattr("app.services.project_runtime_service.ProjectRuntimeWorkerClient", _FakeWorkerClient)

    result = await tool._execute(project_id=7, command="pwd")

    assert result.success is True
    assert result.data["worker"] == "runtime-worker"
    assert result.data["exit_code"] == 0
    assert result.data["stdout"] == "/app/uploads/projects/7\n"
    assert "已通过 runtime-worker 执行 Project bash 命令。" in result.output


@pytest.mark.asyncio
async def test_project_claude_tool_uses_runtime_worker(tmp_path, monkeypatch):
    tool = agent_tools.ProjectClaudeTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    class _FakeWorkerClient:
        @staticmethod
        def enabled():
            return True

        async def claude(self, *, project_id: int, workspace_dir, prompt: str, continue_session: bool):
            assert project_id == 7
            assert workspace_dir == tmp_path
            assert prompt == "inspect the repo"
            assert continue_session is False
            return {
                "project_id": 7,
                "workspace_dir": str(tmp_path),
                "prompt": prompt,
                "continue_session": continue_session,
                "session_id": "test-session-id",
                "assistant_text": "I will inspect the repo.",
                "result_text": "I will inspect the repo.",
                "is_error": False,
                "exit_code": 0,
                "stdout": '{"type":"result"}',
                "stderr": "",
                "error": None,
                "worker": "runtime-worker",
            }

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(tool, "_project_dir_for", lambda _project_id: tmp_path)
    monkeypatch.setattr("app.services.project_runtime_service.ProjectRuntimeWorkerClient", _FakeWorkerClient)

    result = await tool._execute(project_id=7, prompt="inspect the repo", continue_session=False)

    assert result.success is True
    assert result.data["worker"] == "runtime-worker"
    assert result.data["session_id"] == "test-session-id"
    assert result.data["result_text"] == "I will inspect the repo."
    assert "已通过 runtime-worker 调用 Claude Code。" in result.output
    assert "- Session: test-session-id" in result.output
    assert "Claude result:" in result.output


def test_project_claude_tool_has_no_tool_timeout():
    tool = agent_tools.ProjectClaudeTool(db=object(), user_id=1)

    assert tool._resolve_timeout_seconds() is None


def test_docx_generate_tool_has_no_timeout_or_input_length_caps():
    tool = agent_tools.DocxGenerateWithClaudeTool()
    schema = agent_tools.DocxGenerateWithClaudeInput.model_json_schema()

    assert tool._resolve_timeout_seconds() is None
    assert "maxLength" not in schema["properties"]["markdown"]
    assert "maxLength" not in schema["properties"]["requirements"]
    assert "maxLength" not in schema["properties"]["source_path"]


def test_docx_template_service_saves_constraints_files_and_lists_workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    service = DocxTemplateService()
    workspace = tmp_path / "docx" / "demo-docx"
    workspace.mkdir(parents=True)
    (workspace / "review.docx").write_bytes(b"docx")

    template = service.upsert_template(
        template_id=None,
        name="国自然模板",
        description="项目申请书",
        md_constraints="# MD",
        docx_constraints="# DOCX",
        user_id=3,
    )
    uploaded = service.save_template_file(
        template_id=template["template_id"],
        filename="../sample.docx",
        content=b"sample",
        file_role="sample_template",
    )
    overview = service.list_overview()

    assert template["md_constraints"] == "# MD"
    assert template["docx_constraints"] == "# DOCX"
    assert uploaded["relative_path"].startswith(f"templates/{template['template_id']}/files/")
    assert uploaded["file_role"] == "sample_template"
    assert overview["docx_root"] == str(tmp_path / "docx")
    assert overview["templates"][0]["template_id"] == template["template_id"]
    assert overview["templates"][0]["files"][0]["file_role"] == "sample_template"
    assert overview["workspaces"][0]["docx_id"] == "demo-docx"
    assert service.resolve_download_path("demo-docx/review.docx") == workspace / "review.docx"


@pytest.mark.asyncio
async def test_docx_template_service_analyzes_files_and_generates_constraints(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    source_docx = tmp_path / "sample.docx"
    _write_minimal_docx(source_docx)
    service = DocxTemplateService()
    template = service.upsert_template(
        template_id="grant-template",
        name="国自然模板",
        description="项目申请书",
        user_id=3,
    )
    service.save_template_file(
        template_id=template["template_id"],
        filename="sample.docx",
        content=source_docx.read_bytes(),
        file_role="sample_template",
    )
    service.save_template_file(
        template_id=template["template_id"],
        filename="guide.md",
        content="申请书必须包含立项依据、研究内容、创新点。".encode("utf-8"),
        file_role="writing_guide",
    )

    class _FakeLLMService:
        async def chat(self, **kwargs):
            assert "文件分析结果 JSON" in kwargs["messages"][0]["content"]
            return {
                "content": json.dumps(
                    {
                        "md_constraints": "必须包含立项依据、研究内容、创新点。",
                        "docx_constraints": "一级标题采用标题 1 样式。",
                        "notes": "已分析 DOCX 和撰写说明。",
                    },
                    ensure_ascii=False,
                )
            }

    monkeypatch.setattr("app.services.llm_service.LLMService", lambda: _FakeLLMService())

    analysis = service.build_template_analysis(template["template_id"])
    result = await service.generate_constraints_with_llm(template["template_id"])

    assert any(item.get("kind") == "docx_ooxml" for item in analysis["files"])
    assert "立项依据" in json.dumps(analysis, ensure_ascii=False)
    assert result["md_constraints"] == "必须包含立项依据、研究内容、创新点。"
    assert result["docx_constraints"] == "一级标题采用标题 1 样式。"


def test_tool_registry_registers_docx_tool_and_resolves_generation_intent():
    registry = agent_tools.ToolRegistry(db=object(), user_id=1, initialize_mcp=False)
    user_text = "根据这篇 Markdown 生成 docx 文档"

    assert registry.resolve_intent(user_text) == "document_generation"
    assert "docx_generate_with_claude" in registry._tools


@pytest.mark.asyncio
async def test_literature_search_tool_passes_extended_parameters():
    paper = SimpleNamespace(
        source="openalex",
        external_id="W1",
        title="Graph RAG Survey",
        abstract="abstract",
        authors=[{"name": "Ada"}],
        year=2024,
        venue="TestConf",
        citation_count=7,
        reference_count=3,
        url="https://example.test/paper",
        pdf_url="https://example.test/paper.pdf",
        arxiv_id=None,
        doi="10.1234/example",
        fields_of_study=["Computer Science"],
    )

    class _FakeService:
        def __init__(self):
            self.search_kwargs = None

        async def search(self, **kwargs):
            self.search_kwargs = dict(kwargs)
            return {
                "total": 99,
                "offset": kwargs.get("offset"),
                "has_more": True,
                "next_token": "next-token",
                "resolved_source": "openalex",
                "attempted_sources": ["openalex"],
                "papers": [paper],
            }

    service = _FakeService()
    tool = agent_tools.LiteratureSearchTool()
    tool.service = service

    result = await tool.execute(
        query="graph rag",
        source="auto",
        max_results=25,
        offset=50,
        page_token="cursor",
        year_start=2020,
        year_end=2025,
        fields=["Computer Science"],
        open_access=True,
        sort_by="latest",
        sort_order="desc",
        abstract_max_chars=1200,
    )

    assert result.success is True
    assert service.search_kwargs["source"] == "auto"
    assert service.search_kwargs["limit"] == 25
    assert service.search_kwargs["offset"] == 50
    assert service.search_kwargs["page_token"] == "cursor"
    assert service.search_kwargs["year_range"] == (2020, 2025)
    assert service.search_kwargs["fields_of_study"] == ["Computer Science"]
    assert service.search_kwargs["open_access_only"] is True
    assert service.search_kwargs["sort_by"] == "latest"
    assert result.data["next_token"] == "next-token"
    assert result.data["papers"][0]["pdf_url"] == "https://example.test/paper.pdf"
    assert "PDF: https://example.test/paper.pdf" in result.output
    assert "DOI: 10.1234/example" in result.output


@pytest.mark.asyncio
async def test_literature_review_workspace_tools_generate_review_files(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))

    start_result = await agent_tools.LiteratureReviewStartTool(user_id=1).execute(
        topic="Graph RAG for scientific literature",
        target_paper_count=1,
    )
    review_id = start_result.data["literature_review_id"]
    root = Path(start_result.data["root"])

    class _FakeLiteratureService:
        async def download_pdf(self, pdf_url: str, save_path: str):
            assert pdf_url == "https://example.test/paper.pdf"
            Path(save_path).write_bytes(b"%PDF-1.4\nfake\n")
            return True, ""

    monkeypatch.setattr(
        "app.services.literature_service.get_literature_service",
        lambda: _FakeLiteratureService(),
    )

    download_result = await agent_tools.LiteratureReviewDownloadPdfTool(user_id=1).execute(
        literature_review_id=review_id,
        pdf_url="https://example.test/paper.pdf",
        title="Graph RAG Survey",
        paper_key="graph-rag-survey",
    )

    assert download_result.success is True
    assert (root / "pdf" / "graph-rag-survey.pdf").is_file()

    class _FakePdfIngestService:
        async def ingest_pdf(self, *, file_path: str, document_name: str = "", mode: str = "fast"):
            assert file_path == str(root / "pdf" / "graph-rag-survey.pdf")
            assert mode == "fast"
            return {
                "document_text": "# Graph RAG Survey\n\nFull paper body.",
                "extractor": "fake_pdf2md",
                "report": {"page_count": 2},
                "document_source_spans": [],
            }

    monkeypatch.setattr(
        "app.services.pdf_rag_ingest_service.get_pdf_rag_ingest_service",
        lambda: _FakePdfIngestService(),
    )

    read_result = await agent_tools.ReadFullPdfTool(user_id=1).execute(
        literature_review_id=review_id,
        paper_key="graph-rag-survey",
    )

    assert read_result.success is True
    assert read_result.data["md_path"] == str(root / "md" / "graph-rag-survey.md")
    assert "Full paper body" not in read_result.output
    assert (root / "md" / "graph-rag-survey.md").read_text(encoding="utf-8").endswith("Full paper body.")

    async def _fake_call_llm(self, *, system_prompt: str, user_prompt: str):
        if "完整论文 Markdown" in user_prompt:
            return "# 单篇 Review\n\n- 可纳入最终综述。"
        return "# 最终综述\n\n这是汇总后的综述。"

    monkeypatch.setattr(agent_tools.ReviewWriterTool, "_call_llm", _fake_call_llm)
    writer = agent_tools.ReviewWriterTool(user_id=1)

    paper_review = await writer.execute(
        literature_review_id=review_id,
        topic="Graph RAG for scientific literature",
        mode="paper",
        paper_key="graph-rag-survey",
    )
    final_review = await writer.execute(
        literature_review_id=review_id,
        topic="Graph RAG for scientific literature",
        mode="final",
        target_paper_count=1,
    )

    assert paper_review.success is True
    assert (root / "review" / "graph-rag-survey.md").read_text(encoding="utf-8").startswith("# 单篇 Review")
    assert final_review.success is True
    assert final_review.data["final_review_path"] == str(root / "review" / "final.md")
    assert final_review.output.startswith("# 最终综述")


def test_tool_registry_registers_literature_review_tools():
    registry = agent_tools.ToolRegistry(db=object(), user_id=1, conversation_id=1, initialize_mcp=False)

    assert "literature_review_start" in registry._tools
    assert "literature_review_download_pdf" in registry._tools
    assert "read_full_pdf" in registry._tools
    assert "review_writer" in registry._tools


def test_literature_review_skill_injects_session_prompt():
    from app.services.agent_skill_service import AgentSkillService

    service = AgentSkillService(Path(os.getcwd()) / ".agents" / "skills")
    resolution = service.resolve(
        "继续文献综述",
        channel="chat",
        active_skill_names=["literature-review"],
    )

    assert resolution.active_skills[0].name == "literature-review"
    assert "默认目标是 12 篇" in resolution.active_system_prompt
    assert "read_full_pdf" in resolution.active_system_prompt


def test_tool_output_truncation_defaults_enabled_for_long_outputs():
    from app.config import settings

    class _EchoTool(agent_tools.ToolBase):
        name = "echo_for_truncation_test"
        parameters = {"type": "object", "properties": {}}

        async def _execute(self, **kwargs):
            return agent_tools.ToolResult(success=True, output="x " * 5000)

    assert settings.tool_output_truncation_enabled is True
    assert agent_tools.ReadFullPdfTool(user_id=1)._resolve_output_max_tokens() == 9000
    assert agent_tools.ReviewWriterTool(user_id=1)._resolve_output_max_tokens() == 12000

    result = _EchoTool()._with_finalized_result(
        agent_tools.ToolResult(success=True, output="x " * 5000),
        started_at=0.0,
        retry_attempt=1,
    )

    assert result.truncated is True
    assert result.data["output_truncated"] is True
    assert "[TRUNCATED]" in result.output


@pytest.mark.asyncio
async def test_docx_generate_tool_writes_inputs_and_uses_docx_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    tool = agent_tools.DocxGenerateWithClaudeTool()

    class _FakeDocxRuntimeWorkerClient:
        @staticmethod
        def enabled():
            return True

        async def claude(self, *, docx_id: str, workspace_dir, prompt: str, continue_session: bool):
            assert docx_id == "test-doc"
            assert workspace_dir == tmp_path / "docx" / "test-doc"
            assert "source.md" in prompt
            assert "requirements.md" in prompt
            assert "Project/论文复现" in prompt
            assert continue_session is False
            (workspace_dir / "review.docx").write_bytes(b"fake-docx")
            (workspace_dir / "review.pdf").write_bytes(b"fake-pdf")
            return {
                "docx_id": docx_id,
                "workspace_dir": str(workspace_dir),
                "prompt": prompt,
                "continue_session": continue_session,
                "session_id": "docx-session-id",
                "assistant_text": "created",
                "result_text": "All validations PASSED!",
                "is_error": False,
                "exit_code": 0,
                "stdout": '{"type":"result"}',
                "stderr": "",
                "error": None,
                "worker": "runtime-worker",
            }

    monkeypatch.setattr(
        "app.services.docx_runtime_service.DocxRuntimeWorkerClient",
        _FakeDocxRuntimeWorkerClient,
    )

    result = await tool._execute(
        docx_id="test doc",
        markdown="# Title\n\nBody",
        requirements="生成论文综述模板。",
        output_basename="review",
        continue_session=False,
    )

    workspace_dir = tmp_path / "docx" / "test-doc"
    assert (workspace_dir / "source.md").read_text(encoding="utf-8") == "# Title\n\nBody"
    assert (workspace_dir / "requirements.md").read_text(encoding="utf-8") == "生成论文综述模板。"
    assert result.success is True
    assert result.data["docx_id"] == "test-doc"
    assert result.data["docx_path"] == str(workspace_dir / "review.docx")
    assert result.data["pdf_path"] == str(workspace_dir / "review.pdf")
    assert result.data["validation_status"] == "passed"
    assert "已通过 runtime-worker 调用 Claude Code 生成 DOCX。" in result.output


@pytest.mark.asyncio
async def test_docx_generate_tool_applies_docx_template_constraints(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    template = DocxTemplateService().upsert_template(
        template_id="grant-template",
        name="Grant Template",
        md_constraints="必须输出固定 Markdown 章节。",
        docx_constraints="一级标题使用三号黑体。",
        user_id=1,
    )
    DocxTemplateService().save_template_file(
        template_id=template["template_id"],
        filename="reference.docx",
        content=b"template",
    )
    tool = agent_tools.DocxGenerateWithClaudeTool()

    class _FakeDocxRuntimeWorkerClient:
        @staticmethod
        def enabled():
            return True

        async def claude(self, *, docx_id: str, workspace_dir, prompt: str, continue_session: bool):
            assert docx_id == "templated-doc"
            assert (workspace_dir / "template_files" / "reference.docx").is_file()
            assert "一级标题使用三号黑体。" in (workspace_dir / "requirements.md").read_text(encoding="utf-8")
            assert "必须输出固定 Markdown 章节。" in (workspace_dir / "template_md_constraints.md").read_text(encoding="utf-8")
            (workspace_dir / "generated_document.docx").write_bytes(b"fake-docx")
            return {
                "docx_id": docx_id,
                "workspace_dir": str(workspace_dir),
                "prompt": prompt,
                "continue_session": continue_session,
                "session_id": "docx-session-id",
                "assistant_text": "created",
                "result_text": "All validations PASSED!",
                "is_error": False,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "error": None,
                "worker": "runtime-worker",
            }

    monkeypatch.setattr(
        "app.services.docx_runtime_service.DocxRuntimeWorkerClient",
        _FakeDocxRuntimeWorkerClient,
    )

    result = await tool._execute(
        docx_id="templated doc",
        template_id=template["template_id"],
        markdown="# Title",
        requirements="生成项目申请书。",
    )

    assert result.success is True
    assert result.data["template_id"] == template["template_id"]
    assert result.data["template_files"]
    assert result.data["md_constraints_path"].endswith("template_md_constraints.md")


@pytest.mark.asyncio
async def test_probe_url_requires_explicit_resolve_download_gate_for_google_drive(monkeypatch):
    tool = agent_tools.PaperResearchProbeUrlTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    class _Response:
        def __init__(self, *, status_code, url, headers, text=""):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self.text = text

    class _StreamResponse:
        def __init__(self, *, status_code, url, headers, body: bytes):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self):
            yield self._body

    class _Client:
        def __init__(self, *args, **kwargs):
            self.cookies = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def head(self, url):
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "512"},
            )

        def stream(self, method, url, headers=None):
            del method, headers
            return _StreamResponse(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "512"},
                body=b"<!DOCTYPE html><html><title>Google Drive - Virus scan warning</title><body>download</body></html>",
            )

        async def get(self, url):
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "512"},
                text='<!DOCTYPE html><html><head><title>Google Drive - Virus scan warning</title></head><body><form id="download-form"></form></body></html>',
            )

    async def _fake_page_semantics(*args, **kwargs):
        return {
            "title": "Google Drive - Virus scan warning",
            "page_kind": "download_gate",
            "signals": ["google_drive", "virus_scan_warning", "download_form"],
            "text_excerpt": "Google Drive can't scan this file for viruses.",
            "classification_source": "heuristic",
            "rationale": "download-form present",
            "diagnosis": "download_gate",
            "suggested_next_action": "retry_with_resolve_download_gate",
        }

    async def _fake_confirm_download(*, client, url, read_bytes):
        del client, read_bytes
        return {
            "status_code": 206,
            "final_url": f"{url}&confirm=t",
            "content_type": "application/octet-stream",
            "content_length": 256,
            "head_bytes": b"\x1f\x8b\x08\x00",
            "confirm_url": f"{url}&confirm=t",
            "confirm_token_present": True,
        }

    async def _fake_resolver(*args, **kwargs):
        return {
            "resolution": "blocked",
            "diagnosis": "download_gate",
            "suggested_next_action": "retry_with_resolve_download_gate",
            "reason": "Google Drive 门页，需要显式打开 resolve_download_gate。",
            "confidence": 0.99,
            "page_semantics": kwargs.get("semantics") or {},
        }

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(agent_tools, "analyze_html_page_semantics", _fake_page_semantics)
    monkeypatch.setattr(agent_tools, "probe_google_drive_confirm_download", _fake_confirm_download)
    monkeypatch.setattr(agent_tools, "resolve_html_probe_plan_with_llm", _fake_resolver)

    base_kwargs = {
        "project_id": 7,
        "url": "https://drive.google.com/uc?export=download&id=0Bz8a_Dbh9QhbUkVqNEszd0pHaFE",
        "expected_kind": "file",
    }
    without_resolve = await tool._execute(**base_kwargs)
    with_resolve = await tool._execute(**{**base_kwargs, "resolve_download_gate": True})

    assert without_resolve.success is False
    assert without_resolve.data["diagnosis"] in {"download_gate", "gdrive_confirm_required"}
    assert without_resolve.data["suggested_next_action"] == "retry_with_resolve_download_gate"
    assert without_resolve.data["resolve_download_gate"] is False
    assert without_resolve.data["reachable"] is True
    assert without_resolve.data["usable"] is False
    assert "Google Drive" in without_resolve.data["page_title"]
    assert without_resolve.data["page_text_excerpt"]

    assert with_resolve.success is True
    assert with_resolve.data["status_code"] == 206
    assert with_resolve.data["content_type"] == "application/octet-stream"
    assert with_resolve.data["diagnosis"] == "valid_gzip"
    assert with_resolve.data["resolve_download_gate"] is True
    assert with_resolve.data["reachable"] is True
    assert with_resolve.data["usable"] is True


@pytest.mark.asyncio
async def test_probe_url_accepts_reference_page_after_html_resolution(monkeypatch):
    tool = agent_tools.PaperResearchProbeUrlTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    class _Response:
        def __init__(self, *, status_code, url, headers, text=""):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self.text = text

    class _StreamResponse:
        def __init__(self, *, status_code, url, headers, body: bytes):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self):
            yield self._body

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def head(self, url):
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "2048"},
            )

        def stream(self, method, url, headers=None):
            del method, headers
            return _StreamResponse(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "2048"},
                body=b"<!DOCTYPE html><html><title>Datasets \xc2\xb7 fastText</title><body>download</body></html>",
            )

        async def get(self, url):
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "2048"},
                text=(
                    "<!DOCTYPE html><html><head><title>Datasets · fastText</title></head>"
                    "<body><h1>Datasets</h1><a href=\"/docs/en/crawl-vectors.html\">Download YFCC100M Dataset</a></body></html>"
                ),
            )

    async def _fake_page_semantics(*args, **kwargs):
        return {
            "title": "Datasets · fastText",
            "page_kind": "reference_page",
            "signals": ["documentation"],
            "text_excerpt": "Datasets Download YFCC100M Dataset",
            "classification_source": "heuristic",
            "rationale": "文档页，列出了可进一步探索的数据集条目。",
            "diagnosis": "html_page",
            "suggested_next_action": "use_as_reference_page",
            "links": [{"text": "Download YFCC100M Dataset", "href": "/docs/en/crawl-vectors.html"}],
            "forms": [],
        }

    async def _fake_resolver(*args, **kwargs):
        return {
            "resolution": "reference_page_ok",
            "diagnosis": "reference_page_ok",
            "suggested_next_action": "use_as_reference_page",
            "reason": "这是官方文档索引页，可作为后续下载探索的参考页。",
            "confidence": 0.93,
            "page_semantics": kwargs.get("semantics") or {},
        }

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(agent_tools, "analyze_html_page_semantics", _fake_page_semantics)
    monkeypatch.setattr(agent_tools, "resolve_html_probe_plan_with_llm", _fake_resolver)

    result = await tool._execute(
        project_id=7,
        url="https://fasttext.cc/docs/en/dataset.html",
        expected_kind="auto",
    )

    assert result.success is True
    assert result.data["ok"] is True
    assert result.data["usable"] is True
    assert result.data["diagnosis"] == "reference_page_ok"
    assert result.data["resolution_status"] == "reference_page_ok"
    assert result.data["resolved_target_kind"] == "html"
    assert result.data["downloadable"] is False


@pytest.mark.asyncio
async def test_probe_url_can_follow_html_page_link_until_file_resolves(monkeypatch):
    tool = agent_tools.PaperResearchProbeUrlTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    docs_url = "https://fasttext.cc/docs/en/dataset.html"
    file_url = "https://fasttext.cc/data/wiki-news-300d-1M.vec.zip"

    class _Response:
        def __init__(self, *, status_code, url, headers, text=""):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self.text = text

    class _StreamResponse:
        def __init__(self, *, status_code, url, headers, body: bytes):
            self.status_code = status_code
            self.url = url
            self.headers = headers
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self):
            yield self._body

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def head(self, url):
            if url == docs_url:
                return _Response(
                    status_code=200,
                    url=url,
                    headers={"content-type": "text/html; charset=utf-8", "content-length": "2048"},
                )
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "application/zip", "content-length": "4096"},
            )

        def stream(self, method, url, headers=None):
            del method, headers
            if url == docs_url:
                return _StreamResponse(
                    status_code=200,
                    url=url,
                    headers={"content-type": "text/html; charset=utf-8", "content-length": "2048"},
                    body=b"<!DOCTYPE html><html><title>Datasets \xc2\xb7 fastText</title><body>download</body></html>",
                )
            return _StreamResponse(
                status_code=206,
                url=url,
                headers={"content-type": "application/zip", "content-length": "4096"},
                body=b"PK\x03\x04zip-data",
            )

        async def get(self, url):
            assert url == docs_url
            return _Response(
                status_code=200,
                url=url,
                headers={"content-type": "text/html; charset=utf-8", "content-length": "2048"},
                text=(
                    "<!DOCTYPE html><html><head><title>Datasets · fastText</title></head>"
                    f"<body><a href=\"{file_url}\">Download vectors</a></body></html>"
                ),
            )

    async def _fake_page_semantics(*args, **kwargs):
        return {
            "title": "Datasets · fastText",
            "page_kind": "reference_page",
            "signals": ["documentation"],
            "text_excerpt": "Download vectors",
            "classification_source": "heuristic",
            "rationale": "文档页，包含明确下载链接。",
            "diagnosis": "html_page",
            "suggested_next_action": "follow_selected_link",
            "links": [{"text": "Download vectors", "href": file_url}],
            "forms": [],
        }

    async def _fake_resolver(*args, **kwargs):
        return {
            "resolution": "follow_link",
            "selected_href": file_url,
            "selected_absolute_url": file_url,
            "diagnosis": "follow_candidate_found",
            "suggested_next_action": "follow_selected_link",
            "reason": "页面给出了明确的压缩包下载链接。",
            "confidence": 0.96,
            "page_semantics": kwargs.get("semantics") or {},
        }

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(agent_tools.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(agent_tools, "analyze_html_page_semantics", _fake_page_semantics)
    monkeypatch.setattr(agent_tools, "resolve_html_probe_plan_with_llm", _fake_resolver)

    result = await tool._execute(project_id=7, url=docs_url, expected_kind="auto")

    assert result.success is True
    assert result.data["ok"] is True
    assert result.data["diagnosis"] == "followed_link_ok"
    assert result.data["resolution_status"] == "followed_link_ok"
    assert result.data["resolved_target_url"] == file_url
    assert result.data["resolved_target_kind"] == "zip"
    assert result.data["resolved_downloadable"] is True


@pytest.mark.asyncio
async def test_paper_research_git_tools_surface_repo_state(tmp_path, monkeypatch):
    repo_dir = tmp_path / "paper_repo"
    repo_dir.mkdir(parents=True)
    _init_git_repo(repo_dir)

    tracked_file = repo_dir / "train.py"
    tracked_file.write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "train.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    tracked_file.write_text("print('v2')\n", encoding="utf-8")
    (repo_dir / "notes.txt").write_text("todo\n", encoding="utf-8")

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    status_tool = agent_tools.PaperResearchGitStatusTool(db=object(), user_id=1)
    diff_tool = agent_tools.PaperResearchGitDiffTool(db=object(), user_id=1)
    log_tool = agent_tools.PaperResearchGitLogTool(db=object(), user_id=1)
    show_tool = agent_tools.PaperResearchGitShowTool(db=object(), user_id=1)
    for tool in [status_tool, diff_tool, log_tool, show_tool]:
        monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
        monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    status_result = await status_tool._execute(project_id=7)
    diff_result = await diff_tool._execute(project_id=7, repo_relative_paths=["train.py"])
    log_result = await log_tool._execute(project_id=7, max_count=5)
    show_result = await show_tool._execute(project_id=7, ref="HEAD", repo_relative_path="train.py")

    assert status_result.success is True
    assert status_result.data["clean"] is False
    assert any("train.py" in item for item in list(status_result.data.get("entries") or []))
    assert any("notes.txt" in item for item in list(status_result.data.get("entries") or []))

    assert diff_result.success is True
    assert "print('v2')" in diff_result.data["diff"]
    assert diff_result.data["repo_relative_paths"] == ["train.py"]

    assert log_result.success is True
    assert log_result.data["commits"][0]["subject"] == "initial commit"

    assert show_result.success is True
    assert "print('v1')" in show_result.data["content"]
    assert show_result.data["repo_relative_path"] == "train.py"


@pytest.mark.asyncio
async def test_assess_repo_mainpath_prefers_readme_commands_and_entrypoint_hints(tmp_path, monkeypatch):
    tool = agent_tools.PaperResearchAssessRepoMainpathTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    repo_dir = tmp_path / "paper_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "classification-results.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo_dir / "train.py").write_text("print('train')\n", encoding="utf-8")
    (repo_dir / "alignment").mkdir(parents=True, exist_ok=True)
    (repo_dir / "alignment" / "example.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "repo_readme_excerpt.md").write_text(
        "Run the main reproduction with:\n```bash\nbash classification-results.sh\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "repo_reference.json").write_text(
        json.dumps({"repo_url": "https://github.com/facebookresearch/fastText.git"}),
        encoding="utf-8",
    )
    (tmp_path / "repo_file_index.json").write_text(
        json.dumps(
            {
                "readme_excerpt_file": "repo_readme_excerpt.md",
                "files": ["classification-results.sh", "train.py", "alignment/example.sh", "README.md"],
                "entrypoint_candidates": [{"path": "train.py"}, {"path": "classification-results.sh"}, {"path": "alignment/example.sh"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "experiment_spec.json").write_text(
        json.dumps(
            {
                "entrypoint_hints": [
                    {"value": "classification-results.sh"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = await tool._execute(project_id=7)

    assert result.success is True
    assert result.data["status"] == "identified"
    assert result.data["selected_main_path"]["path"] == "classification-results.sh"
    assert "classification-results.sh" in result.data["selected_main_path_reason"]
    assert result.data["top_candidates"][0]["path"] == "classification-results.sh"
    assert result.data["top_candidates"][0]["evidence_excerpts"]
    assert any(item["path"] == "alignment/example.sh" and item.get("why_not_selected") for item in result.data["top_candidates"])


@pytest.mark.asyncio
async def test_assess_repo_mainpath_uses_readme_reproduction_intake_when_available(tmp_path, monkeypatch):
    tool = agent_tools.PaperResearchAssessRepoMainpathTool(db=object(), user_id=1)

    async def _resolve_project_workspace(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload(), _workspace()

    monkeypatch.setattr(tool, "_resolve_project_workspace", _resolve_project_workspace)
    monkeypatch.setattr(tool, "_workspace_dir_for", lambda _workspace_obj: tmp_path)

    repo_dir = tmp_path / "paper_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "classification-results.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo_dir / "train.py").write_text("print('train')\n", encoding="utf-8")
    (tmp_path / "repo_reference.json").write_text(
        json.dumps({"repo_url": "https://github.com/facebookresearch/fastText.git"}),
        encoding="utf-8",
    )
    (tmp_path / "repo_file_index.json").write_text(
        json.dumps(
            {
                "readme_reproduction_intake_file": "repo_readme_reproduction_intake.json",
                "files": ["classification-results.sh", "train.py", "README.md"],
                "entrypoint_candidates": [{"path": "train.py"}, {"path": "classification-results.sh"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "repo_readme_reproduction_intake.json").write_text(
        json.dumps(
            {
                "run_commands": [
                    {
                        "command": "bash classification-results.sh",
                        "entrypoint_path_or_hint": "classification-results.sh",
                        "evidence_text": "bash classification-results.sh",
                    }
                ],
                "entrypoints": [
                    {
                        "path_or_hint": "classification-results.sh",
                        "kind": "script",
                        "evidence_text": "classification-results.sh",
                    }
                ],
                "focus_files": ["classification-results.sh"],
                "evidence_snippets": [{"topic": "run", "text": "bash classification-results.sh"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "experiment_spec.json").write_text(json.dumps({"entrypoint_hints": []}), encoding="utf-8")

    result = await tool._execute(project_id=7)

    assert result.success is True
    assert result.data["selected_main_path"]["path"] == "classification-results.sh"
    assert result.data["readme_main_commands"][0] == "bash classification-results.sh"


@pytest.mark.asyncio
async def test_search_project_zoekt_tool_returns_project_relative_matches(tmp_path, monkeypatch):
    tool = agent_tools.PaperResearchSearchProjectZoektTool(db=object(), user_id=1)

    async def _resolve_project_payload_only(_db, *, project_id: int):
        assert project_id == 7
        return _project_payload()

    reference_dir = tmp_path / "reference" / "paper"
    reference_dir.mkdir(parents=True, exist_ok=True)
    (reference_dir / "paper_interpretation.md").write_text(
        json.dumps(
            {
                "status": "grounded",
                "summary": "classification-results entrypoint confirmed",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _fake_build_project_index(*, project_dir, workspace_dir, force_reindex=False):
        assert project_dir == tmp_path
        assert workspace_dir == tmp_path
        return {
            "success": True,
            "available": True,
            "status": "created",
            "index_dir": str(tmp_path / ".zoekt_project" / "index"),
            "search_binary": "/usr/local/bin/zoekt",
            "git_index_binary": "/usr/local/bin/zoekt-git-index",
            "plain_index_binary": "/usr/local/bin/zoekt-index",
        }

    async def _fake_search_project(*, workspace_dir, query, max_results):
        assert workspace_dir == tmp_path
        assert query == "classification-results"
        assert max_results == 5
        return {
            "success": True,
            "available": True,
            "engine": "zoekt",
            "query": query,
            "index_dir": str(tmp_path / ".zoekt_project" / "index"),
            "manifest_path": str(tmp_path / ".zoekt_project" / "manifest.json"),
            "matched_files": ["reference/paper/paper_interpretation.md"],
            "matched_relative_paths": ["reference/paper/paper_interpretation.md"],
            "truncated": False,
            "matches": [
                {
                    "source_relative_path": "reference/paper/paper_interpretation.md",
                    "relative_path": "reference/paper/paper_interpretation.md",
                    "line_number": 3,
                    "line_text": '  "summary": "classification-results entrypoint confirmed"',
                    "line_fragments": [{"line_offset": 14, "match_length": 22, "text": "classification-results"}],
                    "match_source": "content",
                    "score": 8.0,
                }
            ],
        }

    monkeypatch.setattr(tool, "_resolve_project_payload_only", _resolve_project_payload_only)
    monkeypatch.setattr(tool, "_project_dir_for", lambda _project_id: tmp_path)
    monkeypatch.setattr(agent_tools.ZoektCliService, "build_project_index", _fake_build_project_index)
    monkeypatch.setattr(agent_tools.ZoektCliService, "search_project", _fake_search_project)

    result = await tool._execute(project_id=7, query="classification-results", max_results=5, context_lines=1)

    assert result.success is True
    assert result.data["engine"] == "zoekt"
    assert result.data["matches"][0]["project_relative_path"] == "reference/paper/paper_interpretation.md"
    assert "已使用 Zoekt 搜索 Project 根目录" in result.output
    assert "context 2-4" in result.output
    assert "classification-results entrypoint confirmed" in result.output


@pytest.mark.asyncio
async def test_prepare_tool_runs_project_reference_builder(monkeypatch):
    tool = agent_tools.PaperResearchPrepareTool(db=object(), user_id=1)
    paper = SimpleNamespace(
        id=113,
        title="Example Paper",
        year=2024,
        venue="ICML",
        arxiv_id="2401.00001",
    )

    async def _resolve_paper(_db, *, paper_id=None, paper_title=None):
        assert paper_id == 113
        assert paper_title is None
        return paper

    async def _resolve_project_payload(_service, *, paper, project_id, project_title=None, user_goal=None, create_project=False):
        assert paper.id == 113
        assert project_id is None
        assert create_project is True
        return {
            "id": 7,
            "title": "Example Project",
            "status": "draft",
            "goal": None,
            "paper_count": 1,
            "workspace_count": 0,
        }

    async def _fake_build(self, *, paper, project_id, user_id, refresh=False):
        assert paper.id == 113
        assert project_id == 7
        assert user_id == 1
        assert refresh is False
        return {
            "project_id": 7,
            "project_root": "/app/uploads/projects/7",
            "reference_root": "/app/uploads/projects/7/reference",
            "reference_ready": True,
            "reference_files": [
                "reference/paper/paper_pdf2md.md",
                "reference/paper/paper_interpretation.md",
                "reference/paper/paper_interpretation.json",
                "reference/repo/readme_intake.json",
            ],
            "repo_reference": {
                "repo_materialization": {
                    "status": "reused",
                    "repo_source_dir": "/app/uploads/projects/7/repo/source",
                }
            },
        }

    monkeypatch.setattr(tool, "_resolve_paper", _resolve_paper)
    monkeypatch.setattr(tool, "_resolve_project_payload", _resolve_project_payload)
    monkeypatch.setattr(project_reference_builder_service.ProjectReferenceBuilderService, "build", _fake_build)

    result = await tool._execute(paper_id=113, create_project=True, refresh_intake=False)

    assert result.success is True
    assert result.data["project"]["id"] == 7
    assert result.data["reference_builder"]["reference_ready"] is True
    assert "Project reference builder 完成" in result.output
    assert "reference/paper/paper_pdf2md.md" in result.output
    assert "Repo status: reused" in result.output


@pytest.mark.asyncio
async def test_status_tool_resolves_project_from_paper_and_lists_reference_files(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))

    tool = agent_tools.PaperResearchStatusTool(db=object(), user_id=1)
    paper = SimpleNamespace(
        id=113,
        title="Example Paper",
        year=2024,
        venue="ICML",
        arxiv_id="2401.00001",
    )

    project_root = tmp_path / "uploads" / "projects" / "8"
    for relative_path in (
        "reference/paper/paper_pdf2md.md",
        "reference/paper/paper_interpretation.md",
        "reference/repo/readme_intake.json",
    ):
        path = project_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")

    async def _resolve_paper(_db, *, paper_id=None, paper_title=None):
        assert paper_id == 113
        assert paper_title is None
        return paper

    async def _resolve_project_payload(_service, *, paper, project_id, create_project=False):
        assert paper.id == 113
        assert project_id is None
        assert create_project is False
        return {
            "id": 8,
            "title": "Example Project",
            "status": "draft",
            "goal": None,
            "paper_count": 1,
            "workspace_count": 0,
            "primary_paper": {
                "id": 113,
                "title": "Example Paper",
            },
        }

    monkeypatch.setattr(tool, "_resolve_paper", _resolve_paper)
    monkeypatch.setattr(tool, "_resolve_project_payload", _resolve_project_payload)

    result = await tool._execute(paper_id=113)

    assert result.success is True
    assert result.data["project"]["id"] == 8
    assert result.data["reference_ready"] is False
    assert result.data["reference_files"] == [
        "reference/paper/paper_pdf2md.md",
        "reference/paper/paper_interpretation.md",
        "reference/repo/readme_intake.json",
    ]
    assert "paper_id=113" in result.output
    assert "/projects/8" in result.output
