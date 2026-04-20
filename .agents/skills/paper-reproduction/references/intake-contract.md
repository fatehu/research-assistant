# Intake Contract

Use this reference when preparing or explaining `planning/paper_intake_result.json` and `planning/experiment_spec.json`.

## Source Boundaries

- The intake stage uses the saved paper and the local PDF-to-markdown pipeline when available.
- If the tool reports `source_mode=metadata_abstract_fallback`, explicitly say the result came from metadata/abstract fallback, not full PDF parsing.
- The structured intake artifact is JSON facts and discovery hints. It is not runnable code.
- Do not ask the intake model to generate Python code, shell commands, package installs, or fake repo paths from the paper alone.
- If the paper does not contain repo/data/code evidence, output missing discovery tasks instead of guessed implementation details.

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
