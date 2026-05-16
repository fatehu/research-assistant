from __future__ import annotations

import asyncio
import json

from app.config import settings
from app.services.local_structured_pdf import (
    LocalOllamaQwenVlPageParser,
    PdfBBox,
    PdfHybridParsedPage,
    PdfHybridTriageResult,
    PdfHybridTriageSignals,
    PdfResolvedLine,
    PdfResolvedPage,
)


def _resolved_line(
    *,
    line_id: str,
    text: str,
    order: int,
    x0: float = 80.0,
    top: float = 100.0,
    x1: float = 320.0,
    bottom: float = 114.0,
) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=1,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=12.0,
        dominant_font_name="Times",
        band="body",
        region="main",
        column_id="main",
        reading_order=order,
    )


def test_ollama_page_parser_materializes_blocks_from_source_line_ids(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1})(),
        lines=[
            _resolved_line(line_id="l1", text="Introduction", order=1),
            _resolved_line(line_id="l2", text="First sentence.", order=2, top=120.0, bottom=134.0),
            _resolved_line(line_id="l3", text="Second sentence.", order=3, top=138.0, bottom=152.0),
        ],
        column_count=1,
    )
    triage = PdfHybridTriageResult(
        page=1,
        page_type="mixed_layout",
        decision="backend",
        confidence=0.8,
        reasons=["page_type:mixed_layout"],
        signals=PdfHybridTriageSignals(text_line_count=3, double_column=False),
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "ZmFrZQ==")

    async def _fake_invoke_ollama_json(
        *, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        assert model == "qwen-vl-local"
        assert image_b64 == "ZmFrZQ=="
        assert "source_rows" in user_prompt
        assert '"line_id"' not in user_prompt
        assert use_response_format is True
        assert max_output_tokens == 2400
        return {
            "page": 1,
            "page_role": "body",
            "blocks": [
                {
                    "block_id": "mm_p0001_b0001",
                    "kind": "heading",
                    "reading_order": 1,
                    "source_line_ids": ["l1"],
                    "zone": "main",
                    "merge_strategy": "space",
                    "confidence": 0.92,
                },
                {
                    "block_id": "mm_p0001_b0002",
                    "kind": "paragraph",
                    "reading_order": 2,
                    "source_line_ids": ["l2", "l3"],
                    "zone": "main",
                    "merge_strategy": "space",
                    "confidence": 0.88,
                },
            ],
            "notes": ["stable"],
        }

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
            triage_result=triage,
        )
    )

    assert result.used is True
    assert result.page_role == "body"
    assert result.retry_used is False
    assert result.attempts[0].accepted is True
    assert result.attempts[0].reason == "multiple_blocks"
    assert [block.kind for block in result.blocks] == ["heading", "paragraph"]
    assert result.blocks[0].text == "Introduction"
    assert result.blocks[1].text == "First sentence. Second sentence."
    assert result.blocks[1].source_line_ids == ["l2", "l3"]


def test_ollama_page_parser_parse_pages_uses_single_batch_request_and_orders_by_page_number(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_pages = [
        PdfResolvedPage(
            meta=type("Meta", (), {"page": 2})(),
            lines=[_resolved_line(line_id="p2_l1", text="Page 2", order=1)],
            column_count=1,
        ),
        PdfResolvedPage(
            meta=type("Meta", (), {"page": 1})(),
            lines=[_resolved_line(line_id="p1_l1", text="Page 1", order=1)],
            column_count=1,
        ),
    ]
    triage_results = [
        PdfHybridTriageResult(
            page=1,
            page_type="plain_text",
            decision="local",
            confidence=0.9,
            reasons=["page_type:plain_text"],
            signals=PdfHybridTriageSignals(text_line_count=1),
        ),
        PdfHybridTriageResult(
            page=2,
            page_type="mixed_layout",
            decision="backend",
            confidence=0.8,
            reasons=["page_type:mixed_layout"],
            signals=PdfHybridTriageSignals(text_line_count=1),
        ),
    ]

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])

    render_calls: list[int] = []

    def _fake_render_page_image_base64(pdf_path: str, page: int, max_image_side: int | None = None) -> str:
        del pdf_path, max_image_side
        render_calls.append(page)
        return f"img-{page}"

    monkeypatch.setattr(parser, "_render_page_image_base64", _fake_render_page_image_base64)

    batch_calls: list[tuple[str, tuple[str, ...], bool, int]] = []

    async def _fake_batch(
        *,
        model: str,
        user_prompt: str,
        image_b64s,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ):
        batch_calls.append((model, tuple(image_b64s), use_response_format, max_output_tokens))
        assert model == "qwen-vl-local"
        assert tuple(image_b64s) == ("img-1", "img-2")
        assert "page briefs" in user_prompt
        assert '"line_id"' not in user_prompt
        assert "page_width" not in user_prompt
        assert "page_height" not in user_prompt
        assert "triage" not in user_prompt
        assert "source_rows" not in user_prompt
        assert "kind_whitelist" not in user_prompt
        assert use_response_format is True
        return {
            "pages": [
                {
                    "page": 2,
                    "page_role": "body",
                    "texts": [
                        {
                            "label": "section_header",
                            "text": "Page 2 heading",
                            "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                            "meta": {"level": 1},
                        },
                        {
                            "label": "text",
                            "text": "Page 2 body",
                            "bbox": {"x0": 80.0, "top": 120.0, "x1": 320.0, "bottom": 140.0},
                        }
                    ],
                },
                {
                    "page": 1,
                    "page_role": "body",
                    "texts": [
                        {
                            "label": "section_header",
                            "text": "Page 1",
                            "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                            "meta": {"level": 1},
                        }
                    ],
                },
            ]
        }, "", "{\"pages\":[...]}"

    monkeypatch.setattr(parser, "_invoke_ollama_batch_json", _fake_batch)

    result = asyncio.run(
        parser.parse_pages(
            pdf_path="/tmp/demo.pdf",
            resolved_pages=resolved_pages,
            triage_results=triage_results,
        )
    )

    assert [item.page for item in result] == [1, 2]
    assert render_calls == [1, 2]
    assert batch_calls == [("qwen-vl-local", ("img-1", "img-2"), True, 4712)]
    assert result[0].used is True
    assert result[1].used is True
    assert result[0].blocks[0].text == "Page 1"
    assert result[1].blocks[0].text == "Page 2 heading"
    assert result[1].blocks[1].text == "Page 2 body"


