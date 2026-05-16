---
name: paper2code
description: Use the upstream Paper2Code/PaperCoder repository as an isolated, repo-first paper-to-code generator. Trigger when the user explicitly asks to use Paper2Code/PaperCoder, wants a separate paper-to-code demo, or wants to generate an implementation repository from a paper using the Paper2Code pipeline instead of the built-in paper-reproduction workflow.
---

# Paper2Code

Use this skill only for the **external Paper2Code/PaperCoder workflow**. Do not fold it into `paper-reproduction`.

This skill is for:
- Paper2Code / PaperCoder demo runs
- isolated paper-to-code generation from a paper JSON or LaTeX source
- comparing our current workflow with the upstream three-stage Paper2Code pipeline

This skill is **not** for:
- continuing an existing saved paper Project
- writing `implementation_spec.json` / `run_drafts.json`
- using `paper_research_*` tools as the primary workflow

## Workflow

1. Keep the run isolated.
   - Do not write into the existing paper workspace unless the user explicitly asks.
   - Prefer a dedicated demo directory under `tmp/` or another isolated local path.

2. Bootstrap the upstream repo and run wrapper first.
   - Use `scripts/bootstrap_paper2code_demo.py`.
   - This script clones or updates the upstream repo, prepares output directories, and writes a deterministic run script.

3. Prefer the smallest viable demo.
   - For a quick validation, use the upstream bundled `Transformer` example.
   - For a real paper, prefer a cleaned paper JSON or LaTeX source.
   - Do not promise PDF ingestion unless the PDF-to-JSON dependency is actually available.

4. Run in one of two modes:
   - `openai`: upstream OpenAI scripts
   - `vllm`: upstream local/vLLM scripts

5. Treat outputs as external artifacts.
   - The important outputs are:
     - `planning_artifacts/`
     - `analyzing_artifacts/`
     - generated repository directory
   - Summarize what was generated and where it lives.

## Commands

Prepare a demo wrapper:

```bash
python .agents/skills/paper2code/scripts/bootstrap_paper2code_demo.py \
  --workspace-root /tmp/paper2code-demo \
  --mode openai \
  --paper-name Transformer \
  --paper-format json \
  --input-path /tmp/paper2code-demo/vendor/Paper2Code/examples/Transformer_cleaned.json
```

Then run the generated script:

```bash
bash /tmp/paper2code-demo/runs/transformer_openai_json/run.sh
```

## Rules

- Keep this workflow separate from `paper-reproduction`.
- Do not mutate existing saved-paper truth files.
- Do not claim the upstream repo can consume arbitrary PDFs directly unless the conversion prerequisite is satisfied.
- If the user asks for “just a small demo”, prefer the bundled Transformer example.

## References

- For upstream pipeline shape, input modes, and limitations, read `references/upstream-notes.md`.

