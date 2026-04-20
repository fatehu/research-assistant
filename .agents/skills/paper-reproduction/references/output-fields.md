# Planning Output Fields

The most useful fields returned through `paper_research_prepare` / workspace payload are:

- `workspace.execution_mode`
- `workspace.intake_status`
  - `has_llm_intake`
  - `input.source_mode`
  - `input.extractor`
  - `input.total_chars`
  - `input.sent_chars`
  - `input.truncated`
  - `markdown`
  - `error`
- `workspace.task`
  - task type / domain / problem statement
- `workspace.baseline`
  - entrypoint type
  - entrypoint hint
  - model family
  - default params
- `workspace.datasets`
- `workspace.metrics`
- `workspace.safe_knobs`
- `workspace.model_swap_candidates`
- `workspace.first_runs`

Interpretation rules:

- Treat all of these as planning output, not executed result.
- Missing fields mean missing evidence, not “the model forgot”.
- `first_runs` are suggestions from the intake/optimization brief.
- `safe_knobs` are the lowest-risk parameter controls suitable for early run drafts.
- `model_swap_candidates` are candidate replacements, not validated improvements.
