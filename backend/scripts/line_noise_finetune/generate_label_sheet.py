"""
Generate a TSV label sheet for manual annotation.

Output columns:
  line_id    label    text

label convention:
  1 -> noise (drop)
  0 -> keep
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate manual label TSV for line-noise data.")
    parser.add_argument("--input-text", required=True, help="Input text file.")
    parser.add_argument("--output-tsv", required=True, help="Output TSV path.")
    args = parser.parse_args()

    text = Path(args.input_text).read_text(encoding="utf-8", errors="ignore")
    lines = normalize_lines(text)
    out = Path(args.output_tsv)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["line_id", "label", "text"])
        for line_id, line in lines:
            writer.writerow([line_id, "", line])

    print({"output_tsv": str(out), "total_lines": len(lines)})


if __name__ == "__main__":
    main()