def test_ollama_page_parser_parse_pages_accepts_single_docling_like_batch_payload(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_pages = [
        PdfResolvedPage(
            meta=type("Meta", (), {"page": 1})(),
            lines=[
                _resolved_line(line_id="p1_l1", text="Poster title", order=1, top=72.0, bottom=120.0),
                _resolved_line(line_id="p1_l2", text="Recovered OCR paragraph", order=2, top=150.0, bottom=190.0),
            ],
            column_count=1,
        )
    ]
    triage_results = [
        PdfHybridTriageResult(
            page=1,
            page_type="visual_or_scanned",
            decision="backend",
            confidence=0.95,
            reasons=["page_type:visual_or_scanned"],
            signals=PdfHybridTriageSignals(text_line_count=2),
        )
    ]

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "img-1")

    batch_calls: list[tuple[str, tuple[str, ...], bool, int]] = []

    async def _fake_batch(
        *,
        model: str,
        user_prompt: str,
        image_b64s,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ):
        batch_calls.append((model, tuple(image_b64s), use_response_format, max_output_tokens))
        assert model == "qwen-vl-local"
        assert tuple(image_b64s) == ("img-1",)
        assert "page briefs" in user_prompt
        assert "page_width" not in user_prompt
        assert "source_rows" not in user_prompt
        return {
            "page": 1,
            "page_role": "body",
            "texts": [
                {
                    "label": "section_header",
                    "text": "Poster title",
                    "bbox": {"x0": 72.0, "top": 72.0, "x1": 520.0, "bottom": 120.0},
                    "meta": {"level": 1},
                },
                {
                    "label": "text",
                    "text": "Recovered OCR paragraph",
                    "bbox": {"x0": 96.0, "top": 150.0, "x1": 520.0, "bottom": 190.0},
                },
            ],
            "notes": ["single-page-docling-like"],
        }, "", "{\"page\":1,\"page_role\":\"body\",\"texts\":[...]}"

    monkeypatch.setattr(parser, "_invoke_ollama_batch_json", _fake_batch)

    result = asyncio.run(
        parser.parse_pages(
            pdf_path="/tmp/demo.pdf",
            resolved_pages=resolved_pages,
            triage_results=triage_results,
        )
    )

    assert [item.page for item in result] == [1]
    assert batch_calls == [("qwen-vl-local", ("img-1",), False, 3712)]
    assert result[0].used is True
    assert result[0].attempts[0].accepted is True
    assert result[0].attempts[0].reason == "has_unanchored_ocr_blocks"
    assert [block.text for block in result[0].blocks] == ["Poster title", "Recovered OCR paragraph"]
    assert result[0].page_role == "body"


def test_ollama_page_parser_parse_pages_splits_visual_batch_from_structured_pages(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_pages = [
        PdfResolvedPage(
            meta=type("Meta", (), {"page": 1})(),
            lines=[_resolved_line(line_id="p1_l1", text="Structured page", order=1)],
            column_count=1,
        ),
        PdfResolvedPage(
            meta=type("Meta", (), {"page": 2})(),
            lines=[_resolved_line(line_id="p2_l1", text="Poster title", order=1)],
            column_count=1,
        ),
    ]
    triage_results = [
        PdfHybridTriageResult(
            page=1,
            page_type="mixed_layout",
            decision="backend",
            confidence=0.9,
            reasons=["page_type:mixed_layout"],
            signals=PdfHybridTriageSignals(text_line_count=1),
        ),
        PdfHybridTriageResult(
            page=2,
            page_type="visual_or_scanned",
            decision="backend",
            confidence=0.95,
            reasons=["page_type:visual_or_scanned"],
            signals=PdfHybridTriageSignals(text_line_count=1),
        ),
    ]

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda pdf_path, page, max_image_side=None: f"img-{page}")

    batch_calls: list[tuple[str, tuple[str, ...], bool, int]] = []

    async def _fake_batch(
        *,
        model: str,
        user_prompt: str,
        image_b64s,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ):
        del user_prompt
        batch_calls.append((model, tuple(image_b64s), use_response_format, max_output_tokens))
        if tuple(image_b64s) == ("img-1",):
            return {
                "pages": [
                    {
                        "page": 1,
                        "page_role": "body",
                        "texts": [
                            {
                                "label": "section_header",
                                "text": "Structured page",
                                "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                                "meta": {"level": 1},
                            }
                        ],
                    }
                ]
            }, "", "{\"pages\":[{\"page\":1}]}"
        return {
            "pages": [
                {
                    "page": 2,
                    "page_role": "body",
                    "texts": [
                        {
                            "label": "text",
                            "text": "Recovered OCR paragraph",
                            "bbox": {"x0": 96.0, "top": 150.0, "x1": 520.0, "bottom": 190.0},
                        }
                    ],
                }
            ]
        }, "", "{\"pages\":[{\"page\":2}]}"

    monkeypatch.setattr(parser, "_invoke_ollama_batch_json", _fake_batch)

    result = asyncio.run(
        parser.parse_pages(
            pdf_path="/tmp/demo.pdf",
            resolved_pages=resolved_pages,
            triage_results=triage_results,
        )
    )

    assert [item.page for item in result] == [1, 2]
    assert batch_calls == [
        ("qwen-vl-local", ("img-1",), True, 2912),
        ("qwen-vl-local", ("img-2",), False, 3712),
    ]
    assert result[0].used is True
    assert result[1].used is True
    assert result[0].blocks[0].text == "Structured page"
    assert result[1].blocks[0].text == "Recovered OCR paragraph"


