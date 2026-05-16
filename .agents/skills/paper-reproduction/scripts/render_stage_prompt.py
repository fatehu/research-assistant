#!/usr/bin/env python3
"""Render stage prompts for the project-first paper-reproduction skill."""

from __future__ import annotations

import argparse
import json


def _build_prompt(stage: str, paper_id: int, project_id: int | None, goal: str | None, preferred_draft_id: str | None) -> str:
    header = [f"paper_id={paper_id}"]
    if project_id is not None:
        header.append(f"project_id={project_id}")
    if goal:
        header.append(f"goal={goal}")
    if preferred_draft_id:
        header.append(f"preferred_draft_id={preferred_draft_id}")
    prefix = "\n".join(header)

    lines = [
        f"stage={stage}",
        "Use the paper-reproduction skill for this project-based paper reproduction task.",
        "When the task is reproduction work, read and follow the paper-reproduction skill before doing anything else.",
        "Do not invent a workflow state machine.",
        "First confirm the paper and project binding.",
        "Treat /app/uploads/projects/{project_id} as the only working root for this task.",
        "If no project exists yet, create or reuse it through paper_research_prepare.",
        "If a project already exists, check its status first with paper_research_status.",
        "If prepare is not finished yet, call paper_research_prepare.",
        "As soon as prepare is finished, stop trying to do the reproduction work yourself and start using project_claude.",
        "Let project_claude work in the current Project until it reports a result.",
        "Use paper_search only to find a saved paper by title, authors, keywords, or natural-language description; if paper_id is already known, pass it directly instead of searching for the numeric ID.",
        "Use project_tree only to inspect directory structure when needed.",
        "Use project_read_file to read a specific known file by relative path when needed.",
        "Use project_write_report to persist research reports, tuning plans, and worker handoff notes under reference/reports/*.md.",
        "Do not write code, scripts, data, or repo/source files yourself in this skill; those changes go through project_claude.",
        "Use project_claude as the default worker for coding, command execution, debugging, and continuing the reproduction attempt inside the current Project directory.",
        "Use paper_research_search_project_zoekt only for targeted text search across the project.",
        "Use paper_research_probe_repo only for checking a remote official repo URL.",
        "Use paper_research_probe_url only for lightweight checks of external download/doc links.",
        "Do not use `*` to list files with Zoekt; that is what project_tree is for.",
        "Preferred Zoekt query patterns include plain terms, file:README, content:\"...\", regex:/.../, case:yes, lang:python, sym:\"...\", boolean OR with parentheses, and negation such as -file:website/.",
        "Useful reproduction-oriented Zoekt recipes include (README or docs) supervised, content:\"train_supervised\", bucket wordNgrams dim lr epoch, file:classification-results.sh test, and file:dictionary.cc getLine.",
        "If a Zoekt query returns 0 results, shorten it to one strong term or one concrete filename before adding more filters.",
        "Do not stay in an inspection loop after prepare is ready; hand the concrete reproduction work to project_claude.",
    ]
    return f"{prefix}\n" + "\n".join(lines)


build_prompt = _build_prompt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["planning", "execution", "tuning"])
    parser.add_argument("--paper-id", type=int, required=True)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--goal")
    parser.add_argument("--preferred-draft-id")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    prompt = _build_prompt(
        stage=args.stage,
        paper_id=args.paper_id,
        project_id=args.project_id,
        goal=args.goal,
        preferred_draft_id=args.preferred_draft_id,
    )
    if args.as_json:
        print(json.dumps({"stage": args.stage, "prompt": prompt}, ensure_ascii=False))
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
