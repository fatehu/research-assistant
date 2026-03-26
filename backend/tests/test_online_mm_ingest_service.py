from __future__ import annotations

import asyncio

import app.services.online_mm_ingest_service as online_mm_module
from PIL import Image, ImageDraw

from app.services.online_mm_ingest_service import OnlineMmIngestService


def test_online_mm_ingest_service_materializes_model_chunks(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    page_images = [
        tmp_path / "page_0001.png",
        tmp_path / "page_0002.png",
        tmp_path / "page_0003.png",
    ]
    for page_image in page_images:
        page_image.write_bytes(b"fake-image")

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(online_mm_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(
        online_mm_module.settings,
        "aliyun_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_pages_per_call", 2, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_extract_max_concurrency", 2, raising=False)
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "is_available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(service, "_count_pages", lambda _pdf_path: 3)
    monkeypatch.setattr(service, "_render_pdf_pages", lambda **_kwargs: list(page_images))

    chat_calls: list[list[str]] = []

    async def _fake_chat_json(
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths,
        max_tokens: int,
        temperature: float = 0.2,
    ):
        del api_key, base_url, model, system_prompt, max_tokens, temperature
        window = [str(item) for item in list(image_paths or [])]
        chat_calls.append(window)
        assert "Granularity: fine" in user_prompt
        assert "final semantic chunks" in user_prompt
        assert "document_metadata" in user_prompt
        assert "Do not output visual blocks" in user_prompt
        assert "clean_text" not in user_prompt
        assert "cleanup_reason" not in user_prompt
        assert "confidence" not in user_prompt
        assert "table_header_state" in user_prompt
        assert "list_continues_previous" in user_prompt
        assert "front_matter_items" in user_prompt
        assert "main sections like I/II/III or 1/2/3 use level 1" in user_prompt
        assert "Common scientific paper structure is usually" in user_prompt
        assert "Treat Acknowledgements as an end-matter section" in user_prompt
        assert "include the inherited parent section_path" in user_prompt
        if len(chat_calls) == 1:
            assert len(window) == 2
            return {
                "parsed": {
                    "pages": [
                        {
                            "page": 1,
                            "document_metadata": {
                                "title": "Attention Is All You Need",
                                "authors": ["Ashish Vaswani", "Noam Shazeer"],
                                "affiliations": ["Google Brain"],
                                "emails": ["attention@google.com"],
                                "identifiers": ["arXiv:1706.03762"],
                                "front_matter_items": ["Index Terms—Transformers"],
                            },
                            "chunks": [
                                {
                                    "chunk_type": "paragraph",
                                    "order": 1,
                                    "text": "Abstract—Transformers improve sequence modelling.",
                                    "content_role": "abstract_body",
                                    "cleanup_action": "keep",
                                    "section_path": ["Abstract"],
                                    "section_level_path": [1],
                                    "zone": "full_width",
                                    "span": "double_column",
                                    "title_hint": "Abstract",
                                }
                            ],
                        },
                        {
                            "page": 2,
                            "document_metadata": {},
                            "chunks": [
                                {
                                    "chunk_type": "paragraph",
                                    "order": 1,
                                    "text": "Transformers improve sequence modelling in translation tasks.",
                                    "content_role": "body_paragraph",
                                    "cleanup_action": "keep",
                                    "section_path": ["1 Introduction"],
                                    "section_level_path": [1],
                                    "zone": "left_column",
                                    "span": "single_column",
                                }
                            ],
                        },
                    ]
                },
                "usage": {"prompt_tokens": 18, "completion_tokens": 7, "total_tokens": 25},
                "model": "qwen3-vl-flash",
                "raw_text": "{}",
            }

        assert len(window) == 1
        return {
            "parsed": {
                "document_metadata": {},
                "chunks": [
                    {
                        "chunk_type": "equation",
                        "order": 1,
                        "text": "Attention(Q,K,V)=softmax(QK^T/sqrt(d_k))V",
                        "latex": "Attention(Q,K,V)=softmax(QK^T/\\sqrt{d_k})V",
                        "content_role": "equation_body",
                        "cleanup_action": "keep",
                        "section_path": ["1 Introduction", "1.1 Attention"],
                        "section_level_path": [1, 2],
                        "zone": "left_column",
                        "span": "single_column",
                        "continues_from_previous_page": False,
                    }
                ],
            },
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
            "model": "qwen3-vl-flash",
            "raw_text": "{}",
        }

    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "chat_json",
        staticmethod(_fake_chat_json),
    )

    result = asyncio.run(
        service.ingest_pdf(
            file_path=str(pdf_path),
            document_name="paper.pdf",
            extract_profile="academic_formula",
            extract_granularity="fine",
        )
    )

    assert result["applied"] is True
    assert chat_calls == [
        [str(page_images[0]), str(page_images[1])],
        [str(page_images[2])],
    ]
    assert result["report"]["page_count"] == 3
    assert result["report"]["extract_granularity"] == "fine"
    assert result["report"]["planner_model"] == "model_chunker"
    assert result["report"]["image_preprocess"]["trim_whitespace"] is True
    assert result["report"]["chunk_count"] == 3
    assert result["report"]["context_chunk_count"] == 4
    assert result["report"]["document_title"] == "Attention Is All You Need"
    assert result["report"]["document_metadata"] == {
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer"],
        "affiliations": ["Google Brain"],
        "emails": ["attention@google.com"],
        "identifiers": ["arXiv:1706.03762"],
        "front_matter_items": ["Index Terms—Transformers"],
    }
    assert [row["section_title"] for row in result["report"]["section_spine"]] == [
        "Abstract",
        "1 Introduction",
        "1.1 Attention",
    ]
    assert result["report"]["section_spine"][1]["path_levels"] == [1]
    assert result["report"]["section_spine"][2]["path_levels"] == [1, 2]
    assert result["report"]["usage"]["total_tokens"] == 38

    assert [block["type"] for block in result["blocks"]] == ["paragraph", "paragraph", "equation"]
    assert [block["content_role"] for block in result["blocks"]] == ["abstract_body", "body_paragraph", "equation_body"]

    abstract_chunk = result["chunks"][0]
    intro_chunk = result["chunks"][1]
    equation_chunk = result["chunks"][2]
    assert abstract_chunk["metadata"]["parent_id"] == "sec_p0001_b0001_abstract"
    assert abstract_chunk["metadata"]["section_title"] == "Abstract"
    assert intro_chunk["metadata"]["parent_id"] == "sec_p0002_b0001_1"
    assert intro_chunk["metadata"]["section_title"] == "1 Introduction"
    assert intro_chunk["metadata"]["extra"]["context_path_titles"] == ["Attention Is All You Need", "1 Introduction"]
    assert intro_chunk["metadata"]["extra"]["context_path_levels"] == [0, 1]
    assert equation_chunk["metadata"]["parent_id"] == "sec_p0003_b0001_2"
    assert equation_chunk["metadata"]["extra"]["context_path_titles"] == [
        "Attention Is All You Need",
        "1 Introduction",
        "1.1 Attention",
    ]
    assert equation_chunk["metadata"]["extra"]["context_path_levels"] == [0, 1, 2]
    assert "$$" in equation_chunk["content"]

    assert result["context_chunks"][0]["metadata"]["section_type"] == "document"
    assert [ctx["metadata"]["section_title"] for ctx in result["context_chunks"]] == [
        "Attention Is All You Need",
        "Abstract",
        "1 Introduction",
        "1.1 Attention",
    ]


def test_online_mm_model_chunk_plan_honors_cleanup_and_cross_page_merge():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0001_b0001",
            "type": "paragraph",
            "page": 1,
            "order": 1,
            "text": "Author metadata",
            "content_role": "front_matter_misc",
            "cleanup_action": "route_to_metadata",
            "section_path_titles": [],
            "section_path_levels": [],
        },
        {
            "block_id": "p0001_b0002",
            "type": "paragraph",
            "page": 1,
            "order": 2,
            "text": "Paragraph part one.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "chunk_hint": "intro_para",
            "section_path_titles": ["1 Introduction"],
            "section_path_levels": [1],
            "continues_from_previous_page": False,
        },
        {
            "block_id": "p0002_b0001",
            "type": "paragraph",
            "page": 2,
            "order": 1,
            "text": "Paragraph part two.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "chunk_hint": "intro_para_cont",
            "section_path_titles": ["1 Introduction"],
            "section_path_levels": [1],
            "continues_from_previous_page": True,
        },
        {
            "block_id": "p0002_b0002",
            "type": "table",
            "page": 2,
            "order": 2,
            "text": "| model | bleu |\n| --- | --- |\n| transformer | 28.4 |",
            "table_markdown": "| model | bleu |\n| --- | --- |\n| transformer | 28.4 |",
            "content_role": "table_body",
            "cleanup_action": "keep",
            "chunk_hint": "results_table",
            "section_path_titles": ["2 Results"],
            "section_path_levels": [1],
            "continues_from_previous_page": False,
        },
    ]

    chunk_plan_result = service._build_model_chunk_plan(
        blocks=blocks,
        document_name="paper.pdf",
        extract_profile="general",
        extract_granularity="medium",
    )

    assert chunk_plan_result["ok"] is True
    assert len(chunk_plan_result["chunks"]) == 2
    assert chunk_plan_result["chunks"][0]["chunk_type"] == "paragraph"
    assert chunk_plan_result["chunks"][0]["block_ids"] == ["p0001_b0002", "p0002_b0001"]
    assert chunk_plan_result["chunks"][1]["chunk_type"] == "table"
    assert chunk_plan_result["chunks"][1]["block_ids"] == ["p0002_b0002"]


