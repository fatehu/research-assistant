## Eval Manifests

This directory stores suite manifests for the local structured PDF parser.

Current manifest files:

- `local_structured_pdf_suites_v1.json`: internal suites, mainly the local OpenDataLoader-style benchmark mirror and smoke sets.
- `local_structured_pdf_external_suites_v1.json`: external suites, such as READoc-derived holdouts.

What a manifest does:

- names a suite
- points to its `pdfs/` directory
- points to its ground-truth Markdown directory
- controls whether the suite is enabled

These files are configuration, not generated output.

