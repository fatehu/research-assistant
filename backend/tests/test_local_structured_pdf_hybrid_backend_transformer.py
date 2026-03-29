from __future__ import annotations

from app.services.local_structured_pdf import LocalPdfHybridBackendTransformer


def test_hybrid_backend_transformer_accepts_loose_elements_payload():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "triage": {"page_type": "plain_text"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "Abstract",
                "bbox": {"x0": 80.0, "top": 100.0, "x1": 180.0, "bottom": 114.0},
            },
            {
                "line_id": "l2",
                "text": "First sentence.",
                "bbox": {"x0": 80.0, "top": 120.0, "x1": 320.0, "bottom": 134.0},
            },
        ],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "elements": [
            {"type": "section_header", "line_ids": ["l1"]},
            {"type": "text", "line_ids": ["l2"]},
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["kind"] for block in result["blocks"]] == ["heading", "paragraph"]
    assert result["blocks"][0]["source_line_ids"] == ["l1"]


def test_hybrid_backend_transformer_prefers_docling_payload_over_custom_blocks():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "blocks": [
            {
                "kind": "paragraph",
                "text": "WRONG CUSTOM BLOCK",
                "bbox": {"x0": 50.0, "top": 50.0, "x1": 200.0, "bottom": 80.0},
            }
        ],
        "texts": [
            {
                "label": "section_header",
                "text": "Introduction",
                "meta": {"level": 2},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 100.0, "r": 220.0, "b": 116.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            }
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["kind"] == "heading"
    assert result["blocks"][0]["text"] == "Introduction"


def test_hybrid_backend_transformer_filters_docling_elements_to_current_page():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "section_header",
                "text": "Introduction",
                "meta": {"level": 2},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 100.0, "r": 220.0, "b": 116.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "text",
                "text": "Other page text",
                "prov": [
                    {
                        "page_no": 2,
                        "bbox": {"l": 80.0, "t": 130.0, "r": 320.0, "b": 150.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["text"] == "Introduction"


def test_hybrid_backend_transformer_allows_unanchored_text_for_visual_pages():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "triage": {"page_type": "visual_or_scanned"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "tiny residual",
                "bbox": {"x0": 20.0, "top": 760.0, "x1": 80.0, "bottom": 772.0},
            }
        ],
    }
    payload = {
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

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert result["blocks"][0]["source_line_ids"] == []
    assert result["blocks"][0]["text"] == "REAL TITLE FROM OCR"


def test_hybrid_backend_transformer_preserves_picture_block_without_text_when_bbox_exists():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 2200.0,
        "triage": {"page_type": "visual_or_scanned"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "poster",
        "pictures": [
            {
                "label": "picture",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 100.0,
                            "t": 2079.8,
                            "r": 224.2,
                            "b": 1978.7,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
                "annotations": [],
            }
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert result["blocks"][0]["kind"] == "figure_meta"
    assert result["blocks"][0]["text"] == ""


def test_hybrid_backend_transformer_drops_invalid_line_ids_for_visual_pages_when_text_present():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "triage": {"page_type": "visual_or_scanned"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "tiny residual",
                "bbox": {"x0": 20.0, "top": 760.0, "x1": 80.0, "bottom": 772.0},
            }
        ],
    }
    payload = {
        "page": 1,
        "page_role": "poster",
        "elements": [
            {
                "type": "text",
                "line_ids": ["made_up_line"],
                "text": "Recovered OCR paragraph",
                "bbox": {"x0": 90.0, "top": 180.0, "x1": 510.0, "bottom": 260.0},
            }
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert result["blocks"][0]["source_line_ids"] == []
    assert result["blocks"][0]["text"] == "Recovered OCR paragraph"


def test_hybrid_backend_transformer_salvages_valid_blocks_when_some_blocks_are_bad():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "triage": {"page_type": "visual_or_scanned"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "Kept line",
                "bbox": {"x0": 80.0, "top": 100.0, "x1": 180.0, "bottom": 114.0},
            }
        ],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "elements": [
            {"type": "text", "line_ids": ["l1"]},
            {"type": "text", "line_ids": ["missing_1", "missing_2"]},
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["source_line_ids"] == ["l1"]


def test_hybrid_backend_transformer_infers_source_line_ids_from_bbox_for_non_visual_pages():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_width": 600.0,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "Introduction",
                "reading_order": 1,
                "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 112.0},
            },
            {
                "line_id": "l2",
                "text": "First sentence.",
                "reading_order": 2,
                "bbox": {"x0": 80.0, "top": 120.0, "x1": 320.0, "bottom": 136.0},
            },
            {
                "line_id": "l3",
                "text": "Second sentence.",
                "reading_order": 3,
                "bbox": {"x0": 80.0, "top": 140.0, "x1": 320.0, "bottom": 156.0},
            },
        ],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "blocks": [
            {
                "block_id": "mm_p0001_b0001",
                "kind": "heading",
                "reading_order": 1,
                "text": "Introduction",
                "bbox": {"x0": 72.0, "top": 92.0, "x1": 240.0, "bottom": 116.0},
            },
            {
                "block_id": "mm_p0001_b0002",
                "kind": "paragraph",
                "reading_order": 2,
                "text": "First sentence. Second sentence.",
                "bbox": {"x0": 72.0, "top": 118.0, "x1": 336.0, "bottom": 160.0},
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["source_line_ids"] for block in result["blocks"]] == [["l1"], ["l2", "l3"]]


def test_hybrid_backend_transformer_accepts_docling_like_texts_with_prov_bbox():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_width": 600.0,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "Introduction",
                "reading_order": 1,
                "bbox": {"x0": 80.0, "top": 96.0, "x1": 220.0, "bottom": 112.0},
            },
            {
                "line_id": "l2",
                "text": "First sentence.",
                "reading_order": 2,
                "bbox": {"x0": 80.0, "top": 130.0, "x1": 260.0, "bottom": 146.0},
            },
            {
                "line_id": "l3",
                "text": "Second sentence.",
                "reading_order": 3,
                "bbox": {"x0": 262.0, "top": 130.0, "x1": 430.0, "bottom": 146.0},
            },
            {
                "line_id": "l4",
                "text": "Extra local line that should not be stitched in.",
                "reading_order": 4,
                "bbox": {"x0": 80.0, "top": 150.0, "x1": 520.0, "bottom": 166.0},
            },
        ],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "section_header",
                "text": "Introduction",
                "meta": {"level": 2},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 100.0, "r": 220.0, "b": 116.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "text",
                "orig": "First sentence. Second sentence.",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 130.0, "r": 520.0, "b": 170.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["kind"] for block in result["blocks"]] == ["heading", "paragraph"]
    assert result["blocks"][0]["source_line_ids"] == []
    assert result["blocks"][0]["bbox"] == {"x0": 80.0, "top": 100.0, "x1": 220.0, "bottom": 116.0}
    assert result["blocks"][0]["heading_level"] == 2
    assert result["blocks"][1]["source_line_ids"] == []
    assert result["blocks"][1]["text"] == "First sentence. Second sentence."


def test_hybrid_backend_transformer_does_not_expand_docling_text_from_prompt_line_rows():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_width": 600.0,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [
            {
                "line_id": "l1",
                "text": "Alpha",
                "reading_order": 1,
                "bbox": {"x0": 80.0, "top": 130.0, "x1": 140.0, "bottom": 146.0},
            },
            {
                "line_id": "l2",
                "text": "Beta",
                "reading_order": 2,
                "bbox": {"x0": 142.0, "top": 130.0, "x1": 190.0, "bottom": 146.0},
            },
            {
                "line_id": "l3",
                "text": "Gamma should stay local only",
                "reading_order": 3,
                "bbox": {"x0": 80.0, "top": 150.0, "x1": 320.0, "bottom": 166.0},
            },
        ],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "text",
                "text": "Alpha Beta",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 128.0, "r": 320.0, "b": 168.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            }
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["text"] == "Alpha Beta"
    assert result["blocks"][0]["source_line_ids"] == []