def test_ollama_page_parser_parse_pages_retries_batch_with_next_model(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_pages = [
        PdfResolvedPage(
            meta=type("Meta", (), {"page": 1})(),
            lines=[_resolved_line(line_id="p1_l1", text="Page 1", order=1)],
            column_count=1,
        )
    ]
    triage_results = [
        PdfHybridTriageResult(
            page=1,
            page_type="mixed_layout",
            decision="backend",
            confidence=0.8,
            reasons=["page_type:mixed_layout"],
            signals=PdfHybridTriageSignals(text_line_count=1),
        )
    ]

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-cheap", "qwen-vl-fallback"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "img-1")

    batch_calls: list[str] = []

    async def _fake_batch(
        *,
        model: str,
        user_prompt: str,
        image_b64s,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ):
        del user_prompt, image_b64s, use_response_format, max_output_tokens
        batch_calls.append(model)
        if model == "qwen-vl-cheap":
            parser._last_invoke_protocol = "openai_compat"
            parser._last_raw_response_preview = "cheap preview"
            return None, "ollama_batch_parse_failed", "cheap preview"
        parser._last_invoke_protocol = "native"
        parser._last_raw_response_preview = "fallback preview"
        return {
            "pages": [
                {
                    "page": 1,
                    "page_role": "body",
                    "texts": [
                        {
                            "label": "section_header",
                            "text": "Page 1",
                            "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                            "meta": {"level": 1},
                        }
                    ],
                }
            ]
        }, "", "fallback preview"

    monkeypatch.setattr(parser, "_invoke_ollama_batch_json", _fake_batch)

    result = asyncio.run(
        parser.parse_pages(
            pdf_path="/tmp/demo.pdf",
            resolved_pages=resolved_pages,
            triage_results=triage_results,
        )
    )

    assert batch_calls == ["qwen-vl-cheap", "qwen-vl-fallback"]
    assert result[0].model == "qwen-vl-fallback"
    assert result[0].attempted_models == ["qwen-vl-cheap", "qwen-vl-fallback"]
    assert result[0].protocol == "native"
    assert result[0].raw_response_preview == "fallback preview"
    assert result[0].used is True


def test_ollama_openai_batch_json_recovers_from_response_json_failure_using_raw_text(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    raw_outer_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "pages": [
                                {
                                    "page": 1,
                                    "page_role": "body",
                                    "texts": [
                                        {
                                            "label": "section_header",
                                            "text": "Recovered heading",
                                            "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                                            "meta": {"level": 1},
                                        }
                                    ],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    raw_text = "```json\n" + json.dumps(raw_outer_response, ensure_ascii=False) + "\n```"
    inner_payload = {
        "pages": [
            {
                "page": 1,
                "page_role": "body",
                "texts": [
                    {
                        "label": "section_header",
                        "text": "Recovered heading",
                        "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                        "meta": {"level": 1},
                    }
                ],
            }
        ]
    }

    calls: list[str] = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not valid json")

        @property
        def text(self):
            return raw_text

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            del url, json
            return _Response()

    async def _fake_parse_json_dict_from_model_text(text: str):
        calls.append(text)
        if text == raw_text:
            return raw_outer_response
        if text == raw_outer_response["choices"][0]["message"]["content"]:
            return inner_payload
        raise AssertionError(f"unexpected parse input: {text}")

    monkeypatch.setattr("app.services.local_structured_pdf.ollama_page_parser.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.services.local_structured_pdf.ollama_page_parser.parse_json_dict_from_model_text",
        _fake_parse_json_dict_from_model_text,
    )

    result, error, preview = asyncio.run(
        parser._invoke_openai_compat_batch_json(
            base_url="http://host.docker.internal:11434",
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64s=["ZmFrZQ=="],
            use_response_format=True,
            max_output_tokens=1800,
        )
    )

    assert error == ""
    assert result == inner_payload
    assert calls == [raw_text, raw_outer_response["choices"][0]["message"]["content"]]
    assert "Recovered heading" in preview


def test_ollama_openai_batch_json_reports_specific_error_when_raw_text_cannot_be_recovered(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    raw_text = "```json\nnot recoverable\n```"

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not valid json")

        @property
        def text(self):
            return raw_text

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            del url, json
            return _Response()

    async def _fake_parse_json_dict_from_model_text(text: str):
        del text
        return None

    monkeypatch.setattr("app.services.local_structured_pdf.ollama_page_parser.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.services.local_structured_pdf.ollama_page_parser.parse_json_dict_from_model_text",
        _fake_parse_json_dict_from_model_text,
    )

    result, error, preview = asyncio.run(
        parser._invoke_openai_compat_batch_json(
            base_url="http://host.docker.internal:11434",
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64s=["ZmFrZQ=="],
            use_response_format=True,
            max_output_tokens=1800,
        )
    )

    assert result is None
    assert error.startswith("ollama_openai_batch_response_json_failed")
    assert "request_failed" not in error
    assert "not recoverable" in preview


def test_ollama_page_parser_rejects_dense_table_backend_without_table_structure(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[
            _resolved_line(line_id="l1", text="Table title", order=1, top=60.0, bottom=78.0),
            _resolved_line(line_id="l2", text="A  B  C", order=2, top=86.0, bottom=100.0),
        ],
        column_count=1,
    )
    triage = PdfHybridTriageResult(
        page=1,
        page_type="dense_table",
        decision="backend",
        confidence=0.99,
        reasons=["page_type:dense_table"],
        signals=PdfHybridTriageSignals(text_line_count=2),
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "img-1")

    async def _fake_invoke_ollama_json(
        *,
        model: str,
        user_prompt: str,
        image_b64: str,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ):
        del model, user_prompt, image_b64, use_response_format, max_output_tokens
        return {
            "page": 1,
            "page_role": "body",
            "blocks": [
                {
                    "kind": "heading",
                    "reading_order": 1,
                    "source_line_ids": ["l1"],
                },
                {
                    "kind": "paragraph",
                    "reading_order": 2,
                    "source_line_ids": ["l2"],
                    "table_rows": [],
                },
            ],
        }

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
            triage_result=triage,
        )
    )

    assert result.used is False
    assert result.error == "backend_result_insufficient:dense_table_missing_table_structure"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].reason == "dense_table_missing_table_structure"


def test_ollama_page_parser_batch_prompt_uses_page_briefs_instead_of_raw_input_skeleton():
    parser = LocalOllamaQwenVlPageParser()
    prompt = parser._build_batch_prompt_text(
        batch_prompt_payload={
            "pages": [
                {
                    "page": 1,
                    "page_width": 600.0,
                    "page_height": 800.0,
                    "column_count": 1,
                    "triage": {"page_type": "visual_or_scanned", "decision": "backend"},
                    "source_rows": [
                        {
                            "text": "and.org",
                            "band": "body",
                            "column_id": "main",
                            "region": "main",
                            "bbox": {"x0": 100.0, "top": 200.0, "x1": 180.0, "bottom": 220.0},
                        }
                    ],
                    "kind_whitelist": ["heading"],
                    "zone_whitelist": ["main"],
                },
                {
                    "page": 2,
                    "page_width": 600.0,
                    "page_height": 800.0,
                    "column_count": 2,
                    "triage": {"page_type": "mixed_layout", "decision": "backend"},
                    "source_rows": [
                        {
                            "text": "Introduction",
                            "band": "body",
                            "column_id": "main",
                            "region": "main",
                            "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                        }
                    ],
                    "kind_whitelist": ["heading"],
                    "zone_whitelist": ["main"],
                },
            ]
        },
        retry_hint="",
    )

    assert "page briefs" in prompt
    assert "page 1 | role_hint=visual_or_scanned | requested=none | anchors=0 | image_first=true" in prompt
    assert "page 2 | role_hint=mixed_layout | requested=none | anchors=1 | first_lines=Introduction" in prompt
    assert "page_width" not in prompt
    assert "page_height" not in prompt
    assert "triage" not in prompt
    assert "source_rows" not in prompt
    assert "kind_whitelist" not in prompt
    assert "zone_whitelist" not in prompt
    assert '"pages":[' in prompt


def test_ollama_page_parser_prefers_openai_compat_only_by_default(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "ollama_base_url", "http://host.docker.internal:11434")
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_enable_native_fallback", False)

    calls: list[str] = []

    async def _fake_openai(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        calls.append(f"openai:{model}:{base_url}")
        assert use_response_format is True
        assert max_output_tokens == 1800
        return {"page": 1, "page_role": "body", "blocks": []}, "", "{}"

    async def _fake_native(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        calls.append(f"native:{model}:{base_url}")
        return {"page": 1, "page_role": "body", "blocks": []}, "", "{}"

    monkeypatch.setattr(parser, "_invoke_openai_compat_json", _fake_openai)
    monkeypatch.setattr(parser, "_invoke_native_ollama_json", _fake_native)

    result = asyncio.run(
        parser._invoke_ollama_json(
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64="ZmFrZQ==",
        )
    )

    assert result == {"page": 1, "page_role": "body", "blocks": []}
    assert calls == ["openai:qwen3.5:0.8b:http://host.docker.internal:11434"]


def test_ollama_page_parser_retries_after_validation_failure(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1})(),
        lines=[
            _resolved_line(line_id="l1", text="Introduction", order=1),
            _resolved_line(line_id="l2", text="Body line", order=2, top=120.0, bottom=134.0),
        ],
        column_count=1,
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "ZmFrZQ==")

    responses = [
        {
            "page": 1,
            "page_role": "body",
            "blocks": [
                {
                    "kind": "paragraph",
                    "reading_order": 1,
                    "source_line_ids": ["missing"],
                }
            ],
        },
        {
            "page": 1,
            "page_role": "body",
            "blocks": [
                {
                    "kind": "heading",
                    "reading_order": 1,
                    "source_line_ids": ["l1"],
                },
                {
                    "kind": "paragraph",
                    "reading_order": 2,
                    "source_line_ids": ["l2"],
                },
            ],
        },
    ]

    async def _fake_invoke_ollama_json(
        *, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del model, image_b64
        assert use_response_format is True
        assert max_output_tokens == 1800
        return responses.pop(0)

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
        )
    )

    assert result.used is True
    assert result.retry_used is True
    assert result.retry_count == 1
    assert result.attempts[0].accepted is True
    assert [block.text for block in result.blocks] == ["Introduction", "Body line"]


def test_ollama_page_parser_materialize_blocks_does_not_infer_source_lines_from_bbox(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[],
        column_count=1,
    )
    payload = {
        "page": 1,
        "blocks": [
            {
                "block_id": "mm_p0001_b0001",
                "kind": "heading",
                "reading_order": 1,
                "text": "Introduction",
                "bbox": {"x0": 72.0, "top": 92.0, "x1": 240.0, "bottom": 116.0},
            }
        ],
    }
    blocks = parser._materialize_blocks(payload=payload, resolved_page=resolved_page, line_rows=[])
    assert [block.source_line_ids for block in blocks] == [[]]


def test_ollama_native_payload_explicitly_disables_thinking(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_disable_thinking", True)

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "{}"}}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return _Response()

    monkeypatch.setattr("app.services.local_structured_pdf.ollama_page_parser.httpx.AsyncClient", _Client)
    async def _fake_parse_json_dict_from_model_text(text: str):
        del text
        return {"page": 1, "page_role": "body", "blocks": []}

    monkeypatch.setattr(
        "app.services.local_structured_pdf.ollama_page_parser.parse_json_dict_from_model_text",
        _fake_parse_json_dict_from_model_text,
    )

    result, error, preview = asyncio.run(
        parser._invoke_native_ollama_json(
            base_url="http://host.docker.internal:11434",
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64="ZmFrZQ==",
            use_response_format=True,
        )
    )

    assert isinstance(result, dict)
    assert error == ""
    assert captured["url"] == "http://host.docker.internal:11434/api/chat"
    assert captured["json"]["think"] is False


def test_ollama_openai_payload_explicitly_disables_reasoning(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_disable_thinking", True)

    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "{\"page\":1,\"page_role\":\"body\",\"blocks\":[]}",
                        }
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return _Response()

    monkeypatch.setattr("app.services.local_structured_pdf.ollama_page_parser.httpx.AsyncClient", _Client)
    async def _fake_parse_json_dict_from_model_text(text: str):
        del text
        return {"page": 1, "page_role": "body", "blocks": []}

    monkeypatch.setattr(
        "app.services.local_structured_pdf.ollama_page_parser.parse_json_dict_from_model_text",
        _fake_parse_json_dict_from_model_text,
    )

    result, error, preview = asyncio.run(
        parser._invoke_openai_compat_json(
            base_url="http://host.docker.internal:11434",
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64="ZmFrZQ==",
            use_response_format=False,
        )
    )

    assert isinstance(result, dict)
    assert error == ""
    assert captured["url"] == "http://host.docker.internal:11434/v1/chat/completions"
    assert captured["json"]["reasoning_effort"] == "none"
    assert captured["json"]["reasoning"]["effort"] == "none"


def test_ollama_openai_uses_reasoning_when_content_is_empty(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning": '{"page":1,"page_role":"poster","texts":[]}',
                        }
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            return _Response()

    monkeypatch.setattr("app.services.local_structured_pdf.ollama_page_parser.httpx.AsyncClient", _Client)
    async def _fake_parse_json_dict_from_model_text(text: str):
        assert '"page_role":"poster"' in text
        return {"page": 1, "page_role": "poster", "texts": []}

    monkeypatch.setattr(
        "app.services.local_structured_pdf.ollama_page_parser.parse_json_dict_from_model_text",
        _fake_parse_json_dict_from_model_text,
    )

    result, error, preview = asyncio.run(
        parser._invoke_openai_compat_json(
            base_url="http://host.docker.internal:11434",
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64="ZmFrZQ==",
            use_response_format=False,
        )
    )

    assert isinstance(result, dict)
    assert error == ""
    assert '"page_role":"poster"' in preview


def test_ollama_prompt_payload_trims_visual_page_context():
    parser = LocalOllamaQwenVlPageParser(max_lines_per_page=80)
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[],
        column_count=1,
    )
    line_rows = [
        {
            "line_id": f"l{i}",
            "text": f"row-{i}-" + ("x" * 40),
            "band": "body",
            "column_id": "main",
            "region": "main",
            "reading_order": i,
            "bbox": {"x0": 80.0, "top": float(100 + i * 20), "x1": 320.0, "bottom": float(114 + i * 20)},
        }
        for i in range(1, 61)
    ]
    triage = PdfHybridTriageResult(
        page=1,
        page_type="visual_or_scanned",
        decision="backend",
        confidence=0.95,
        reasons=["page_type:visual_or_scanned"],
        signals=PdfHybridTriageSignals(text_line_count=60),
    )

    payload = parser._build_prompt_payload(
        resolved_page=resolved_page,
        line_rows=line_rows,
        triage_result=triage,
    )

    assert len(payload["source_rows"]) == 24
    assert len(payload["line_rows"]) == 24
    assert len(payload["source_text_full"]) <= 400