def test_online_mm_model_chunk_plan_merges_table_and_list_with_optional_hints():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0003_b0001",
            "type": "table",
            "page": 3,
            "order": 1,
            "text": "Table 2",
            "table_markdown": "| model | score |\n| --- | --- |\n| alpha | 91 |",
            "content_role": "table_body",
            "cleanup_action": "keep",
            "section_path_titles": ["4 Results"],
            "section_path_levels": [1],
            "continues_from_previous_page": False,
        },
        {
            "block_id": "p0004_b0001",
            "type": "table",
            "page": 4,
            "order": 1,
            "text": "continued table rows",
            "table_markdown": "| beta | 92 |",
            "content_role": "table_body",
            "cleanup_action": "keep",
            "section_path_titles": ["4 Results"],
            "section_path_levels": [1],
            "continues_from_previous_page": False,
            "table_header_state": "missing",
        },
        {
            "block_id": "p0004_b0002",
            "type": "paragraph",
            "page": 4,
            "order": 2,
            "text": "1. First item\n2. Second item",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["4 Results", "A. Ablations"],
            "section_path_levels": [1, 2],
            "continues_from_previous_page": False,
            "list_marker_type": "numbered",
            "list_index_start": 1,
            "list_index_end": 2,
        },
        {
            "block_id": "p0005_b0001",
            "type": "paragraph",
            "page": 5,
            "order": 1,
            "text": "3. Third item\n4. Fourth item",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["4 Results", "A. Ablations"],
            "section_path_levels": [1, 2],
            "continues_from_previous_page": False,
            "list_continues_previous": True,
            "list_marker_type": "numbered",
            "list_index_start": 3,
            "list_index_end": 4,
        },
    ]

    chunk_plan_result = service._build_model_chunk_plan(
        blocks=blocks,
        document_name="paper.pdf",
        extract_profile="table_first",
        extract_granularity="medium",
    )

    assert chunk_plan_result["ok"] is True
    assert len(chunk_plan_result["chunks"]) == 2
    assert chunk_plan_result["chunks"][0]["chunk_type"] == "table"
    assert chunk_plan_result["chunks"][0]["block_ids"] == ["p0003_b0001", "p0004_b0001"]
    assert chunk_plan_result["chunks"][1]["chunk_type"] == "paragraph"
    assert chunk_plan_result["chunks"][1]["block_ids"] == ["p0004_b0002", "p0005_b0001"]


