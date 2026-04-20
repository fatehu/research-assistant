# PDF -> Markdown -> Intake LLM Pipeline

This skill maps to the existing `PaperExperimentService` pipeline. It is not a hypothetical workflow.

Actual backend stages:

1. Resolve or download the paper PDF for the saved paper.
2. Run `pdf_ingest_service.ingest_pdf(..., mode="fast")`.
3. Read:
   - `document_text` as the paper markdown
   - `document_source_spans`
   - `extractor`
   - `report`
4. If markdown extraction fails or the PDF is unavailable, fall back to the paper abstract.
   - In that case `source_mode=metadata_abstract_fallback`
   - Do not describe the output as full PDF parsing
5. Send this payload into the intake LLM:
   - `metadata`
   - `raw_import_metadata_json`
   - `input_info`
   - `full_paper_markdown`
6. The intake LLM must return strict JSON.
7. Backend converts the intake JSON into `experiment_spec`, including:
   - `baseline`
   - `datasets`
   - `metrics`
   - `safe_knobs`
   - `allowed_model_swaps`
   - `optimization_brief.first_runs`
   - `intake_status`

Important fallback semantics:

- `source_mode=local_pdf_markdown` means the planning result was based on parsed paper markdown.
- `source_mode=metadata_abstract_fallback` means the planning result came from much weaker context.
- `intake_status.has_llm_intake=false` means no structured intake JSON was produced.

The workflow prepares planning artifacts only. It does not prove repo reproducibility and does not execute training.
