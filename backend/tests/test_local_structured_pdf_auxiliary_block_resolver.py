from __future__ import annotations

from app.services.local_structured_pdf import (
    LocalPdfAuxiliaryBlockResolver,
    PdfBBox,
    PdfPageMeta,
    PdfSemanticBlock,
    PdfStructuredDocument,
    PdfStructuredPage,
)


def _block(
    *,
    block_id: str,
    block_type: str,
    text: str,
    top: float,
    bottom: float,
    avg_font_size: float = 10.0,
    page: int = 1,
    column_id: str = "main",
    region: str = "main",
) -> PdfSemanticBlock:
    return PdfSemanticBlock(
        block_id=block_id,
        block_type=block_type,
        page_start=page,
        page_end=page,
        text=text,
        bbox=PdfBBox(x0=60.0, top=top, x1=320.0, bottom=bottom),
        line_ids=[f"{block_id}:l1"],
        column_id=column_id,
        region=region,
        avg_font_size=avg_font_size,
        reading_order_start=int(top),
        reading_order_end=int(bottom),
    )


def test_resolve_document_merges_caption_followup_blocks():
    resolver = LocalPdfAuxiliaryBlockResolver()
    document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                blocks=[],
            )
        ],
        blocks=[
            _block(
                block_id="b1",
                block_type="paragraph",
                text="Figure 2. Training datasets used for evaluation",
                top=180.0,
                bottom=192.0,
                avg_font_size=9.0,
            ),
            _block(
                block_id="b2",
                block_type="paragraph",
                text="across multiple benchmark settings.",
                top=194.0,
                bottom=206.0,
                avg_font_size=9.0,
            ),
            _block(
                block_id="b3",
                block_type="paragraph",
                text="The body paragraph starts here.",
                top=240.0,
                bottom=252.0,
                avg_font_size=11.0,
            ),
        ],
        body_font_size=11.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["caption", "paragraph"]
    assert resolved.blocks[0].text == (
        "Figure 2. Training datasets used for evaluation\nacross multiple benchmark settings."
    )


def test_resolve_document_merges_footnote_followup_blocks():
    resolver = LocalPdfAuxiliaryBlockResolver()
    document = PdfStructuredDocument(
        pages=[
            PdfStructuredPage(
                meta=PdfPageMeta(page=1, page_width=600.0, page_height=800.0, rotation=0),
                blocks=[],
            )
        ],
        blocks=[
            _block(
                block_id="b1",
                block_type="paragraph",
                text="24 For more details on the symbols that appear in al-Sadu",
                top=520.0,
                bottom=532.0,
                avg_font_size=9.0,
                column_id="right",
                region="right_column",
            ),
            _block(
                block_id="b2",
                block_type="paragraph",
                text="weavings, see also Altaf Salem Al-Ali Al-Sabah, Ibjad.",
                top=534.0,
                bottom=546.0,
                avg_font_size=9.0,
                column_id="right",
                region="right_column",
            ),
            _block(
                block_id="b3",
                block_type="paragraph",
                text="Main body paragraph continues elsewhere.",
                top=560.0,
                bottom=572.0,
                avg_font_size=11.0,
                column_id="left",
                region="left_column",
            ),
        ],
        body_font_size=11.0,
    )

    resolved = resolver.resolve_document(document=document)

    assert [block.block_type for block in resolved.blocks] == ["footnote", "paragraph"]
    assert resolved.blocks[0].text == (
        "24 For more details on the symbols that appear in al-Sadu\n"
        "weavings, see also Altaf Salem Al-Ali Al-Sabah, Ibjad."
    )
