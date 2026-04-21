# Upstream Notes

Upstream repository:
- `https://github.com/going-doer/Paper2Code`
- current observed license: `Apache-2.0`

Paper:
- `https://arxiv.org/abs/2504.17192`

## Pipeline

The upstream repo is a separate three-stage pipeline:

1. `1_planning.py`
2. `2_analyzing.py`
3. `3_coding.py`

There are OpenAI-backed and vLLM-backed variants.

## Inputs

Supported inputs in the upstream scripts:
- cleaned paper JSON
- cleaned LaTeX source

The upstream README also documents PDF-to-JSON conversion through `s2orc-doc2json`, but that is an external prerequisite and should be treated as optional.

## Smallest Demo

The smallest reliable demo is the bundled Transformer example:
- `examples/Transformer_cleaned.json`

This avoids the PDF conversion path and keeps the demo focused on the core Paper2Code pipeline.

## Outputs

The important outputs are:
- `planning_artifacts/`
- `analyzing_artifacts/`
- generated repository directory

Treat these as external demo artifacts, not as our built-in project truth files.

