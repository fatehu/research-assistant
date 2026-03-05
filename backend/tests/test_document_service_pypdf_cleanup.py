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