def test_ollama_page_parser_uses_page_type_specific_output_budgets():
    parser = LocalOllamaQwenVlPageParser()

    assert parser._max_output_tokens_for_page(prompt_payload={"triage": {"page_type": "visual_or_scanned"}}) == 3200
    assert parser._max_output_tokens_for_page(prompt_payload={"triage": {"page_type": "mixed_layout"}}) == 2400
    assert parser._max_output_tokens_for_page(prompt_payload={"triage": {"page_type": "front_matter_heavy"}}) == 2400
    assert parser._max_output_tokens_for_page(prompt_payload={"triage": {"page_type": "plain_text"}}) == 1800


def test_ollama_page_parser_output_budget_grows_for_qwen_task_hints():
    parser = LocalOllamaQwenVlPageParser()

    assert (
        parser._max_output_tokens_for_page(
            prompt_payload={
                "triage": {"page_type": "visual_or_scanned"},
                "task_hints": {
                    "force_ocr": True,
                    "enrich_formula": True,
                    "enrich_picture_description": True,
                },
            }
        )
        == 4200
    )
    assert (
        parser._max_output_tokens_for_page(
            prompt_payload={
                "triage": {"page_type": "plain_text"},
                "task_hints": {"enrich_formula": True},
            }
        )
        == 2100
    )


