---
name: paper-reproduction
description: Project-first paper reproduction skill. Use when the user wants to reproduce a saved paper, continue an existing paper reproduction project, or inspect a paper implementation repository for reproduction work.
---

# Paper Reproduction

Use this skill for project-based paper reproduction work.

Activate it when the user is asking to reproduce a paper, continue a paper reproduction project, or work through a paper's implementation repository and reference materials.

When the task is paper reproduction work, read and follow this skill before doing anything else.

Do not invent a workflow state machine. Follow only this main flow:

1. Resolve the bound paper.
2. Resolve the bound project for that paper.
3. If no project exists yet, create or reuse it through `paper_research_prepare`.
4. If a project already exists, check its status first with `paper_research_status`.
5. Treat `/app/uploads/projects/{project_id}` as the only working root.
6. If `prepare` is not finished yet, run `paper_research_prepare`.
7. As soon as `prepare` is finished, stop trying to do the reproduction work yourself.
8. Start talking to `project_claude` and let Claude Code work in the Project until it reports a result.

The core rule is simple:

- No project: create one.
- Existing project: check status.
- Prepare not done: run prepare.
- Prepare done: use `project_claude` as the worker.

The prepare step is expected to build these project reference files:

- `reference/paper/paper_pdf2md.md`
- `reference/paper/paper_interpretation.md`
- `reference/paper/paper_interpretation.json`
- `reference/repo/readme_intake.json`

Prefer the project-scoped tools for inspection and file work:

- `paper_search`
- `paper_research_prepare`
- `paper_research_status`
- `project_tree`
- `project_read_file`
- `project_write_file`
- `project_claude`
- `paper_research_search_project_zoekt`
- `paper_research_probe_repo`
- `paper_research_probe_url`

Use the inspection tools with clear roles:

- `paper_search` is only for finding a saved paper by title, authors, keywords, or natural-language description. If you already know `paper_id`, do not search for `"113"` or `"paperid=113"`; pass the `paper_id` directly to `paper_research_prepare` or `paper_research_status`.
- `paper_research_prepare` is for first-time preparation or explicit refresh. It creates or reuses the Project and builds `project/reference/`.
- `paper_research_status` is for checking whether the Project and its `reference/` bundle are already ready. It only reads state; it does not refresh anything.
- `project_tree` is for browsing directory structure and confirming where files live.
- `project_read_file` is for reading a specific known file by relative path.
- `project_write_file` writes the complete final contents of one file. It overwrites the file; it is not an append tool.
- `project_claude` is the default worker for reproduction work after `prepare` is done. Use it to talk to Claude Code inside the current Project directory so it can edit code, run commands, debug errors, and continue the reproduction attempt. It automatically reuses the existing Claude session for the current Project directory when one exists, otherwise it starts a new one.
- `paper_research_search_project_zoekt` is for fast text search across the whole project after you already know what concept, symbol, filename, path pattern, or phrase you want to find.
- `paper_research_probe_repo` is for checking whether an official remote repo URL is still reachable and cloneable.
- `paper_research_probe_url` is for lightly checking external download links or documentation links without downloading the full file.

Use `project_claude` as the main execution path:

- Once `paper_research_status` or `paper_research_prepare` shows that the Project is ready, hand the actual reproduction work to `project_claude`.
- Do not keep looping on `project_tree`, `project_read_file`, or Zoekt instead of starting Claude.
- Use `project_tree`, `project_read_file`, and Zoekt mainly for preparation, for small clarifications, or for checking what Claude changed after it reports back.
- After `project_claude` returns, read its result, inspect only what you need, then either give Claude the next concrete instruction or report the result to the user.

Use Zoekt as a real search engine, not as a directory lister:

- Do not use `*` to try to list the whole project. If you want the layout, call `project_tree`.
- Use plain terms for substring search, such as `fastText` or `train_supervised`.
- Use `file:` to restrict by filename or path, such as `file:README`, `file:\\.md$`, or `file:repo/source/src/`.
- Use `content:"..."` for exact phrases.
- Use `regex:/.../` for regular-expression search.
- Use `case:yes` or `case:no` to control case sensitivity.
- Use `lang:python`, `lang:cpp`, and similar filters when searching source files.
- Use `sym:"..."` when you are looking for a symbol or API name.
- Use boolean combinations such as `(README or docs)`, and negation such as `-file:website/`.
- Use `type:filename` when you only want matching filenames instead of content matches.

Use task-shaped Zoekt recipes instead of free-form guessing:

- README / docs overview:
  - `file:README fastText`
  - `(README or docs) supervised`
  - `file:\\.md$ reproduction`
- Training entrypoints and commands:
  - `content:"train_supervised"`
  - `regex:/train_(supervised|unsupervised)/`
  - `file:classification-example.sh supervised`
  - `file:classification-results.sh test`
- Hyperparameters and CLI knobs:
  - `bucket wordNgrams dim lr epoch`
  - `loss minn maxn thread`
  - `content:"parseArgs"`
- Data format and labels:
  - `__label__`
  - `dataset download preprocess`
  - `file:dictionary.cc getLine`
- Evaluation and prediction flow:
  - `predict test precision recall`
  - `file:classification-results.sh`
- Core implementation drill-down:
  - `fasttext.cc supervised`
  - `file:dictionary.cc getLine`
  - `sym:"FastText"`
  - `lang:cpp content:"model"`

When a Zoekt query returns 0 results:

- Shorten the query to one strong term or one filename.
- Remove the least certain term before adding more filters.
- Prefer `file:README` or a concrete filename over `file:*.cc`.
- If you know the language but not the file, use `lang:cpp` or `lang:python`.
- Find filenames first, then read the file with `project_read_file`.

For stage launches, use `scripts/render_stage_prompt.py`.