def test_online_mm_front_matter_signals_are_routed_out_of_body():
    service = OnlineMmIngestService()
    payload = service._normalize_window_payload(
        parsed={
            "document_metadata": {},
            "chunks": [
                {
                    "chunk_type": "paragraph",
                    "order": 1,
                    "text": "Abstract—A concise abstract.",
                    "content_role": "abstract_body",
                    "cleanup_action": "keep",
                    "section_path": ["Abstract"],
                    "section_level_path": [1],
                },
                {
                    "chunk_type": "paragraph",
                    "order": 2,
                    "text": "Index Terms—Autonomous Systems, Robotics",
                    "content_role": "body_paragraph",
                    "cleanup_action": "keep",
                },
                {
                    "chunk_type": "paragraph",
                    "order": 3,
                    "text": "This work was supported by Horizon Europe under Grant EC 101120732.",
                    "content_role": "body_paragraph",
                    "cleanup_action": "keep",
                },
                {
                    "chunk_type": "paragraph",
                    "order": 4,
                    "text": "*Corresponding author. Email: omkar.sawant@ntnu.no",
                    "content_role": "body_paragraph",
                    "cleanup_action": "keep",
                },
                {
                    "chunk_type": "paragraph",
                    "order": 5,
                    "text": "CCS Concepts—Human-centered computing~Interaction design",
                    "content_role": "body_paragraph",
                    "cleanup_action": "keep",
                },
            ],
        },
        page_numbers=[1],
    )

    assert payload["blocks"][0]["content_role"] == "abstract_body"
    assert payload["blocks"][0]["cleanup_action"] == "keep"
    assert payload["blocks"][1]["content_role"] == "front_matter_misc"
    assert payload["blocks"][1]["cleanup_action"] == "route_to_metadata"
    assert payload["blocks"][2]["content_role"] == "front_matter_misc"
    assert payload["blocks"][2]["cleanup_action"] == "route_to_metadata"
    assert payload["blocks"][3]["content_role"] == "front_matter_misc"
    assert payload["blocks"][3]["cleanup_action"] == "route_to_metadata"
    assert payload["blocks"][4]["content_role"] == "front_matter_misc"
    assert payload["blocks"][4]["cleanup_action"] == "route_to_metadata"


def test_online_mm_canonicalizes_structural_section_levels():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0001_b0001",
            "type": "paragraph",
            "page": 1,
            "order": 1,
            "text": "Related work body.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["II RELATED WORK"],
            "section_path_levels": [2],
        },
        {
            "block_id": "p0002_b0001",
            "type": "paragraph",
            "page": 2,
            "order": 1,
            "text": "Method subsection body.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["IV METHODOLOGY", "A Cross-Modal Wasserstein Autoencoder", "1) Objective"],
            "section_path_levels": [4, 1, 1],
        },
    ]
    order_index = {block["block_id"]: idx for idx, block in enumerate(blocks)}

    spine = service._build_section_spine(
        blocks=blocks,
        order_index=order_index,
        title_block_id="",
    )

    assert [row["section_title"] for row in spine] == [
        "II RELATED WORK",
        "IV METHODOLOGY",
        "A Cross-Modal Wasserstein Autoencoder",
        "1) Objective",
    ]
    assert [row["heading_level"] for row in spine] == [1, 1, 2, 3]
    assert spine[2]["parent_context_id"] == spine[1]["context_id"]
    assert spine[3]["parent_context_id"] == spine[2]["context_id"]
    assert spine[3]["path_levels"] == [1, 2, 3]


