#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


UPSTREAM_REPO_URL = "https://github.com/going-doer/Paper2Code.git"


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    parts = [part for part in cleaned.split("-") if part]
    return "-".join(parts) or "paper2code-demo"


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def _ensure_repo(repo_dir: Path) -> None:
    git_dir = repo_dir / ".git"
    if git_dir.is_dir():
        _run(["git", "fetch", "--depth", "1", "origin", "HEAD"], cwd=repo_dir)
        _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repo_dir)
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(repo_dir)])


def _write_run_script(
    *,
    repo_dir: Path,
    run_dir: Path,
    mode: str,
    paper_name: str,
    paper_format: str,
    input_path: Path,
) -> Path:
    output_dir = run_dir / "outputs" / paper_name
    output_repo_dir = run_dir / "outputs" / f"{paper_name}_repo"
    cleaned_input = run_dir / "inputs" / (
        f"{paper_name}_cleaned.json" if paper_format == "json" else f"{paper_name}_cleaned.tex"
    )
    cleaned_input.parent.mkdir(parents=True, exist_ok=True)
    if input_path.resolve() != cleaned_input.resolve():
        shutil.copy2(input_path, cleaned_input)

    if mode == "openai":
        header = 'if [ -z "${OPENAI_API_KEY:-}" ]; then echo "OPENAI_API_KEY is required"; exit 1; fi'
        if paper_format == "json":
            body = f"""python "{repo_dir / 'codes' / '1_planning.py'}" \\
  --paper_name "{paper_name}" \\
  --gpt_version "o3-mini" \\
  --pdf_json_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '1.1_extract_config.py'}" \\
  --paper_name "{paper_name}" \\
  --output_dir "{output_dir}"

cp -rp "{output_dir / 'planning_config.yaml'}" "{output_repo_dir / 'config.yaml'}"

python "{repo_dir / 'codes' / '2_analyzing.py'}" \\
  --paper_name "{paper_name}" \\
  --gpt_version "o3-mini" \\
  --pdf_json_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '3_coding.py'}" \\
  --paper_name "{paper_name}" \\
  --gpt_version "o3-mini" \\
  --pdf_json_path "{cleaned_input}" \\
  --output_dir "{output_dir}" \\
  --output_repo_dir "{output_repo_dir}"
"""
        else:
            body = f"""python "{repo_dir / 'codes' / '1_planning.py'}" \\
  --paper_name "{paper_name}" \\
  --gpt_version "o3-mini" \\
  --paper_format "LaTeX" \\
  --pdf_latex_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '1.1_extract_config.py'}" \\
  --paper_name "{paper_name}" \\
  --output_dir "{output_dir}"

cp -rp "{output_dir / 'planning_config.yaml'}" "{output_repo_dir / 'config.yaml'}"

python "{repo_dir / 'codes' / '2_analyzing.py'}" \\
  --paper_name "{paper_name}" \\
  --gpt_version "o3-mini" \\
  --paper_format "LaTeX" \\
  --pdf_latex_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '3_coding.py'}" \\
  --paper_name "{paper_name}" \\
  --gpt_version "o3-mini" \\
  --paper_format "LaTeX" \\
  --pdf_latex_path "{cleaned_input}" \\
  --output_dir "{output_dir}" \\
  --output_repo_dir "{output_repo_dir}"
"""
    else:
        header = 'if ! python -c "import vllm" >/dev/null 2>&1; then echo "vllm is required"; exit 1; fi'
        if paper_format == "json":
            body = f"""python "{repo_dir / 'codes' / '1_planning_llm.py'}" \\
  --paper_name "{paper_name}" \\
  --model_name "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct" \\
  --tp_size "2" \\
  --pdf_json_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '1.1_extract_config.py'}" \\
  --paper_name "{paper_name}" \\
  --output_dir "{output_dir}"

cp -rp "{output_dir / 'planning_config.yaml'}" "{output_repo_dir / 'config.yaml'}"

python "{repo_dir / 'codes' / '2_analyzing_llm.py'}" \\
  --paper_name "{paper_name}" \\
  --model_name "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct" \\
  --tp_size "2" \\
  --pdf_json_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '3_coding_llm.py'}" \\
  --paper_name "{paper_name}" \\
  --model_name "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct" \\
  --tp_size "2" \\
  --pdf_json_path "{cleaned_input}" \\
  --output_dir "{output_dir}" \\
  --output_repo_dir "{output_repo_dir}"
"""
        else:
            body = f"""python "{repo_dir / 'codes' / '1_planning_llm.py'}" \\
  --paper_name "{paper_name}" \\
  --model_name "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct" \\
  --tp_size "2" \\
  --paper_format "LaTeX" \\
  --pdf_latex_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '1.1_extract_config.py'}" \\
  --paper_name "{paper_name}" \\
  --output_dir "{output_dir}"

cp -rp "{output_dir / 'planning_config.yaml'}" "{output_repo_dir / 'config.yaml'}"

python "{repo_dir / 'codes' / '2_analyzing_llm.py'}" \\
  --paper_name "{paper_name}" \\
  --model_name "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct" \\
  --tp_size "2" \\
  --paper_format "LaTeX" \\
  --pdf_latex_path "{cleaned_input}" \\
  --output_dir "{output_dir}"

python "{repo_dir / 'codes' / '3_coding_llm.py'}" \\
  --paper_name "{paper_name}" \\
  --model_name "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct" \\
  --tp_size "2" \\
  --paper_format "LaTeX" \\
  --pdf_latex_path "{cleaned_input}" \\
  --output_dir "{output_dir}" \\
  --output_repo_dir "{output_repo_dir}"
"""

    script_path = run_dir / "run.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_repo_dir.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f'PAPER_NAME="{paper_name}"\n'
        f'OUTPUT_DIR="{output_dir}"\n'
        f'OUTPUT_REPO_DIR="{output_repo_dir}"\n\n'
        f"{header}\n\n"
        f"{body}",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return script_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap an isolated Paper2Code demo workspace.")
    parser.add_argument("--workspace-root", required=True, help="Base directory for the isolated demo workspace.")
    parser.add_argument("--repo-dir", help="Optional explicit upstream repo checkout path.")
    parser.add_argument("--mode", choices=["openai", "vllm"], default="openai")
    parser.add_argument("--paper-name", default="Transformer")
    parser.add_argument("--paper-format", choices=["json", "latex"], default="json")
    parser.add_argument("--input-path", help="Path to cleaned JSON or LaTeX input. If omitted, use the upstream Transformer example.")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    repo_dir = Path(args.repo_dir).resolve() if args.repo_dir else workspace_root / "vendor" / "Paper2Code"
    _ensure_repo(repo_dir)

    default_input = (
        repo_dir / "examples" / "Transformer_cleaned.json"
        if args.paper_format == "json"
        else repo_dir / "examples" / "Transformer_cleaned.tex"
    )
    input_path = Path(args.input_path).resolve() if args.input_path else default_input.resolve()
    if not input_path.is_file():
        raise SystemExit(f"input file not found: {input_path}")

    run_slug = f"{_slugify(args.paper_name)}_{args.mode}_{args.paper_format}"
    run_dir = workspace_root / "runs" / run_slug
    script_path = _write_run_script(
        repo_dir=repo_dir,
        run_dir=run_dir,
        mode=args.mode,
        paper_name=args.paper_name,
        paper_format=args.paper_format,
        input_path=input_path,
    )

    payload = {
        "repo_dir": str(repo_dir),
        "run_dir": str(run_dir),
        "run_script": str(script_path),
        "paper_name": args.paper_name,
        "paper_format": args.paper_format,
        "mode": args.mode,
        "input_path": str(input_path),
        "output_dir": str(run_dir / "outputs" / args.paper_name),
        "output_repo_dir": str(run_dir / "outputs" / f"{args.paper_name}_repo"),
        "upstream_repo_url": UPSTREAM_REPO_URL,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
