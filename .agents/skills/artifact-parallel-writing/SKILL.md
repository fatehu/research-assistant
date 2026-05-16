---
name: artifact-parallel-writing
description: Coordinate multi-block document artifact reading and batch writing without losing the update plan.
---

# Artifact Parallel Writing

Use this skill when the user asks to expand, continue, rewrite, balance, or fill multiple document artifact modules in one task, for example "整体补到 10000", "每个模块 2500", "把这些模块都补完", or "批量更新这些块".

This workflow is for the current chat conversation's active document artifact only. It is not a Project workflow and must not use Project tools.

## Workflow

0. Persist the workflow for this session.
   - At the start of a multi-turn artifact writing task, call `activate_skill` with `skill_name="artifact-parallel-writing"` and `mode="append"` unless the skill is already active.
   - This keeps the same writing contract available when the user later says "继续".
1. Establish the scope.
   - If the user selected block IDs in the artifact panel, treat those block IDs as the primary scope.
   - If no selection is present and the user asks for a whole-document change, call `document_artifact_read` in list mode first: `include_markdown=false`.
   - Use the returned titles, heading paths, target words, and constraints to choose the relevant block IDs.
2. Read context.
   - It is allowed to call `document_artifact_read` with multiple `block_ids` and `include_markdown=true` when global consistency is needed.
   - Do not read unrelated cover, TOC, reference, or appendix blocks unless the user asks for them.
3. Create an internal write plan before writing.
   - Record the target `block_id` list.
   - Record the intended role and target length of each block.
   - Decide whether each block should be replaced or left unchanged.
   - Keep the plan concise; do not output it to the user unless asked.
4. Write atomically when multiple blocks are affected.
   - If more than one block must be updated, prefer `document_artifact_update_blocks`.
   - Each item in `updates` must contain the existing `block_id`, the complete Markdown for that block, and optional `status`.
   - Do not output the update JSON or Markdown as a normal assistant answer instead of calling the tool.
5. Verify and report.
   - After a successful batch update, summarize which block IDs were updated.
   - If a write fails, do not claim completion. Retry with the same planned block IDs, or report the exact blocker after repeated failure.

## Rules

- Multi-block reading is allowed; multi-block writing must be committed through `document_artifact_update_blocks` whenever more than one block changes.
- Never rely on memory after a failed write. Reconstruct the write plan from the latest list/read observations before retrying.
- Do not create new block IDs. Only update block IDs that exist in the active artifact.
- Do not use `project_*`, `paper_research_*`, or DOCX tools for this workflow unless the user explicitly switches tasks.
- If the user asks for DOCX generation after artifact updates, first finish writing the artifact blocks, then call the DOCX tool in a separate step.
