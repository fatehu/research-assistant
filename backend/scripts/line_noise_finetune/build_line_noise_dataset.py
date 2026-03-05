"""
Build a bootstrap JSONL dataset for line-noise binary classification.

Labels:
  1 -> noisy line (drop)
  0 -> useful line (keep)
"""
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Iterable


def normalize_lines(text: str) -> list[tuple[int, str]]:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    rows: list[tuple[int, str]] = []
    line_id = 1
    for line in raw.split("\n"):
        cleaned = re.sub(r"\s+", " ", line).strip()
        if not cleaned:
            continue
        rows.append((line_id, cleaned))
        line_id += 1
    return rows


def is_hard_noise_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if re.fullmatch(r"(?:page\s*)?\d+(?:\s*/\s*\d+)?", s, re.IGNORECASE):
        return True
    if re.fullmatch(r"[-_=*~·•\s]{4,}", s):
        return True
    if re.fullmatch(r"[a-zA-Z]?\d{4,}", s):
        return True
    if re.fullmatch(r"(?:[a-zA-Z]\d+){1,3}", s):
        return True
    compact = re.sub(r"[^a-zA-Z0-9]+", "", s).lower()
    if compact and len(compact) >= 8 and len(set(compact)) <= 2 and any(ch.isdigit() for ch in compact):
        return True
    return False


def is_clean_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if len(s) < 20:
        return False
    word_count = len(s.split())
    if word_count >= 5 and re.search(r"[A-Za-z\u4e00-\u9fff]", s):
        return True
    if s.endswith((".", "!", "?", "。", "！", "？")) and len(s) >= 16:
        return True
    return False


def iter_input_files(patterns: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for p in patterns:
        path = Path(p)
        if path.exists() and path.is_file():
            files.append(path)
            continue
        for candidate in sorted(Path().glob(p)):
            if candidate.is_file():
                files.append(candidate)
    dedup = []
    seen = set()
    for file in files:
        key = str(file.resolve())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(file)
    return dedup


def main() -> None:
    parser = argparse.ArgumentParser(description="Build bootstrap line-noise dataset JSONL.")
    parser.add_argument(
        "--input",
        nargs="+",
        required=True,
        help="Input text files (path or glob).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSONL path.",
    )
    parser.add_argument(
        "--max-negatives",
        type=int,
        default=5000,
        help="Max number of auto-labeled clean lines (label=0).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for negative sampling.",
    )
    args = parser.parse_args()

    random.seed(int(args.seed))
    files = iter_input_files(args.input)
    if not files:
        raise SystemExit("No input files found.")

    positive_rows: list[dict] = []
    negative_rows: list[dict] = []

    for file in files:
        text = file.read_text(encoding="utf-8", errors="ignore")
        stem = file.stem
        for line_id, line in normalize_lines(text):
            row_id = f"{stem}:{line_id}"
            if is_hard_noise_line(line):
                positive_rows.append(
                    {
                        "id": row_id,
                        "text": line,
                        "label": 1,
                        "source": "heuristic_hard",
                    }
                )
                continue
            if is_clean_line(line):
                negative_rows.append(
                    {
                        "id": row_id,
                        "text": line,
                        "label": 0,
                        "source": "heuristic_clean",
                    }
                )

    if not positive_rows and not negative_rows:
        raise SystemExit("No labeled rows produced. Check input quality.")

    max_neg = max(0, int(args.max_negatives))
    if len(negative_rows) > max_neg > 0:
        negative_rows = random.sample(negative_rows, max_neg)

    output_rows = positive_rows + negative_rows
    random.shuffle(output_rows)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in output_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "output": str(out_path),
                "total": len(output_rows),
                "positives": len(positive_rows),
                "negatives": len(negative_rows),
                "input_files": len(files),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

