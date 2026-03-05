from app.services.document_service import DocumentProcessor


def test_merge_hyphenated_lines_should_join_soft_break():
    lines = [
        "This method handles hyphen-",
        "ated tokens correctly.",
        "Another normal line.",
    ]
    merged = DocumentProcessor._merge_hyphenated_lines(lines)
    assert merged[0] == "This method handles hyphenated tokens correctly."
    assert merged[1] == "Another normal line."


def test_drop_repeated_edge_lines_should_remove_running_header_footer():
    pages = [
        ["Journal of Testing", "Body A1", "Body A2", "Page 1"],
        ["Journal of Testing", "Body B1", "Body B2", "Page 2"],
        ["Journal of Testing", "Body C1", "Body C2", "Page 3"],
    ]
    cleaned = DocumentProcessor._drop_repeated_edge_lines(pages)
    merged = "\n".join("\n".join(lines) for lines in cleaned)
    assert "Journal of Testing" not in merged
    assert "Body B1" in merged


def test_drop_repeated_edge_lines_should_remove_doi_footer_with_page_counter():
    pages = [
        [
            "Intro A",
            "Body A1",
            "Body A2",
            "PLOS DIGITAL HEALTH",
            "ChatGPT and medical education",
            "PLOS Digital Health | https://doi.org/10.1371/journal.pdig.0000198 February 9, 2023 1 / 12",
            "Tail A1",
            "Tail A2",
        ],
        [
            "Intro B",
            "Body B1",
            "Body B2",
            "PLOS DIGITAL HEALTH",
            "ChatGPT and medical education",
            "PLOS Digital Health | https://doi.org/10.1371/journal.pdig.0000198 February 9, 2023 2 / 12",
            "Tail B1",
            "Tail B2",
        ],
        [
            "Intro C",
            "Body C1",
            "Body C2",
            "PLOS DIGITAL HEALTH",
            "ChatGPT and medical education",
            "PLOS Digital Health | https://doi.org/10.1371/journal.pdig.0000198 February 9, 2023 3 / 12",
            "Tail C1",
            "Tail C2",
        ],
    ]
    cleaned = DocumentProcessor._drop_repeated_edge_lines(pages)
    merged = "\n".join("\n".join(lines) for lines in cleaned)
    assert "PLOS DIGITAL HEALTH" not in merged
    assert "ChatGPT and medical education" not in merged
    assert "journal.pdig.0000198" not in merged.lower()
    assert "Body B1" in merged


def test_normalize_edge_line_for_dedupe_should_mask_page_numbers():
    line_a = "PLOS Digital Health | https://doi.org/10.1371/journal.pdig.0000198 February 9, 2023 1 / 12"
    line_b = "PLOS Digital Health | https://doi.org/10.1371/journal.pdig.0000198 February 9, 2023 11 / 12"
    key_a = DocumentProcessor._normalize_edge_line_for_dedupe(line_a)
    key_b = DocumentProcessor._normalize_edge_line_for_dedupe(line_b)
    assert key_a == key_b


def test_drop_back_matter_lines_should_remove_reference_entries():
    processor = DocumentProcessor()
    pages = [
        ["Introduction line", "Methods line"],
        ["Results line", "Discussion line"],
        ["References", "1. Smith A. Title. 2020.", "2. Doe B. DOI:10.1000/xyz"],
    ]
    cleaned = processor._drop_back_matter_lines(pages)
    merged = "\n".join("\n".join(lines) for lines in cleaned)
    assert "Introduction line" in merged
    assert "Results line" in merged
    assert "References" not in merged
    assert "Smith A." not in merged
    assert "DOI:10.1000/xyz" not in merged


def test_is_reference_like_line_should_match_doi_and_numbered_items():
    processor = DocumentProcessor()
    assert processor._is_reference_like_line("1. Smith A. Example Title. 2022.")
    assert processor._is_reference_like_line("Some paper DOI:10.1000/xyz PMID:123")
    assert not processor._is_reference_like_line("This is a normal body sentence about the experiment.")


def test_filter_line_records_by_text_pages_should_keep_ordered_matches():
    pages_records = [
        [
            {"text": "Header", "page": 1},
            {"text": "Body A", "page": 1},
            {"text": "Body B", "page": 1},
        ]
    ]
    cleaned_pages = [["Body A", "Body B"]]
    kept = DocumentProcessor._filter_line_records_by_text_pages(pages_records, cleaned_pages)
    assert len(kept) == 1
    assert [row["text"] for row in kept[0]] == ["Body A", "Body B"]


def test_serialize_line_span_should_include_bbox_and_line_id():
    row = {
        "text": "Line text",
        "page": 2,
        "x0": 100.0,
        "y0": 420.0,
        "x1": 260.0,
        "y1": 435.0,
        "page_width": 612.0,
        "page_height": 792.0,
    }
    span = DocumentProcessor._serialize_line_span(line_id=7, row=row, text="Line text")
    assert span["line_id"] == 7
    assert span["page"] == 2
    assert span["x0"] == 100.0
    assert span["y1"] == 435.0
    assert span["coord_space"] == "pdf_user_space_bottom_origin"
