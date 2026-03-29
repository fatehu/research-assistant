from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.local_structured_pdf.contracts import PdfBBox, PdfPageMeta, PdfSemanticBlock, PdfStructuredDocument, PdfStructuredPage
from app.services.local_structured_pdf import opendataloader_compat_server as compat_server
from app.services.local_structured_pdf.picture_enrichment_service import PdfPictureDescription


def _block(
    *,
    block_id: str,
    block_type: str,
    text: str,
    page: int = 1,
    x0: float = 40.0,
    top: float = 40.0,
    x1: float = 200.0,
    bottom: float = 60.0,
    heading_level: int | None = None,
    table_rows: list[list[str]] | None = None,
) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=block_id,
        block_type=block_type,
        page_start=page,
        page_end=page,
        text=text,
        bbox=PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom),
        line_ids=[],
        heading_level=heading_level,
        table_rows=list(table_rows or []),
    )


def test_structured_document_to_docling_json_maps_heading_table_and_picture() -> None:
    document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                blocks=[
                    _block(block_id="h1", block_type="heading", text="Introduction", heading_level=2),
                    _block(block_id="p1", block_type="paragraph", text="Body text"),
                    _block(
                        block_id="t1",
                        block_type="table",
                        text="",
                        table_rows=[["Metric", "Value"], ["A", "1"]],
                    ),
                    _block(block_id="f1", block_type="figure_meta", text="Chart showing growth"),
                ],
            )
        ]
    )

    payload = compat_server.structured_document_to_docling_json(document)

    assert payload["pages"]["1"]["size"] == {"width": 595.0, "height": 842.0}
    assert payload["texts"][0]["label"] == "section_header"
    assert payload["texts"][0]["meta"]["level"] == 2
    assert payload["texts"][1]["label"] == "text"
    assert payload["tables"][0]["data"]["table_cells"][0]["text"] == "Metric"
    assert payload["pictures"][0]["annotations"][0]["kind"] == "description"
    assert payload["pages"]["1"]["page_no"] == 1


def test_structured_document_to_docling_json_merges_external_picture_descriptions() -> None:
    document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                blocks=[
                    _block(block_id="p1", block_type="paragraph", text="Body text"),
                ],
            )
        ]
    )

    payload = compat_server.structured_document_to_docling_json(
        document,
        picture_descriptions=[
            PdfPictureDescription(
                page=1,
                bbox=PdfBBox(x0=100.0, top=200.0, x1=300.0, bottom=420.0),
                description="Chart showing growth over time",
                model="qwen3.5:0.8b",
            )
        ],
    )

    assert payload["pictures"][0]["annotations"][0]["text"] == "Chart showing growth over time"
    assert payload["pictures"][0]["prov"][0]["page_no"] == 1


def test_build_conversion_response_keeps_failed_pages() -> None:
    response = compat_server.build_conversion_response(
        status_value="partial_success",
        json_content={"pages": {}},
        processing_time=1.2,
        errors=["bad page"],
        requested_pages=(1, 3),
        total_pages=3,
    )
    assert response["status"] == "partial_success"
    assert response["failed_pages"] == [1, 2, 3]
    assert response["document"]["json_content"] == {"pages": {}}