def test_ollama_visual_page_skips_openai_response_format(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "ollama_base_url", "http://host.docker.internal:11434")

    captured: dict[str, object] = {}

    async def _fake_openai(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        captured["use_response_format"] = use_response_format
        captured["max_output_tokens"] = max_output_tokens
        return {"page": 1, "page_role": "poster", "texts": []}, "", "{}"

    monkeypatch.setattr(parser, "_invoke_openai_compat_json", _fake_openai)
    monkeypatch.setattr(parser, "_invoke_native_ollama_json", _fake_openai)

    result = asyncio.run(
        parser._invoke_ollama_json(
            model="qwen3.5:0.8b",
            user_prompt="{}",
            image_b64="ZmFrZQ==",
            use_response_format=False,
        )
    )

    assert isinstance(result, dict)
    assert captured["use_response_format"] is False
    assert captured["max_output_tokens"] == 1800


def test_ollama_parser_uses_smaller_visual_page_image_limit():
    parser = LocalOllamaQwenVlPageParser(max_image_side=1600)
    visual = PdfHybridTriageResult(page=1, page_type="visual_or_scanned", decision="backend", confidence=0.9)
    mixed = PdfHybridTriageResult(page=1, page_type="mixed_layout", decision="backend", confidence=0.9)
    plain = PdfHybridTriageResult(page=1, page_type="plain_text", decision="local", confidence=0.9)

    assert parser._max_image_side_for_page(triage_result=visual) == 1024
    assert parser._max_image_side_for_page(triage_result=mixed) == 1280
    assert parser._max_image_side_for_page(triage_result=plain) == 1600


def test_ollama_page_parser_transcribe_ocr_uses_first_model_only_and_ocr_budget(monkeypatch):
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_ocr_max_image_side", 960)
    parser = LocalOllamaQwenVlPageParser(max_image_side=1600)
    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-ocr-primary", "qwen-ocr-fallback"])

    render_budgets: list[int] = []
    invoked_models: list[str] = []

    def _fake_render_page_image_base64(pdf_path: str, page: int, max_image_side: int | None = None) -> str:
        del pdf_path, page
        render_budgets.append(int(max_image_side or 0))
        return "img-ocr"

    async def _fake_invoke_ollama_text(
        *,
        model: str,
        user_prompt: str,
        image_b64: str,
        max_output_tokens: int = 220,
    ) -> str:
        del user_prompt, image_b64, max_output_tokens
        invoked_models.append(model)
        return "   "

    monkeypatch.setattr(parser, "_render_page_image_base64", _fake_render_page_image_base64)
    monkeypatch.setattr(parser, "_invoke_ollama_text", _fake_invoke_ollama_text)

    text, model = asyncio.run(
        parser.transcribe_page_text(
            pdf_path="/tmp/demo.pdf",
            page=1,
        )
    )

    assert text == ""
    assert model == ""
    assert render_budgets == [960]
    assert invoked_models == ["qwen-ocr-primary"]


def test_ollama_page_parser_transcribe_ocr_caps_visual_or_scanned_budget(monkeypatch):
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_ocr_max_image_side", 960)
    parser = LocalOllamaQwenVlPageParser(max_image_side=1600)
    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-ocr-primary"])

    render_budgets: list[int] = []

    def _fake_render_page_image_base64(pdf_path: str, page: int, max_image_side: int | None = None) -> str:
        del pdf_path, page
        render_budgets.append(int(max_image_side or 0))
        return "img-ocr"

    async def _fake_invoke_ollama_text(
        *,
        model: str,
        user_prompt: str,
        image_b64: str,
        max_output_tokens: int = 220,
    ) -> str:
        del model, user_prompt, image_b64, max_output_tokens
        return "Recovered OCR text"

    monkeypatch.setattr(parser, "_render_page_image_base64", _fake_render_page_image_base64)
    monkeypatch.setattr(parser, "_invoke_ollama_text", _fake_invoke_ollama_text)

    text, model = asyncio.run(
        parser.transcribe_page_text(
            pdf_path="/tmp/demo.pdf",
            page=1,
            page_type="visual_or_scanned",
        )
    )

    assert text == "Recovered OCR text"
    assert model == "qwen-ocr-primary"
    assert render_budgets == [768]


def test_ollama_page_parser_picture_formula_image_budgets_are_unchanged_by_ocr_budget(monkeypatch):
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_ocr_max_image_side", 896)
    parser = LocalOllamaQwenVlPageParser(max_image_side=1600)
    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])

    render_budgets: list[int] = []
    bbox = PdfBBox(x0=80.0, top=96.0, x1=240.0, bottom=180.0)

    def _fake_render_region_image_base64(
        pdf_path: str,
        page: int,
        render_bbox: PdfBBox,
        max_image_side: int | None = None,
    ) -> str:
        del pdf_path, page, render_bbox
        render_budgets.append(int(max_image_side or 0))
        return "img-region"

    async def _fake_invoke_ollama_text(
        *,
        model: str,
        user_prompt: str,
        image_b64: str,
        max_output_tokens: int = 220,
    ) -> str:
        del model, user_prompt, image_b64
        if max_output_tokens == 220:
            return "A bar chart."
        return "E = mc^2"

    monkeypatch.setattr(parser, "_render_region_image_base64", _fake_render_region_image_base64)
    monkeypatch.setattr(parser, "_invoke_ollama_text", _fake_invoke_ollama_text)

    picture_text, _ = asyncio.run(
        parser.describe_picture_region(
            pdf_path="/tmp/demo.pdf",
            page=1,
            bbox=bbox,
        )
    )
    formula_text, _ = asyncio.run(
        parser.describe_formula_region(
            pdf_path="/tmp/demo.pdf",
            page=1,
            bbox=bbox,
        )
    )

    assert picture_text == "A bar chart."
    assert formula_text == "E = mc^2"
    assert render_budgets == [1024, 768]


