# Run Draft Heuristics

The run-draft stage turns `implementation_spec` plus repo evidence into actionable, reviewable run drafts. It does not execute anything.

Research basis from arXiv-linked ML/DL repositories:

- `automl/nanoTabPFN`: lightweight repo; README separates dependency install, Figshare HDF5 data download, `train.py` baseline, and sklearn demo usage.
- `facebookresearch/dinov2`: large-scale repo; README separates dataset metadata preparation, training scripts, evaluation scripts, pretrained weights, notebooks, and strong resource requirements.
- `microsoft/LoRA`: package plus example directories; README points to NLU/NLG examples for paper reproduction rather than one root-level training script.
- `dreamquark-ai/tabnet`: library-style repo; README emphasizes installation, sklearn-style API usage, notebooks, metrics, and tunable model parameters instead of a single reproduction command.

Use the README/repo evidence the way mature ML repositories do:

- Treat installation, data preparation, baseline training, evaluation, and tuning as separate concerns.
- Do not force every concern to have a repo script. README commands and manual dataset steps are valid draft entrypoints.
- Only use `repo_script`, `notebook`, or `config` when the referenced path exists in `repo/repo_file_index.json` or was read via `paper_research_read_repo_file`.
- For `repo_script`, write the repo file into `entrypoint.path_or_hint` as a repo-relative path such as `seq2seq.py`, not `repo/source/seq2seq.py`.
- If a README contains a download command but no script exists, use `entrypoint.type="readme_command"` or `entrypoint.type="dataset_step"` and cite `repo/source/README.md`.
- If dependencies are only described in README and no dependency file exists, create an `env_setup` draft or add an explicit blocker. Do not invent `requirements.txt`.
- Baseline reproduction should come before tuning. A tuning draft should depend on a baseline draft or state that the baseline is still blocked.
- After a baseline succeeds, create `first_tuning` from the paper-grounded `tuning_plan`, not from generic ML advice.
- Prefer a single low-risk change for `first_tuning`; save model swaps, algorithm changes, and sweeps for later confirmed runs.
- Canonical artifact evidence still uses archived paths such as `repo/source/seq2seq.py`; do not mix this with `entrypoint.path_or_hint`.
- If a repo hard-codes parameters and has no config/CLI, represent the variant as an execution-scoped generated script rather than modifying the original repository.
- A generated variant script should live under `executions/{execution_id}/...` and should be referenced from the execution spec with `generated_files`.
- When the execution `cwd` remains `repo/source`, command paths to generated files should be relative to that cwd, such as `../executions/{execution_id}/train_variant.py`.
- Keep drafts small and ordered by execution dependency.

Recommended draft order:

1. `env_setup`: install dependencies or identify missing dependency evidence.
2. `data_prep`: download or prepare required data.
3. `smoke_test`: optional lightweight import/data-loader check when evidence supports it.
4. `baseline_repro`: run the main verified training/notebook entrypoint with default parameters.
5. `evaluation`: run or describe the verified evaluation path.
6. `first_tuning`: one small grounded variation after the baseline is feasible.

Never invent a file such as `data_prep.py` just because a data-prep draft needs an entrypoint. Use a non-file entrypoint type instead.

Repository shape rules:

- Lightweight script repo: prefer `env_setup`, `data_prep`, `baseline_repro`, then `first_tuning`.
- Notebook-first repo: use `entrypoint.type="notebook"` for verified `.ipynb` files and keep parameter changes in draft `params`.
- Library-style repo: use `manual_step` or `notebook` unless an actual example script exists; do not invent a training script.
- Large-scale training repo: include resource blockers and separate `evaluation` from `baseline_repro`; do not mark the draft executable when datasets, checkpoints, or cluster resources are missing.
- Example-directory repo: entrypoints may live under `examples/...`; read the relevant example README or script before drafting.