def test_create_app_exposes_upstream_compatible_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    testclient = pytest.importorskip("fastapi.testclient")

    class _FakeConverter:
        def convert(self, pdf_path: str, page_range=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_range
            return compat_server._CompatConversionResult(
                status=compat_server._CompatConversionStatus.SUCCESS,
                document=compat_server._CompatDocument(json_content={"pages": {"1": {"page_no": 1}}}),
                errors=[],
                input=compat_server._CompatInput(page_count=1),
            )

    monkeypatch.setattr(compat_server, "create_converter", lambda **kwargs: _FakeConverter())
    app = compat_server.create_app(
        force_ocr=False,
        ocr_lang=None,
        enrich_formula=False,
        enrich_picture_description=False,
        picture_description_prompt=None,
    )
    with testclient.TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        convert = client.post(
            "/v1/convert/file",
            files={"files": ("sample.pdf", b"%PDF-1.4 test", "application/pdf")},
        )
        assert convert.status_code == 200
        payload = convert.json()
        assert payload["status"] == "success"
        assert payload["document"]["json_content"]["pages"]["1"]["page_no"] == 1


def test_compat_backend_runs_base_pipeline_for_document_parse() -> None:
    captured: dict[str, object] = {}

    class _FakePipeline:
        def parse_document(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            captured["pdf_path"] = pdf_path
            captured["page_limit"] = page_limit
            return PdfStructuredDocument(
                pages=[
                    PdfStructuredPage(
                        meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                        blocks=[_block(block_id="p1", block_type="paragraph", text="Recovered text")],
                    )
                ]
            )

    backend = compat_server.LocalStructuredPdfDoclingCompatBackend(
        pipeline=_FakePipeline(),
    )

    document = asyncio.run(
        backend._parse_document(
            pdf_path="/tmp/sample.pdf",
            force_ocr=True,
            ocr_lang=["en", "fr"],
            enrich_formula=True,
            enrich_picture_description=True,
            picture_description_prompt="Describe the chart",
        )
    )

    assert len(document.pages) == 1
    assert captured == {
        "pdf_path": "/tmp/sample.pdf",
        "page_limit": None,
    }


def test_compat_backend_runs_picture_enrichment_stage_when_enabled(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakePipeline:
        def parse_document(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            return PdfStructuredDocument(
                pages=[
                    PdfStructuredPage(
                        meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                        blocks=[_block(block_id="p1", block_type="paragraph", text="Recovered text")],
                    )
                ]
            )

    class _FakePictureEnrichmentService:
        async def enrich_document(self, *, pdf_path: str, document: PdfStructuredDocument, picture_description_prompt: str | None = None):  # type: ignore[no-untyped-def]
            captured["pdf_path"] = pdf_path
            captured["document_page_count"] = len(list(document.pages or []))
            captured["picture_description_prompt"] = picture_description_prompt
            return [
                PdfPictureDescription(
                    page=1,
                    bbox=PdfBBox(x0=120.0, top=220.0, x1=320.0, bottom=420.0),
                    description="Bar chart with three categories",
                    model="qwen3.5:0.8b",
                )
            ]

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")

    backend = compat_server.LocalStructuredPdfDoclingCompatBackend(
        enrich_picture_description=True,
        picture_description_prompt="Describe the image",
        pipeline=_FakePipeline(),
        picture_enrichment_service=_FakePictureEnrichmentService(),
    )

    result = backend.convert(str(pdf_path))

    assert result.status == compat_server._CompatConversionStatus.SUCCESS
    payload = result.document.json_content
    assert payload["pictures"][0]["annotations"][0]["text"] == "Bar chart with three categories"
    assert captured == {
        "pdf_path": str(pdf_path),
        "document_page_count": 1,
        "picture_description_prompt": "Describe the image",
    }


def test_compat_backend_runs_formula_enrichment_stage_when_enabled(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakePipeline:
        def parse_document(self, *, pdf_path: str, page_limit=None):  # type: ignore[no-untyped-def]
            del pdf_path, page_limit
            eq = _block(block_id="eq1", block_type="equation", text="raw")
            return PdfStructuredDocument(
                pages=[
                    PdfStructuredPage(
                        meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                        blocks=[eq],
                    )
                ],
                blocks=[eq],
            )

    class _FakeFormulaEnrichmentService:
        async def enrich_document(self, *, pdf_path: str, document: PdfStructuredDocument):  # type: ignore[no-untyped-def]
            captured["pdf_path"] = pdf_path
            captured["document_page_count"] = len(list(document.pages or []))
            eq = _block(block_id="eq1", block_type="equation", text=r"E=mc^2")
            return PdfStructuredDocument(
                pages=[
                    PdfStructuredPage(
                        meta=PdfPageMeta(page=1, page_width=595.0, page_height=842.0),
                        blocks=[eq],
                    )
                ],
                blocks=[eq],
            )

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")

    backend = compat_server.LocalStructuredPdfDoclingCompatBackend(
        enrich_formula=True,
        pipeline=_FakePipeline(),
        formula_enrichment_service=_FakeFormulaEnrichmentService(),
    )

    result = backend.convert(str(pdf_path))

    assert result.status == compat_server._CompatConversionStatus.SUCCESS
    payload = result.document.json_content
    assert payload["texts"][0]["label"] == "formula"
    assert payload["texts"][0]["text"] == r"E=mc^2"
    assert captured == {
        "pdf_path": str(pdf_path),
        "document_page_count": 1,
    }