def test_ollama_page_parser_transforms_loose_elements_to_blocks(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1})(),
        lines=[
            _resolved_line(line_id="l1", text="Abstract", order=1),
            _resolved_line(line_id="l2", text="First sentence.", order=2, top=120.0, bottom=134.0),
            _resolved_line(line_id="l3", text="Second sentence.", order=3, top=138.0, bottom=152.0),
        ],
        column_count=1,
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "ZmFrZQ==")

    async def _fake_invoke_ollama_json(
        *, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del model, user_prompt, image_b64
        assert use_response_format is True
        assert max_output_tokens == 1800
        parser._last_invoke_protocol = "native"
        parser._last_raw_response_preview = '{"elements":[{"type":"section_header","line_ids":["l1"]}]}'
        return {
            "page": 1,
            "page_role": "body",
            "elements": [
                {
                    "type": "section_header",
                    "line_ids": ["l1"],
                },
                {
                    "label": "text",
                    "line_ids": ["l2", "l3"],
                },
            ],
            "notes": ["loose-shape"],
        }

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
        )
    )

    assert result.used is True
    assert result.protocol == "native"
    assert "elements" in result.raw_response_preview
    assert result.attempts[0].accepted is True
    assert [block.kind for block in result.blocks] == ["heading", "paragraph"]
    assert result.blocks[0].text == "Abstract"
    assert result.blocks[1].text == "First sentence. Second sentence."


def test_ollama_page_parser_allows_visual_page_ocr_text_without_line_ids(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[
            _resolved_line(line_id="l1", text="tiny residual", order=1, top=760.0, bottom=772.0, x0=20.0, x1=80.0),
        ],
        column_count=1,
    )
    triage = PdfHybridTriageResult(
        page=1,
        page_type="visual_or_scanned",
        decision="backend",
        confidence=0.95,
        reasons=["page_type:visual_or_scanned"],
        signals=PdfHybridTriageSignals(text_line_count=1, image_count=1),
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "ZmFrZQ==")

    async def _fake_invoke_ollama_json(
        *, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del model, user_prompt, image_b64
        assert use_response_format is False
        assert max_output_tokens == 3200
        return {
            "page": 1,
            "page_role": "poster",
            "elements": [
                {
                    "type": "title",
                    "text": "REAL TITLE FROM OCR",
                    "bbox": {"x0": 100.0, "top": 80.0, "x1": 520.0, "bottom": 150.0},
                }
            ],
        }

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
            triage_result=triage,
        )
    )

    assert result.used is True
    assert len(result.blocks) == 1
    assert result.attempts[0].accepted is True
    assert result.attempts[0].reason == "has_unanchored_ocr_blocks"
    assert result.blocks[0].source_line_ids == []
    assert result.blocks[0].text == "REAL TITLE FROM OCR"