def test_online_mm_page_order_section_context_inherits_parent_heading_across_pages():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0001_b0001",
            "type": "paragraph",
            "page": 1,
            "order": 1,
            "text": "Method overview text.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["IV METHODOLOGY"],
            "section_path_levels": [1],
        },
        {
            "block_id": "p0002_b0001",
            "type": "paragraph",
            "page": 2,
            "order": 1,
            "text": "Navigation policy subsection body.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["B Navigation Policy Learning"],
            "section_path_levels": [1],
        },
    ]

    resolved = service._apply_page_order_section_context(blocks=blocks)

    assert resolved[0]["section_path_titles"] == ["IV METHODOLOGY"]
    assert resolved[0]["section_path_levels"] == [1]
    assert resolved[1]["section_path_titles"] == ["IV METHODOLOGY", "B Navigation Policy Learning"]
    assert resolved[1]["section_path_levels"] == [1, 2]


def test_online_mm_page_order_section_context_inherits_active_path_for_pathless_continuation():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0001_b0001",
            "type": "paragraph",
            "page": 1,
            "order": 1,
            "text": "Subsection opening paragraph.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["IV METHODOLOGY", "A Cross-Modal Wasserstein Autoencoder"],
            "section_path_levels": [1, 2],
        },
        {
            "block_id": "p0002_b0001",
            "type": "paragraph",
            "page": 2,
            "order": 1,
            "text": "Continuation on next page without repeated heading.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": [],
            "section_path_levels": [],
        },
    ]

    resolved = service._apply_page_order_section_context(blocks=blocks)

    assert resolved[1]["section_path_titles"] == [
        "IV METHODOLOGY",
        "A Cross-Modal Wasserstein Autoencoder",
    ]
    assert resolved[1]["section_path_levels"] == [1, 2]


def test_online_mm_materialize_chunks_promotes_abstract_out_of_front_matter():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0001_b0001",
            "type": "paragraph",
            "page": 1,
            "order": 1,
            "text": "Ashish Vaswani, Google Brain, attention@google.com",
            "content_role": "front_matter_misc",
            "cleanup_action": "route_to_metadata",
            "section_path_titles": [],
            "section_path_levels": [],
        },
        {
            "block_id": "p0001_b0002",
            "type": "paragraph",
            "page": 1,
            "order": 2,
            "text": "Abstract—This paper introduces a robust chunking pipeline.",
            "content_role": "abstract_body",
            "cleanup_action": "keep",
            "section_path_titles": ["Abstract"],
            "section_path_levels": [1],
        },
        {
            "block_id": "p0001_b0003",
            "type": "paragraph",
            "page": 1,
            "order": 3,
            "text": "Introduction body text follows here.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["I. INTRODUCTION"],
            "section_path_levels": [1],
        },
    ]

    chunk_plan_result = service._build_model_chunk_plan(
        blocks=blocks,
        document_name="paper.pdf",
        extract_profile="general",
        extract_granularity="medium",
    )
    materialized = service._materialize_chunks(
        blocks=blocks,
        chunk_plan=chunk_plan_result["chunks"],
        document_name="paper.pdf",
        extract_profile="general",
        extract_granularity="medium",
        source_model="qwen3-vl-flash",
        document_metadata_seed={"title": "Test Paper"},
    )

    assert [row["section_title"] for row in materialized["section_spine"]] == ["Abstract", "I. INTRODUCTION"]

    abstract_chunk = materialized["chunks"][0]
    assert abstract_chunk["metadata"]["parent_id"] == "sec_p0001_b0002_abstract"
    assert abstract_chunk["metadata"]["section_title"] == "Abstract"

    front_matter_context = next(ctx for ctx in materialized["context_chunks"] if ctx["id"] == "front_matter")
    assert "Abstract—This paper introduces" not in front_matter_context["content"]

    abstract_context = next(ctx for ctx in materialized["context_chunks"] if ctx["id"] == "sec_p0001_b0002_abstract")
    assert "Abstract—This paper introduces" in abstract_context["content"]


def test_online_mm_section_spine_nests_appendix_subheadings_from_section_path():
    service = OnlineMmIngestService()
    blocks = [
        {
            "block_id": "p0001_b0001",
            "type": "paragraph",
            "page": 1,
            "order": 1,
            "text": "Appendix intro text.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["APPENDIX: EVALUATOR DETAILS"],
            "section_path_levels": [1],
        },
        {
            "block_id": "p0001_b0002",
            "type": "paragraph",
            "page": 1,
            "order": 2,
            "text": "Subsection body text.",
            "content_role": "body_paragraph",
            "cleanup_action": "keep",
            "section_path_titles": ["APPENDIX: EVALUATOR DETAILS", "Feasibility vetoes:"],
            "section_path_levels": [1, 2],
        },
        {
            "block_id": "p0002_b0001",
            "type": "paragraph",
            "page": 2,
            "order": 1,
            "text": "[1] Vaswani et al.",
            "content_role": "reference_entry",
            "cleanup_action": "keep",
            "section_path_titles": ["REFERENCES"],
            "section_path_levels": [1],
        },
    ]
    order_index = {block["block_id"]: idx for idx, block in enumerate(blocks)}

    spine = service._build_section_spine(
        blocks=blocks,
        order_index=order_index,
        title_block_id="",
    )

    assert [row["section_title"] for row in spine] == [
        "APPENDIX: EVALUATOR DETAILS",
        "Feasibility vetoes:",
        "REFERENCES",
    ]
    assert spine[0]["heading_level"] == 1
    assert spine[0]["section_type"] == "appendix"
    assert spine[1]["heading_level"] == 2
    assert spine[1]["parent_context_id"] == spine[0]["context_id"]
    assert spine[1]["path_levels"] == [1, 2]
    assert spine[2]["heading_level"] == 1
    assert spine[2]["parent_context_id"] is None