def test_hybrid_backend_transformer_preserves_docling_caption_and_footnote_semantics():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "caption",
                "text": "Figure 1. Trend overview.",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 90.0, "t": 220.0, "r": 420.0, "b": 240.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "footnote",
                "text": "1 Supplemental note.",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 90.0, "t": 720.0, "r": 260.0, "b": 735.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["kind"] for block in result["blocks"]] == ["caption", "footnote"]


def test_hybrid_backend_transformer_preserves_docling_list_item_semantics():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "plain_text"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "list_item",
                "text": "• First bullet",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 90.0, "t": 220.0, "r": 420.0, "b": 240.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "list",
                "text": "• Second bullet",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 90.0, "t": 250.0, "r": 420.0, "b": 270.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["kind"] for block in result["blocks"]] == ["list_item", "list_item"]


def test_hybrid_backend_transformer_preserves_docling_table_grid_rows():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "dense_table"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "table",
        "tables": [
            {
                "label": "table",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 120.0, "r": 320.0, "b": 220.0, "coord_origin": "TOPLEFT"},
                    }
                ],
                "data": {
                    "grid": [
                        ["Metric", "Value"],
                        ["A", "1"],
                    ]
                },
            }
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert result["blocks"][0]["kind"] == "table"
    assert result["blocks"][0]["table_rows"] == [["Metric", "Value"], ["A", "1"]]
    assert "Metric | Value" in result["blocks"][0]["text"]