def test_ollama_page_parser_rejects_visual_page_redundant_heading_paragraph_pair(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[
            _resolved_line(line_id="l1", text="Poster headline and body", order=1, top=82.0, bottom=124.0),
            _resolved_line(line_id="l2", text="Poster headline and body", order=2, top=126.0, bottom=168.0),
        ],
        column_count=1,
    )
    triage = PdfHybridTriageResult(
        page=1,
        page_type="visual_or_scanned",
        decision="backend",
        confidence=0.95,
        reasons=["page_type:visual_or_scanned"],
        signals=PdfHybridTriageSignals(text_line_count=2, image_count=1),
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-vl-local"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "ZmFrZQ==")

    async def _fake_invoke_ollama_json(
        *, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del model, user_prompt, image_b64
        assert use_response_format is False
        assert max_output_tokens == 3200
        return {
            "page": 1,
            "page_role": "poster",
            "texts": [
                {
                    "label": "section_header",
                    "text": "Poster headline and body",
                    "bbox": {"x0": 96.0, "top": 84.0, "x1": 520.0, "bottom": 132.0},
                },
                {
                    "label": "text",
                    "text": "Poster headline and body",
                    "bbox": {"x0": 100.0, "top": 88.0, "x1": 516.0, "bottom": 136.0},
                },
            ],
        }

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
            triage_result=triage,
        )
    )

    assert result.used is False
    assert result.error == "backend_result_insufficient:visual_page_redundant_heading_paragraph_pair"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].reason == "visual_page_redundant_heading_paragraph_pair"


def test_ollama_page_parser_escalates_model_chain_when_small_model_result_is_insufficient(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[
            _resolved_line(line_id="l1", text="tiny residual", order=1, top=760.0, bottom=772.0, x0=20.0, x1=80.0),
        ],
        column_count=1,
    )
    triage = PdfHybridTriageResult(
        page=1,
        page_type="visual_or_scanned",
        decision="backend",
        confidence=0.95,
        reasons=["page_type:visual_or_scanned"],
        signals=PdfHybridTriageSignals(text_line_count=1, image_count=1),
    )

    monkeypatch.setattr(parser, "_resolved_models", lambda: ["qwen-0.8b", "qwen-2b"])
    monkeypatch.setattr(parser, "_render_page_image_base64", lambda *args, **kwargs: "ZmFrZQ==")

    async def _fake_invoke_ollama_json(
        *, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del user_prompt, image_b64
        if model == "qwen-0.8b":
            assert use_response_format is False
            assert max_output_tokens == 3200
            return {
                "page": 1,
                "page_role": "body",
                "elements": [
                    {
                        "type": "section_header",
                        "line_ids": ["l1"],
                    }
                ],
            }
        return {
            "page": 1,
            "page_role": "poster",
            "elements": [
                {
                    "type": "title",
                    "text": "REAL TITLE FROM OCR",
                    "bbox": {"x0": 100.0, "top": 80.0, "x1": 520.0, "bottom": 150.0},
                }
            ],
        }

    monkeypatch.setattr(parser, "_invoke_ollama_json", _fake_invoke_ollama_json)

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
            triage_result=triage,
        )
    )

    assert result.used is True
    assert result.model == "qwen-2b"
    assert result.attempted_models == ["qwen-0.8b", "qwen-2b"]
    assert len(result.attempts) == 2
    assert result.attempts[0].accepted is False
    assert result.attempts[0].reason == "visual_page_still_only_residual_text"
    assert result.attempts[1].accepted is True
    assert result.attempts[1].reason == "has_unanchored_ocr_blocks"
    assert result.blocks[0].text == "REAL TITLE FROM OCR"


def test_ollama_page_parser_visual_prompt_prefers_text_over_many_titles():
    parser = LocalOllamaQwenVlPageParser()
    prompt = parser._build_prompt_text(
        prompt_payload={
            "page": 1,
            "page_width": 600.0,
            "page_height": 800.0,
            "column_count": 1,
            "source_checksum": "",
            "source_text_full": "tiny residual",
            "triage": {"page_type": "visual_or_scanned", "decision": "backend", "confidence": 0.95},
            "line_rows": [
                {
                    "line_id": "l1",
                    "text": "tiny residual",
                    "band": "body",
                    "column_id": "main",
                    "reading_order": 1,
                }
            ],
        },
        retry_hint="",
    )

    assert "docling-like loose structure" in prompt
    assert "Large sentence-like statements, slogans, or explanatory claims should use label=text" in prompt
    assert "Avoid returning many section_header elements on a single poster-like page" in prompt
    assert "Return a small number of large regions." in prompt
    assert "Do not wrap the JSON in markdown fences." in prompt
    assert "Residual text hint" not in prompt
    assert "Allowed labels: section_header, text, caption, footnote, list_item, formula, page_header, page_footer, table, picture, unknown." in prompt
    assert "source_rows" not in prompt
    assert "source_checksum" not in prompt
    assert "page_width" not in prompt
    assert "triage:" not in prompt
    assert "line_rows" not in prompt
    assert "line_ids are optional" not in prompt


def test_ollama_page_parser_visual_prompt_mentions_qwen_task_hints():
    parser = LocalOllamaQwenVlPageParser()
    prompt = parser._build_prompt_text(
        prompt_payload={
            "page": 1,
            "triage": {"page_type": "visual_or_scanned", "decision": "backend", "confidence": 0.95},
            "task_hints": {
                "force_ocr": True,
                "enrich_formula": True,
                "enrich_picture_description": True,
                "picture_description_prompt": "Describe the chart in plain English",
            },
        },
        retry_hint="",
    )

    assert "OCR is explicitly requested for this page." in prompt
    assert "Formula enrichment is requested." in prompt
    assert "Picture description is requested." in prompt
    assert 'Picture description prompt: "Describe the chart in plain English"' in prompt