def test_online_mm_ingest_service_returns_failure_reason_when_chunk_plan_empty(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(online_mm_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(
        online_mm_module.settings,
        "aliyun_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "is_available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(service, "_count_pages", lambda _pdf_path: 1)
    monkeypatch.setattr(service, "_render_pdf_pages", lambda **_kwargs: [tmp_path / "page.png"])

    async def _fake_extract_page_blocks(
        *,
        page_number: int,
        image_path,
        document_name: str,
        extract_profile: str,
        extract_granularity: str,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int,
    ):
        del page_number, image_path, document_name, extract_profile, extract_granularity, api_key, base_url, model, max_tokens
        return {
            "ok": True,
            "blocks": [
                {
                    "block_id": "p0001_b0001",
                    "type": "paragraph",
                    "page": 1,
                    "order": 1,
                    "text": "Only one block",
                    "content_role": "body_paragraph",
                    "cleanup_action": "keep",
                    "section_path_titles": ["1 Introduction"],
                    "section_path_levels": [1],
                }
            ],
            "document_metadata": {},
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(service, "_extract_page_blocks", _fake_extract_page_blocks)
    monkeypatch.setattr(
        service,
        "_build_model_chunk_plan",
        lambda **_kwargs: {"ok": True, "chunks": [], "usage": {}},
    )

    result = asyncio.run(
        service.ingest_pdf(
            file_path=str(pdf_path),
            document_name="paper.pdf",
            extract_profile="general",
        )
    )

    assert result["applied"] is False
    assert result["failure_reason"] == "chunk_plan_empty"
    assert result["report"]["block_count"] == 1


def test_online_mm_normalize_window_payload_accepts_chunk_schema():
    service = OnlineMmIngestService()
    parsed = {
        "pages": [
            {
                "page": 1,
                "document_metadata": {
                    "title": "Test Paper",
                    "authors": ["A. Author"],
                    "front_matter_items": ["Index Terms—Transformers"],
                },
                "chunks": [
                    {
                        "chunk_type": "paragraph",
                        "order": 1,
                        "text": "Abstract—A concise abstract.",
                        "content_role": "abstract_body",
                        "cleanup_action": "keep",
                        "section_path": ["Abstract"],
                        "section_level_path": [1],
                        "zone": "centered",
                        "span": "single-column",
                        "continues_previous": "true",
                        "chunk_boundary": "continued",
                        "list_continues_previous": "false",
                        "list_marker_type": "numeric",
                        "list_index_start": "1",
                        "list_index_end": "2",
                    },
                    {
                        "chunk_type": "paragraph",
                        "order": 2,
                        "text": "Author metadata",
                        "content_role": "front_matter",
                        "cleanup_action": "metadata",
                    },
                ],
            }
        ]
    }

    payload = service._normalize_window_payload(parsed=parsed, page_numbers=[1])

    assert payload["document_metadata"]["title"] == "Test Paper"
    assert payload["document_metadata"]["authors"] == ["A. Author"]
    assert payload["document_metadata"]["front_matter_items"] == ["Index Terms—Transformers"]
    assert len(payload["blocks"]) == 2
    assert payload["blocks"][0]["type"] == "paragraph"
    assert payload["blocks"][0]["zone"] == "full_width"
    assert payload["blocks"][0]["span"] == "single_column"
    assert payload["blocks"][0]["continues_from_previous_page"] is True
    assert payload["blocks"][0]["chunk_boundary"] == "open"
    assert payload["blocks"][0]["list_continues_previous"] is False
    assert payload["blocks"][0]["list_marker_type"] == "numbered"
    assert payload["blocks"][0]["list_index_start"] == 1
    assert payload["blocks"][0]["list_index_end"] == 2
    assert payload["blocks"][1]["cleanup_action"] == "route_to_metadata"
    assert payload["blocks"][1]["content_role"] == "front_matter_misc"


def test_online_mm_normalize_window_payload_accepts_optional_table_merge_hints():
    service = OnlineMmIngestService()
    parsed = {
        "document_metadata": {},
        "chunks": [
            {
                "chunk_type": "table",
                "order": 1,
                "text": "Table continuation",
                "table_markdown": "| beta | 92 |",
                "content_role": "table_body",
                "cleanup_action": "keep",
                "table_header_state": "header_missing",
                "table_continues_previous": "true",
                "table_id_hint": "table_2",
                "table_caption": "Table 2. Results",
            }
        ],
    }

    payload = service._normalize_window_payload(parsed=parsed, page_numbers=[4])

    assert len(payload["blocks"]) == 1
    block = payload["blocks"][0]
    assert block["type"] == "table"
    assert block["table_header_state"] == "missing"
    assert block["table_continues_previous"] is True
    assert block["table_id_hint"] == "table_2"
    assert block["table_caption"] == "Table 2. Results"


def test_online_mm_preprocess_rendered_page_image_trims_whitespace_and_downscales(monkeypatch, tmp_path):
    image_path = tmp_path / "page_0001.png"
    image = Image.new("RGB", (2200, 2800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((420, 320, 1760, 2400), fill="black")
    image.save(image_path, format="PNG")

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_trim_whitespace", True, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_trim_padding_px", 16, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_image_max_side", 1400, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_image_max_pixels", 1_100_000, raising=False)

    result = OnlineMmIngestService._preprocess_rendered_page_image(image_path=image_path)

    assert result["cropped"] is True
    assert result["resized"] is True
    assert result["original_size"] == [2200, 2800]

    with Image.open(image_path) as processed:
        assert processed.size[0] < 2200
        assert processed.size[1] < 2800
        assert max(processed.size) <= 1400
        assert processed.size[0] * processed.size[1] <= 1_100_000

        mask = processed.convert("L").point(
            lambda pixel: 255 if pixel < online_mm_module._PAGE_TRIM_WHITE_THRESHOLD else 0,
            mode="L",
        )
        assert mask.getbbox() is not None
        assert sum(1 for pixel in mask.getdata() if int(pixel) > 0) > 1000

    assert result["final_size"][0] <= 1400
    assert result["final_size"][0] * result["final_size"][1] <= 1_100_000


def test_online_mm_extract_pdf_blocks_splits_window_when_completion_hits_cap(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    page_images = []
    for index in range(1, 5):
        page_image = tmp_path / f"page_{index:04d}.png"
        page_image.write_bytes(b"fake-image")
        page_images.append(page_image)

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(online_mm_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(
        online_mm_module.settings,
        "aliyun_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_pages_per_call", 3, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_extract_max_concurrency", 1, raising=False)
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "is_available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(service, "_count_pages", lambda _pdf_path: 4)
    monkeypatch.setattr(service, "_render_pdf_pages", lambda **_kwargs: list(page_images))

    def _page_tuple(image_paths) -> tuple[int, ...]:
        values = []
        for path in list(image_paths or []):
            stem = str(path).split("page_")[-1].split(".")[0]
            values.append(int(stem))
        return tuple(values)

    call_pages: list[tuple[int, ...]] = []

    async def _fake_chat_json(
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths,
        max_tokens: int,
        temperature: float = 0.2,
    ):
        del api_key, base_url, model, system_prompt, user_prompt, temperature
        pages = _page_tuple(image_paths)
        call_pages.append(pages)
        assert max_tokens <= 8192
        if pages == (1, 2, 3):
            return {
                "parsed": {
                    "pages": [
                        {"page": 1, "document_metadata": {}, "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p1", "content_role": "body_paragraph", "cleanup_action": "keep"}]},
                        {"page": 2, "document_metadata": {}, "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p2", "content_role": "body_paragraph", "cleanup_action": "keep"}]},
                        {"page": 3, "document_metadata": {}, "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p3", "content_role": "body_paragraph", "cleanup_action": "keep"}]},
                    ]
                },
                "usage": {"prompt_tokens": 30, "completion_tokens": 8192, "total_tokens": 8222},
                "model": "qwen3-vl-flash",
                "raw_text": "{}",
            }
        if pages == (1, 2):
            return {
                "parsed": {
                    "pages": [
                        {"page": 1, "document_metadata": {}, "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p1", "content_role": "body_paragraph", "cleanup_action": "keep"}]},
                        {"page": 2, "document_metadata": {}, "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p2", "content_role": "body_paragraph", "cleanup_action": "keep"}]},
                    ]
                },
                "usage": {"prompt_tokens": 20, "completion_tokens": 2000, "total_tokens": 2020},
                "model": "qwen3-vl-flash",
                "raw_text": "{}",
            }
        if pages == (3,):
            return {
                "parsed": {
                    "document_metadata": {},
                    "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p3", "content_role": "body_paragraph", "cleanup_action": "keep"}],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 900, "total_tokens": 910},
                "model": "qwen3-vl-flash",
                "raw_text": "{}",
            }
        if pages == (4,):
            return {
                "parsed": {
                    "document_metadata": {},
                    "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p4", "content_role": "body_paragraph", "cleanup_action": "keep"}],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 800, "total_tokens": 810},
                "model": "qwen3-vl-flash",
                "raw_text": "{}",
            }
        raise AssertionError(f"unexpected pages: {pages}")

    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "chat_json",
        staticmethod(_fake_chat_json),
    )

    result = asyncio.run(
        service.extract_pdf_blocks(
            file_path=str(pdf_path),
            document_name="paper.pdf",
            extract_profile="general",
            extract_granularity="fine",
        )
    )

    assert result["ok"] is True
    assert set(call_pages) == {(1, 2, 3), (1, 2), (3,), (4,)}
    assert [entry["page_numbers"] for entry in result["window_cache"]] == [[1, 2], [3], [4]]
    assert [block["block_id"] for block in result["blocks"]] == [
        "p0001_b0001",
        "p0002_b0001",
        "p0003_b0001",
        "p0004_b0001",
    ]
    assert result["report"]["resolved_window_count"] == 3