def test_hybrid_backend_transformer_filters_docling_page_furniture_labels():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "plain_text"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "page_header",
                "text": "Journal Header",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 40.0, "r": 300.0, "b": 58.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "section_header",
                "text": "Introduction",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 100.0, "r": 220.0, "b": 116.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "page_footer",
                "text": "12",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 280.0, "t": 760.0, "r": 300.0, "b": 775.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["kind"] for block in result["blocks"]] == ["heading"]
    assert [block["text"] for block in result["blocks"]] == ["Introduction"]


def test_hybrid_backend_transformer_prefers_picture_description_annotation():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "pictures": [
            {
                "label": "picture",
                "annotations": [
                    {"kind": "classifier", "text": "chart"},
                    {"kind": "description", "text": "A bar chart comparing yearly totals."},
                ],
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 120.0, "t": 200.0, "r": 420.0, "b": 420.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            }
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert result["blocks"][0]["kind"] == "figure_meta"
    assert result["blocks"][0]["text"] == "A bar chart comparing yearly totals."


def test_hybrid_backend_transformer_keeps_raw_block_sorting_by_bbox():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 800.0,
        "triage": {"page_type": "mixed_layout"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "blocks": [
            {
                "block_id": "mm_p0001_b0002",
                "kind": "paragraph",
                "reading_order": 1,
                "text": "Body paragraph.",
                "bbox": {"x0": 80.0, "top": 180.0, "x1": 520.0, "bottom": 220.0},
            },
            {
                "block_id": "mm_p0001_b0001",
                "kind": "heading",
                "reading_order": 99,
                "text": "Introduction",
                "bbox": {"x0": 80.0, "top": 100.0, "x1": 220.0, "bottom": 116.0},
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["kind"] for block in result["blocks"]] == ["heading", "paragraph"]
    assert [block["reading_order"] for block in result["blocks"]] == [1, 2]


def test_hybrid_backend_transformer_preserves_docling_payload_order():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 1200.0,
        "triage": {"page_type": "plain_text"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "text",
                "text": "A long narrative paragraph that should stay before the later figure and chart labels because Docling already grouped this page semantically.",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 100.0, "r": 220.0, "b": 116.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "text",
                "text": "A second long prose block that confirms this page is prose-dominant rather than just a single top-of-page figure with local labels.",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 320.0, "t": 120.0, "r": 520.0, "b": 150.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "caption",
                "text": "Figure 8",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 180.0, "r": 220.0, "b": 196.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "picture",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 200.0, "r": 400.0, "b": 420.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["text"] for block in result["blocks"][:3]] == [
        "A long narrative paragraph that should stay before the later figure and chart labels because Docling already grouped this page semantically.",
        "A second long prose block that confirms this page is prose-dominant rather than just a single top-of-page figure with local labels.",
        "Figure 8",
    ]
    assert [block["reading_order"] for block in result["blocks"][:3]] == [1, 2, 3]


def test_hybrid_backend_transformer_does_not_preserve_docling_order_for_single_top_figure_page():
    transformer = LocalPdfHybridBackendTransformer()
    prompt_payload = {
        "page": 1,
        "page_height": 1200.0,
        "triage": {"page_type": "plain_text"},
        "line_rows": [],
    }
    payload = {
        "page": 1,
        "page_role": "body",
        "texts": [
            {
                "label": "caption",
                "text": "Figure 7.1",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 220.0, "r": 260.0, "b": 236.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "text",
                "text": "66%",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 420.0, "t": 80.0, "r": 480.0, "b": 96.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
            {
                "label": "section_header",
                "text": "IMPLEMENTATION",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 80.0, "t": 360.0, "r": 260.0, "b": 380.0, "coord_origin": "TOPLEFT"},
                    }
                ],
            },
        ],
    }

    result = transformer.transform_payload(payload=payload, prompt_payload=prompt_payload)

    assert result is not None
    assert [block["text"] for block in result["blocks"]] == ["66%", "Figure 7.1", "IMPLEMENTATION"]