def test_ollama_page_parser_non_visual_prompt_keeps_source_rows_but_drops_internal_metadata():
    parser = LocalOllamaQwenVlPageParser()
    prompt = parser._build_prompt_text(
        prompt_payload={
            "page": 1,
            "page_width": 600.0,
            "page_height": 800.0,
            "column_count": 2,
            "source_checksum": "abc",
            "source_text_full": "Introduction\nBody",
            "triage": {"page_type": "mixed_layout", "decision": "backend", "confidence": 0.82},
            "source_rows": [
                {
                    "text": "Introduction",
                    "band": "body",
                    "column_id": "main",
                    "region": "main",
                    "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
                }
            ],
        },
        retry_hint="",
    )

    assert "source_rows" in prompt
    assert "Do not wrap the JSON in markdown fences." in prompt
    assert "Allowed labels: section_header, text, caption, footnote, list_item, formula, page_header, page_footer, table, picture, unknown." in prompt
    assert "source_checksum" not in prompt
    assert "page_width" not in prompt
    assert "page_height" not in prompt
    assert "column_count" not in prompt
    assert "triage:" not in prompt


def test_ollama_page_parser_prompt_payload_normalizes_task_hints():
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1, "page_width": 600.0, "page_height": 800.0})(),
        lines=[_resolved_line(line_id="l1", text="Body", order=1)],
        column_count=1,
    )
    payload = parser._build_prompt_payload(
        resolved_page=resolved_page,
        line_rows=[
            {
                "line_id": "l1",
                "text": "Body",
                "band": "body",
                "column_id": "main",
                "region": "main",
                "reading_order": 1,
                "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 116.0},
            }
        ],
        triage_result=PdfHybridTriageResult(page=1, page_type="mixed_layout", decision="backend", confidence=0.8),
        task_hints={
            "force_ocr": 1,
            "enrich_formula": "yes",
            "enrich_picture_description": True,
            "picture_description_prompt": "x" * 450,
            "ignored": "value",
        },
    )

    assert payload["task_hints"] == {
        "force_ocr": True,
        "enrich_formula": True,
        "enrich_picture_description": True,
        "picture_description_prompt": "x" * 400,
    }


def test_ollama_page_parser_uses_default_qwen_chain_when_settings_are_blank(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_model_chain", "")
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_model", "")

    assert parser._resolved_models() == [
        "qwen3.5:0.8b",
        "qwen3.5:2b-q4_K_M",
        "qwen3.5:4b-q4_K_M",
    ]


def test_ollama_page_parser_visual_batch_payload_drops_residual_text_rows():
    parser = LocalOllamaQwenVlPageParser()
    payload = parser._sanitize_batch_prompt_payload(
        {
            "page": 1,
            "page_width": 1728.0,
            "page_height": 2592.0,
            "column_count": 1,
            "source_checksum": "abc",
            "source_text_full": "and.org",
            "triage": {"page_type": "visual_or_scanned", "decision": "backend", "confidence": 0.9},
            "source_rows": [
                {
                    "text": "and.org",
                    "band": "body",
                    "column_id": "main",
                    "region": "main",
                    "bbox": {"x0": 100.0, "top": 200.0, "x1": 180.0, "bottom": 220.0},
                }
            ],
        }
    )

    assert payload["source_rows"] == []
    assert "source_checksum" not in payload
    assert "source_text_full" not in payload


def test_ollama_page_parser_returns_error_when_model_missing(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    resolved_page = PdfResolvedPage(
        meta=type("Meta", (), {"page": 1})(),
        lines=[_resolved_line(line_id="l1", text="Introduction", order=1)],
        column_count=1,
    )
    monkeypatch.setattr(parser, "_resolved_models", lambda: [])

    result = asyncio.run(
        parser.parse_page(
            pdf_path="/tmp/demo.pdf",
            resolved_page=resolved_page,
        )
    )

    assert result.used is False
    assert result.error == "ollama_model_missing"


def test_ollama_page_parser_falls_back_to_native_only_when_enabled(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "ollama_base_url", "http://host.docker.internal:11434")
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_enable_native_fallback", True)
    calls: list[str] = []

    async def _fake_openai(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del base_url, model, user_prompt, image_b64
        calls.append("openai")
        return None, "ollama_openai_http_404", ""

    async def _fake_native(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del base_url, model, user_prompt, image_b64
        calls.append("native")
        return {"page": 1, "page_role": "body", "blocks": []}, "", '{"blocks":[]}'

    monkeypatch.setattr(parser, "_invoke_openai_compat_json", _fake_openai)
    monkeypatch.setattr(parser, "_invoke_native_ollama_json", _fake_native)

    result = asyncio.run(
        parser._invoke_ollama_json(
            model="qwen-vl-local",
            user_prompt="hello",
            image_b64="ZmFrZQ==",
        )
    )

    assert calls == ["openai", "native"]
    assert isinstance(result, dict)
    assert parser._last_invoke_error == ""


def test_ollama_page_parser_preserves_last_error_when_all_protocols_fail(monkeypatch):
    parser = LocalOllamaQwenVlPageParser()
    monkeypatch.setattr(settings, "ollama_base_url", "http://host.docker.internal:11434")
    monkeypatch.setattr(settings, "local_structured_pdf_hybrid_enable_native_fallback", True)

    async def _fake_openai(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del base_url, model, user_prompt, image_b64
        return None, "ollama_openai_http_500", '{"error":"boom"}'

    async def _fake_native(
        *, base_url: str, model: str, user_prompt: str, image_b64: str, use_response_format: bool = True, max_output_tokens: int = 1800
    ):
        del base_url, model, user_prompt, image_b64
        return None, "ollama_native_http_404", ""

    monkeypatch.setattr(parser, "_invoke_openai_compat_json", _fake_openai)
    monkeypatch.setattr(parser, "_invoke_native_ollama_json", _fake_native)

    result = asyncio.run(
        parser._invoke_ollama_json(
            model="qwen-vl-local",
            user_prompt="hello",
            image_b64="ZmFrZQ==",
        )
    )

    assert result is None
    assert parser._last_invoke_error == "ollama_native_http_404"
    assert parser._last_invoke_protocol == "openai_compat"
    assert "boom" in parser._last_raw_response_preview
