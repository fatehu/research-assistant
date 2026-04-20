from __future__ import annotations

import asyncio

from app.services.local_structured_pdf import (
    LocalStructuredPdfHybridPipeline,
    PdfBBox,
    PdfHybridParsedBlock,
    PdfHybridParsedPage,
    PdfHybridTriageDocument,
    PdfHybridTriageResult,
    PdfHybridTriageSignals,
    PdfNormalizedPage,
    PdfPageAtoms,
    PdfPageMeta,
    PdfResolvedDocument,
    PdfResolvedLine,
    PdfResolvedPage,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


def _meta(page: int) -> PdfPageMeta:
    return PdfPageMeta(page=page, page_width=600.0, page_height=800.0, rotation=0)


def _line(page: int, line_id: str, text: str, order: int) -> PdfResolvedLine:
    return PdfResolvedLine(
        line_id=line_id,
        page=page,
        text=text,
        bbox=PdfBBox(x0=80.0, top=100.0 + order * 18.0, x1=320.0, bottom=114.0 + order * 18.0),
        word_ids=[f"{line_id}:w1"],
        avg_font_size=12.0,
        dominant_font_name="Times",
        band="body",
        region="main",
        column_id="main",
        reading_order=order,
    )


class _Extractor:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def extract_document_atoms(self, *, pdf_path: str, page_limit: int | None = None) -> list[PdfPageAtoms]:
        self._calls.append(f"extract:{pdf_path}:{page_limit}")
        return [
            PdfPageAtoms(meta=_meta(1)),
            PdfPageAtoms(meta=_meta(2)),
        ]


class _Normalizer:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def normalize_page(self, *, page_atoms: PdfPageAtoms) -> PdfNormalizedPage:
        self._calls.append(f"normalize:{page_atoms.page}")
        return PdfNormalizedPage(meta=page_atoms.meta)


class _DocumentResolver:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def resolve_document(self, *, pages: list[PdfNormalizedPage]) -> PdfResolvedDocument:
        self._calls.append(f"resolve:{len(pages)}")
        return PdfResolvedDocument(
            pages=[
                PdfResolvedPage(meta=_meta(1), lines=[_line(1, "p1_l1", "Local page", 1)], column_count=1),
                PdfResolvedPage(meta=_meta(2), lines=[_line(2, "p2_l1", "Backend page", 1)], column_count=1),
            ]
        )


class _BlockBuilder:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def build_document(self, *, document: PdfResolvedDocument) -> PdfStructuredDocument:
        self._calls.append("build")
        return PdfStructuredDocument(
            pages=[
                PdfStructuredPage(
                    meta=_meta(1),
                    blocks=[
                        PdfSemanticBlock(
                            block_id="local_p1_b1",
                            block_type="paragraph",
                            page_start=1,
                            page_end=1,
                            text="Local page",
                            bbox=PdfBBox(x0=80.0, top=118.0, x1=320.0, bottom=132.0),
                            line_ids=["p1_l1"],
                            reading_order_start=1,
                            reading_order_end=1,
                        )
                    ],
                ),
                PdfStructuredPage(
                    meta=_meta(2),
                    blocks=[
                        PdfSemanticBlock(
                            block_id="local_p2_b1",
                            block_type="paragraph",
                            page_start=2,
                            page_end=2,
                            text="Backend page",
                            bbox=PdfBBox(x0=80.0, top=118.0, x1=320.0, bottom=132.0),
                            line_ids=["p2_l1"],
                            reading_order_start=1,
                            reading_order_end=1,
                        )
                    ],
                ),
            ],
            body_font_size=12.0,
        )


class _TableDetector:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def detect_document(
        self,
        *,
        page_atoms=None,
        normalized_pages=None,
        resolved_document=None,
        structured_document: PdfStructuredDocument,
    ) -> PdfStructuredDocument:
        self._calls.append("table")
        return structured_document


class _TriageService:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def triage_document(
        self,
        *,
        page_atoms,
        normalized_pages,
        resolved_document,
        structured_document,
        mode: str = "auto",
    ) -> PdfHybridTriageDocument:
        self._calls.append(f"triage:{mode}")
        return PdfHybridTriageDocument(
            mode=mode,
            pages=[
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
            ],
        )


class _PageParser:
    def __init__(self, calls: list[str], *, configured: bool = True, used: bool = True) -> None:
        self._calls = calls
        self._configured = configured
        self._used = used

    def is_configured(self) -> bool:
        return self._configured

    async def parse_pages(
        self,
        *,
        pdf_path: str,
        resolved_pages,
        triage_results,
    ):
        page_numbers = [int(page.page) for page in list(resolved_pages or [])]
        self._calls.append(f"parse_pages:{pdf_path}:{page_numbers}")
        result: list[PdfHybridParsedPage] = []
        triage_map = {
            int(item.page): item
            for item in list(triage_results or [])
            if item is not None
        }
        for resolved_page in list(resolved_pages or []):
            triage_result = triage_map.get(int(resolved_page.page))
            result.append(
                PdfHybridParsedPage(
                    page=resolved_page.page,
                    model="qwen-vl-local",
                    page_role="body",
                    used=self._used,
                    error="" if self._used else "backend_parse_failed",
                    blocks=[
                        PdfHybridParsedBlock(
                            block_id=f"mm_p{int(resolved_page.page):04d}_b0001",
                            kind="paragraph",
                            page=resolved_page.page,
                            reading_order=1,
                            text=f"Backend page improved {int(resolved_page.page)}",
                            bbox=PdfBBox(x0=80.0, top=118.0, x1=320.0, bottom=132.0),
                            source_line_ids=[f"p{int(resolved_page.page)}_l1"],
                            zone="main",
                            merge_strategy="space",
                            confidence=0.91,
                        )
                    ]
                    if self._used
                    else [],
                    notes=[str(triage_result.page_type)] if triage_result is not None else [],
                )
            )
        return result

    async def parse_page(self, *, pdf_path: str, resolved_page: PdfResolvedPage, triage_result: PdfHybridTriageResult):
        self._calls.append(f"parse:{pdf_path}:{resolved_page.page}:{triage_result.page_type}")
        return PdfHybridParsedPage(
            page=resolved_page.page,
            model="qwen-vl-local",
            page_role="body",
            used=self._used,
            error="" if self._used else "backend_parse_failed",
            blocks=[
                PdfHybridParsedBlock(
                    block_id="mm_p0002_b0001",
                    kind="paragraph",
                    page=resolved_page.page,
                    reading_order=1,
                    text="Backend page improved",
                    bbox=PdfBBox(x0=80.0, top=118.0, x1=320.0, bottom=132.0),
                    source_line_ids=["p2_l1"],
                    zone="main",
                    merge_strategy="space",
                    confidence=0.91,
                )
            ]
            if self._used
            else [],
        )


class _FusionService:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def fuse_document(
        self,
        *,
        resolved_document,
        local_document,
        triage_document,
        parsed_pages,
    ) -> PdfStructuredDocument:
        del resolved_document, triage_document
        self._calls.append(f"fuse:{len(parsed_pages)}")
        if parsed_pages and parsed_pages[0].used:
            return PdfStructuredDocument(
                pages=[
                    local_document.pages[0],
                    PdfStructuredPage(
                        meta=_meta(2),
                        blocks=[
                            PdfSemanticBlock(
                                block_id="mm_p0002_b0001",
                                block_type="paragraph",
                                page_start=2,
                                page_end=2,
                                text="Backend page improved",
                                bbox=PdfBBox(x0=80.0, top=118.0, x1=320.0, bottom=132.0),
                                line_ids=["p2_l1"],
                                reading_order_start=1,
                                reading_order_end=1,
                            )
                        ],
                    ),
                ],
                blocks=[
                    local_document.pages[0].blocks[0],
                    PdfSemanticBlock(
                        block_id="mm_p0002_b0001",
                        block_type="paragraph",
                        page_start=2,
                        page_end=2,
                        text="Backend page improved",
                        bbox=PdfBBox(x0=80.0, top=118.0, x1=320.0, bottom=132.0),
                        line_ids=["p2_l1"],
                        reading_order_start=1,
                        reading_order_end=1,
                    ),
                ],
                body_font_size=12.0,
            )
        return local_document


class _Resolver:
    def __init__(self, calls: list[str], name: str) -> None:
        self._calls = calls
        self._name = name

    def resolve_document(self, *, document: PdfStructuredDocument) -> PdfStructuredDocument:
        self._calls.append(self._name)
        return document


def test_hybrid_pipeline_routes_only_backend_pages_and_fuses_result():
    calls: list[str] = []
    pipeline = LocalStructuredPdfHybridPipeline(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
        page_parser=_PageParser(calls, configured=True, used=True),
        fusion_service=_FusionService(calls),
        block_role_resolver=_Resolver(calls, "role"),
        section_resolver=_Resolver(calls, "section"),
        heuristic_profile="structural",
    )

    result = asyncio.run(
        pipeline.parse_document_with_trace(
            pdf_path="/tmp/demo.pdf",
            page_limit=2,
            mode="auto",
        )
    )

    assert result.backend_attempted_pages == [2]
    assert result.backend_used_pages == [2]
    assert result.backend_fallback_pages == []
    assert result.document.pages[1].blocks[0].text == "Backend page improved"
    assert calls == [
        "extract:/tmp/demo.pdf:2",
        "normalize:1",
        "normalize:2",
        "resolve:2",
        "build",
        "table",
        "triage:auto",
        "parse_pages:/tmp/demo.pdf:[2]",
        "fuse:1",
        "role",
        "section",
    ]


def test_hybrid_pipeline_records_fallback_when_backend_parser_disabled():
    calls: list[str] = []
    pipeline = LocalStructuredPdfHybridPipeline(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
        page_parser=_PageParser(calls, configured=False, used=False),
        fusion_service=_FusionService(calls),
        block_role_resolver=_Resolver(calls, "role"),
        section_resolver=_Resolver(calls, "section"),
        heuristic_profile="structural",
    )

    result = asyncio.run(
        pipeline.parse_document_with_trace(
            pdf_path="/tmp/demo.pdf",
            page_limit=2,
            mode="auto",
        )
    )

    assert result.backend_attempted_pages == [2]
    assert result.backend_used_pages == []
    assert result.backend_fallback_pages == [2]
    assert result.parsed_pages[0].error == "backend_parser_not_configured"
    assert calls == [
        "extract:/tmp/demo.pdf:2",
        "normalize:1",
        "normalize:2",
        "resolve:2",
        "build",
        "table",
        "triage:auto",
        "fuse:1",
        "role",
        "section",
    ]


def test_hybrid_pipeline_records_batch_level_fallback_when_backend_batch_throws():
    class _FailingBatchParser(_PageParser):
        async def parse_pages(self, *, pdf_path: str, resolved_pages, triage_results):
            page_numbers = [int(page.page) for page in list(resolved_pages or [])]
            self._calls.append(f"parse_pages:{pdf_path}:{page_numbers}")
            raise RuntimeError("backend down")

    calls: list[str] = []
    pipeline = LocalStructuredPdfHybridPipeline(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
        page_parser=_FailingBatchParser(calls, configured=True, used=True),
        fusion_service=_FusionService(calls),
        block_role_resolver=_Resolver(calls, "role"),
        section_resolver=_Resolver(calls, "section"),
        heuristic_profile="structural",
    )

    result = asyncio.run(
        pipeline.parse_document_with_trace(
            pdf_path="/tmp/demo.pdf",
            page_limit=2,
            mode="auto",
        )
    )

    assert result.backend_attempted_pages == [2]
    assert result.backend_used_pages == []
    assert result.backend_fallback_pages == [2]
    assert result.parsed_pages[0].error == "backend_batch_failed"
    assert calls == [
        "extract:/tmp/demo.pdf:2",
        "normalize:1",
        "normalize:2",
        "resolve:2",
        "build",
        "table",
        "triage:auto",
        "parse_pages:/tmp/demo.pdf:[2]",
        "fuse:1",
        "role",
        "section",
    ]


def test_hybrid_pipeline_does_not_count_used_when_backend_returns_empty_blocks():
    class _EmptyBlocksParser(_PageParser):
        async def parse_pages(self, *, pdf_path: str, resolved_pages, triage_results):
            page_numbers = [int(page.page) for page in list(resolved_pages or [])]
            self._calls.append(f"parse_pages:{pdf_path}:{page_numbers}")
            return [
                PdfHybridParsedPage(
                    page=2,
                    model="qwen-vl-local",
                    page_role="body",
                    used=True,
                    error="",
                    blocks=[],
                )
            ]

    calls: list[str] = []
    pipeline = LocalStructuredPdfHybridPipeline(
        extractor=_Extractor(calls),
        normalizer=_Normalizer(calls),
        document_resolver=_DocumentResolver(calls),
        block_builder=_BlockBuilder(calls),
        table_detector=_TableDetector(calls),
        triage_service=_TriageService(calls),
        page_parser=_EmptyBlocksParser(calls, configured=True, used=True),
        fusion_service=_FusionService(calls),
        block_role_resolver=_Resolver(calls, "role"),
        section_resolver=_Resolver(calls, "section"),
        heuristic_profile="structural",
    )

    result = asyncio.run(
        pipeline.parse_document_with_trace(
            pdf_path="/tmp/demo.pdf",
            page_limit=2,
            mode="auto",
        )
    )

    assert result.backend_attempted_pages == [2]
    assert result.backend_used_pages == []
    assert result.backend_fallback_pages == [2]
    assert result.parsed_pages[0].error == "backend_empty_result"
    assert result.document.pages[1].blocks[0].text == "Backend page"
    assert calls == [
        "extract:/tmp/demo.pdf:2",
        "normalize:1",
        "normalize:2",
        "resolve:2",
        "build",
        "table",
        "triage:auto",
        "parse_pages:/tmp/demo.pdf:[2]",
        "fuse:1",
        "role",
        "section",
    ]
