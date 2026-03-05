"""
Run inference with a trained line-noise classifier on a text file.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


def _require_infer_deps() -> None:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except Exception as exc:
        raise SystemExit(
            "Missing inference dependencies. Install with: "
            "pip install transformers torch"
        ) from exc


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
    parser = argparse.ArgumentParser(description="Evaluate line-noise classifier on text file.")
    parser.add_argument("--model-dir", required=True, help="Path to trained model directory.")
    parser.add_argument("--input-text", required=True, help="Input text file.")
    parser.add_argument("--output-tsv", required=True, help="Output TSV predictions.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Noise probability threshold for label=1.",
    )
    args = parser.parse_args()

    _require_infer_deps()
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    model_dir = Path(args.model_dir)
    input_text = Path(args.input_text).read_text(encoding="utf-8", errors="ignore")
    lines = normalize_lines(input_text)
    if not lines:
        raise SystemExit("No non-empty lines in input text.")

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), use_fast=True)
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.eval()

    out_path = Path(args.output_tsv)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["line_id", "pred_label", "score_noise", "text"])

        batch_size = 64
        threshold = float(args.threshold)
        noise_count = 0
        for i in range(0, len(lines), batch_size):
            batch = lines[i:i + batch_size]
            batch_texts = [x[1] for x in batch]
            enc = tokenizer(batch_texts, padding=True, truncation=True, max_length=192, return_tensors="pt")
            with torch.no_grad():
                logits = model(**enc).logits
                probs = torch.softmax(logits, dim=-1)
                noise_scores = probs[:, 1].cpu().numpy().tolist()
            for (line_id, text), score in zip(batch, noise_scores):
                label = 1 if score >= threshold else 0
                if label == 1:
                    noise_count += 1
                writer.writerow([line_id, label, f"{float(score):.6f}", text])

    print(
        {
            "output_tsv": str(out_path),
            "total_lines": len(lines),
            "noise_predicted": noise_count,
            "threshold": float(args.threshold),
        }
    )


if __name__ == "__main__":
    main()
