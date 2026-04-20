## Local Structured PDF

This package is the new local PDF parsing line built during the OpenDataLoader-style parser work.

What lives here:

- `native_extractor.py`: Stage 0 raw extraction. Reads page-level atoms from `pdfplumber`, `PyMuPDF`, and `pypdf`.
- `page_normalizer.py`: Stage 1 cleanup. Filters noisy atoms and groups words into stable text lines.
- `document_resolver.py`: Stage 2 reading-order logic. Removes repeated header/footer noise and resolves page order and column flow.
- `block_builder.py`: Stage 3 semantic block recovery. Builds `heading`, `paragraph`, and `list_item` blocks from resolved lines.
- `table_detector.py`: Table reconstruction and table block materialization.
- `block_role_resolver.py`, `auxiliary_block_resolver.py`, `front_matter_resolver.py`, `heading_refiner.py`, `toc_resolver.py`, `section_resolver.py`: post-processors that assign page or block roles and improve final structure.
- `markdown_renderer.py`: Renders the parsed document into Markdown for evaluation and downstream ingestion.
- `eval_suite_runner.py`, `eval_diagnostics.py`, `external_holdout_builder.py`, `readoc_holdout_builder.py`: tooling for running internal and external evaluation suites.
- `pipeline.py`: end-to-end orchestration for the local parser.
- `contracts.py`: shared data contracts used across all stages.

How to think about this directory:

- This is source code, not generated output.
- The parser is intentionally isolated from the legacy PDF ingestion chains.
- Evaluation artifacts are written under `backend/tmp/` and `backend/eval/results/`, not here.