def test_online_mm_extract_pdf_blocks_reuses_cached_windows(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    page_images = []
    for index in range(1, 5):
        page_image = tmp_path / f"page_{index:04d}.png"
        page_image.write_bytes(b"fake-image")
        page_images.append(page_image)

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(online_mm_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(
        online_mm_module.settings,
        "aliyun_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_pages_per_call", 3, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_extract_max_concurrency", 1, raising=False)
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "is_available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(service, "_count_pages", lambda _pdf_path: 4)
    monkeypatch.setattr(service, "_render_pdf_pages", lambda **_kwargs: list(page_images))

    def _page_tuple(image_paths) -> tuple[int, ...]:
        values = []
        for path in list(image_paths or []):
            stem = str(path).split("page_")[-1].split(".")[0]
            values.append(int(stem))
        return tuple(values)

    call_pages: list[tuple[int, ...]] = []
    cached_windows = [
        {
            "page_numbers": [1, 2],
            "blocks": [
                {"block_id": "p0001_b0001", "type": "paragraph", "page": 1, "order": 1, "text": "p1", "content_role": "body_paragraph", "cleanup_action": "keep"},
                {"block_id": "p0002_b0001", "type": "paragraph", "page": 2, "order": 1, "text": "p2", "content_role": "body_paragraph", "cleanup_action": "keep"},
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 2000, "total_tokens": 2020},
            "model": "qwen3-vl-flash",
            "document_name": "paper.pdf",
            "extract_profile": "general",
            "extract_granularity": "fine",
        },
        {
            "page_numbers": [3],
            "blocks": [
                {"block_id": "p0003_b0001", "type": "paragraph", "page": 3, "order": 1, "text": "p3", "content_role": "body_paragraph", "cleanup_action": "keep"},
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 900, "total_tokens": 910},
            "model": "qwen3-vl-flash",
            "document_name": "paper.pdf",
            "extract_profile": "general",
            "extract_granularity": "fine",
        },
    ]

    async def _fake_chat_json(
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths,
        max_tokens: int,
        temperature: float = 0.2,
    ):
        del api_key, base_url, model, system_prompt, user_prompt, max_tokens, temperature
        pages = _page_tuple(image_paths)
        call_pages.append(pages)
        assert pages == (4,)
        return {
            "parsed": {
                "document_metadata": {},
                "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p4", "content_role": "body_paragraph", "cleanup_action": "keep"}],
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 800, "total_tokens": 810},
            "model": "qwen3-vl-flash",
            "raw_text": "{}",
        }

    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "chat_json",
        staticmethod(_fake_chat_json),
    )

    result = asyncio.run(
        service.extract_pdf_blocks(
            file_path=str(pdf_path),
            document_name="paper.pdf",
            extract_profile="general",
            extract_granularity="fine",
            cached_windows=cached_windows,
        )
    )

    assert result["ok"] is True
    assert call_pages == [(4,)]
    assert [entry["page_numbers"] for entry in result["window_cache"]] == [[1, 2], [3], [4]]
    assert [block["block_id"] for block in result["blocks"]] == [
        "p0001_b0001",
        "p0002_b0001",
        "p0003_b0001",
        "p0004_b0001",
    ]


