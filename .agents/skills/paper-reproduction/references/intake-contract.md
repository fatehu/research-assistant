# Intake Contract

Use this reference when preparing or explaining the stage-1 planning bundle:

- `planning/paper_intake_result.json`
- `planning/paper_summary.json`
- `planning/experiment_spec.json`

Current runtime stage id is still `planning`; semantically, that stage now corresponds to the new workflow stage `intake_summary`.

## Source Boundaries

- The intake stage uses the saved paper and the local PDF-to-markdown pipeline when available.
- The intake stage must produce both structured extraction facts and a reusable paper summary, not only experiment-oriented planning hints.
- Treat the paper's narrative sections as primary evidence: title, abstract, introduction, method, experiment text, conclusion, and figure captions.
- Treat table cells as reference evidence, not as the sole source of truth for execution scope.
- If the tool reports `source_mode=metadata_abstract_fallback`, explicitly say the result came from metadata/abstract fallback, not full PDF parsing.
- The structured intake artifact is JSON facts and discovery hints. It is not runnable code.
- Do not ask the intake model to generate Python code, shell commands, package installs, or fake repo paths from the paper alone.
- If the paper does not contain repo/data/code evidence, output missing discovery tasks instead of guessed implementation details.
- Stage 1 should classify paper-provided links and summarize research direction, research method, research content, and tuning hints.
- `planning/experiment_spec.json` at this stage is only a light paper-derived scaffold. It must not be treated as the final repo execution truth.

## JSON Output Rules

- Return strict JSON only when generating intake artifacts.
- Keep output compact enough for provider limits but preserve paper-grounded facts.
- Every concrete repo, dataset, metric, model, parameter, or artifact claim needs short paper evidence.
- Keep evidence strings short, ideally under 120 characters.
- Do not use confidence scores.

## Repository Fields

- `code_repositories` should distinguish:
  - `primary_official`
  - baseline implementation
  - third-party reference
  - unknown link
- A repository may be `primary_official` when the PDF claims code/details are open-sourced or the repo clearly matches the paper/topic/authors.
- Use `verification_status=paper_claimed` for PDF evidence. Do not mark anything `externally_verified` during intake because intake does not browse.

## Dataset Fields

- `dataset_candidates` should distinguish:
  - pretraining dumps
  - benchmark/evaluation datasets
  - demo datasets
  - repo assets
  - built-in datasets such as sklearn loaders
- Preserve filenames, Figshare-style artifacts, sklearn loaders, and notebook/script hints as `artifact_hint`, `source_type`, or `entrypoint_hints`.

## Optimization Fields

- `optimization_candidates` are paper-grounded analysis for later stages, not executable params.
- Include rationale, expected effect, risk, paper values, and verification needs.
- `run_plan_templates` may describe baseline/variant/sweep intent, required params, expected metrics, and blockers.
- `run_plan_templates` must not contain runnable code.
