from app.services.smart_chunking.text_preprocessor import preprocess_text


def test_pdf_noise_cleanup_removes_figure_fragments():
    text = "\n".join(
        [
            "This paragraph explains the model design in detail.",
            "It contains complete sentences and should be preserved.",
            "Additional context is provided for the experiment setup.",
            "SAM",
            "VITDET",
            "80M",
            "Figure 1. Model architecture overview.",
            "Results show consistent gains across all datasets.",
            "The ablation section discusses component impact.",
            "Conclusion highlights practical deployment guidance.",
            "References are listed at the end of the paper.",
            "Appendix includes implementation details.",
        ]
    )

    cleaned = preprocess_text(text, file_type="pdf")

    assert "SAM" not in cleaned
    assert "VITDET" not in cleaned
    assert "80M" not in cleaned
    assert "Figure 1. Model architecture overview." in cleaned
    assert "Results show consistent gains across all datasets." in cleaned


def test_non_pdf_text_skips_ocr_cleanup():
    text = "\n".join(
        [
            "This is plain text content for a markdown file.",
            "SAM",
            "VITDET",
            "80M",
            "Figure 1. This line is not a real PDF caption context.",
            "Body content continues here with valid context.",
            "Another complete sentence appears in the document.",
            "This line keeps the sample above threshold for preprocessing.",
            "More normal text.",
            "And more normal text.",
            "And more normal text.",
            "And more normal text.",
        ]
    )

    cleaned = preprocess_text(text, file_type="txt")

    assert "SAM" in cleaned
    assert "VITDET" in cleaned
    assert "80M" in cleaned