def test_online_mm_extract_pdf_blocks_splits_multi_page_window_on_data_inspection_failed(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    page_images = []
    for index in range(1, 5):
        page_image = tmp_path / f"page_{index:04d}.png"
        page_image.write_bytes(b"fake-image")
        page_images.append(page_image)

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(online_mm_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(
        online_mm_module.settings,
        "aliyun_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_pages_per_call", 3, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_extract_max_concurrency", 1, raising=False)
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "is_available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(service, "_count_pages", lambda _pdf_path: 4)
    monkeypatch.setattr(service, "_render_pdf_pages", lambda **_kwargs: list(page_images))

    def _page_tuple(image_paths) -> tuple[int, ...]:
        values = []
        for path in list(image_paths or []):
            stem = str(path).split("page_")[-1].split(".")[0]
            values.append(int(stem))
        return tuple(values)

    calls: list[tuple[str, tuple[int, ...]]] = []

    async def _fake_chat_json(
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths,
        max_tokens: int,
        temperature: float = 0.2,
    ):
        del api_key, base_url, system_prompt, user_prompt, max_tokens, temperature
        pages = _page_tuple(image_paths)
        calls.append((model, pages))
        if pages == (1, 2, 3):
            raise RuntimeError(
                "dashscope_multimodal_failed:400:DataInspectionFailed:"
                "<400> InternalError.Algo.DataInspectionFailed: Input image data may contain inappropriate content."
            )
        if pages == (4,):
            return {
                "parsed": {
                    "document_metadata": {},
                    "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p4", "content_role": "body_paragraph", "cleanup_action": "keep"}],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 800, "total_tokens": 810},
                "model": model,
                "raw_text": "{}",
            }
        return {
            "parsed": {
                "document_metadata": {},
                "chunks": [{"chunk_type": "paragraph", "order": 1, "text": f"p{pages[0]}", "content_role": "body_paragraph", "cleanup_action": "keep"}],
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 900, "total_tokens": 910},
            "model": model,
            "raw_text": "{}",
        }

    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "chat_json",
        staticmethod(_fake_chat_json),
    )

    result = asyncio.run(
        service.extract_pdf_blocks(
            file_path=str(pdf_path),
            document_name="paper.pdf",
            extract_profile="general",
            extract_granularity="fine",
        )
    )

    assert result["ok"] is True
    assert [entry["page_numbers"] for entry in result["window_cache"]] == [[1], [2], [3], [4]]
    assert [block["block_id"] for block in result["blocks"]] == [
        "p0001_b0001",
        "p0002_b0001",
        "p0003_b0001",
        "p0004_b0001",
    ]
    assert ("qwen3-vl-flash", (1, 2, 3)) in calls
    assert all(model == "qwen3-vl-flash" for model, _pages in calls)


def test_online_mm_extract_pdf_blocks_preserves_successful_window_cache_on_failed_window(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    page_images = []
    for index in range(1, 3):
        page_image = tmp_path / f"page_{index:04d}.png"
        page_image.write_bytes(b"fake-image")
        page_images.append(page_image)

    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_ingest_enabled", True)
    monkeypatch.setattr(online_mm_module.settings, "aliyun_api_key", "test-key")
    monkeypatch.setattr(
        online_mm_module.settings,
        "aliyun_base_url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_pages_per_call", 1, raising=False)
    monkeypatch.setattr(online_mm_module.settings, "kb_online_mm_extract_max_concurrency", 1, raising=False)
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "is_available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(service, "_count_pages", lambda _pdf_path: 2)
    monkeypatch.setattr(service, "_render_pdf_pages", lambda **_kwargs: list(page_images))

    def _page_tuple(image_paths) -> tuple[int, ...]:
        values = []
        for path in list(image_paths or []):
            stem = str(path).split("page_")[-1].split(".")[0]
            values.append(int(stem))
        return tuple(values)

    async def _fake_chat_json(
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths,
        max_tokens: int,
        temperature: float = 0.2,
    ):
        del api_key, base_url, model, system_prompt, user_prompt, max_tokens, temperature
        pages = _page_tuple(image_paths)
        if pages == (1,):
            return {
                "parsed": {
                    "document_metadata": {},
                    "chunks": [{"chunk_type": "paragraph", "order": 1, "text": "p1", "content_role": "body_paragraph", "cleanup_action": "keep"}],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 900, "total_tokens": 910},
                "model": "qwen3-vl-flash",
                "raw_text": "{}",
            }
        raise RuntimeError(
            "dashscope_multimodal_failed:400:DataInspectionFailed:"
            "<400> InternalError.Algo.DataInspectionFailed: Input image data may contain inappropriate content."
        )

    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "chat_json",
        staticmethod(_fake_chat_json),
    )

    result = asyncio.run(
        service.extract_pdf_blocks(
            file_path=str(pdf_path),
            document_name="paper.pdf",
            extract_profile="general",
            extract_granularity="fine",
        )
    )

    assert result["ok"] is False
    assert "DataInspectionFailed" in str(result["failure_reason"])
    assert [entry["page_numbers"] for entry in result["window_cache"]] == [[1]]


def test_online_mm_extract_page_blocks_retries_once_on_remote_disconnect(monkeypatch, tmp_path):
    service = OnlineMmIngestService()
    image_path = tmp_path / "page_0006.png"
    image_path.write_bytes(b"fake-image")

    async def _fast_sleep(_seconds: float):
        return None

    attempts = {"count": 0}

    async def _fake_chat_json(
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_paths,
        max_tokens: int,
        temperature: float = 0.2,
    ):
        del api_key, base_url, model, system_prompt, user_prompt, image_paths, max_tokens, temperature
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
        return {
            "parsed": {
                "document_metadata": {},
                "chunks": [
                    {
                        "chunk_type": "paragraph",
                        "order": 1,
                        "text": "Recovered after retry.",
                        "content_role": "body_paragraph",
                        "cleanup_action": "keep",
                        "section_path": ["1 Introduction"],
                        "section_level_path": [1],
                    }
                ],
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "model": "qwen3-vl-flash",
            "raw_text": "{}",
        }

    monkeypatch.setattr(online_mm_module.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(
        online_mm_module.DashScopeMultimodalService,
        "chat_json",
        staticmethod(_fake_chat_json),
    )

    result = asyncio.run(
        service._extract_page_blocks(
            image_path=image_path,
            page_number=6,
            document_name="paper.pdf",
            extract_profile="general",
            extract_granularity="medium",
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/api/v1",
            model="qwen3-vl-flash",
            max_tokens=4096,
        )
    )

    assert result["ok"] is True
    assert attempts["count"] == 2
    assert result["page_numbers"] == [6]
    assert [block["block_id"] for block in result["blocks"]] == ["p0006_b0001"]


def test_normalize_page_payload_accepts_chunk_aliases_for_reference_pages():
    service = OnlineMmIngestService()

    payload = service._normalize_page_payload(
        parsed={
            "document_metadata": {},
            "items": [
                {
                    "chunk_type": "reference",
                    "order": 1,
                    "text": "[1] Vaswani et al. Attention Is All You Need.",
                    "content_role": "reference_entry",
                    "cleanup_action": "keep",
                }
            ],
        },
        page_number=9,
    )

    assert len(payload["blocks"]) == 1
    block = payload["blocks"][0]
    assert block["type"] == "paragraph"
    assert block["content_role"] == "reference_entry"
    assert block["text"].startswith("[1] Vaswani")


def test_normalize_page_payload_coerces_textual_reference_chunk_types_to_paragraph():
    service = OnlineMmIngestService()

    payload = service._normalize_page_payload(
        parsed={
            "document_metadata": {},
            "chunks": [
                {
                    "chunk_type": "bibliography",
                    "order": 2,
                    "text": "Moore, C. (2015). Moral disengagement.",
                    "content_role": "reference_entry",
                }
            ],
        },
        page_number=9,
    )

    assert len(payload["blocks"]) == 1
    assert payload["blocks"][0]["type"] == "paragraph"
    assert payload["blocks"][0]["content_role"] == "reference_entry"
