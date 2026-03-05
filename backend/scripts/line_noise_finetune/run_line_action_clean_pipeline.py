"""
Run two-model line pipeline:
1) Action model -> KEEP / REPAIR / DROP
2) Repair-op model -> deterministic cleaning op for REPAIR lines
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


_SPLIT_LETTER_SEQ_RE = re.compile(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b")
_UPPER_FRAG_RE = re.compile(r"\b([A-Z]{3,})\s+([A-Z]{1,3})\b")
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f\u200b\u200c\u200d\ufeff]")

OP_JOIN_FRAGMENTS = "JOIN_FRAGMENTS"
OP_NORMALIZE_SPACES = "NORMALIZE_SPACES"
OP_STRIP_CONTROL = "STRIP_CONTROL"


def _normalize_lines(text: str) -> list[tuple[int, str]]:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    rows: list[tuple[int, str]] = []
    line_id = 1
    for line in raw.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            continue
        rows.append((line_id, cleaned))
        line_id += 1
    return rows


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_control(text: str) -> str:
    return _CONTROL_RE.sub("", str(text or ""))


def _join_split_letters(text: str) -> str:
    s = str(text or "")

    def _collapse_letters(match: re.Match[str]) -> str:
        return re.sub(r"\s+", "", match.group(0))

    s = _SPLIT_LETTER_SEQ_RE.sub(_collapse_letters, s)

    previous = None
    while previous != s:
        previous = s
        s = _UPPER_FRAG_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}", s)
    return s


def _clean_text_by_op(text: str, op_name: str) -> str:
    s = str(text or "")
    if op_name == OP_JOIN_FRAGMENTS:
        s = _join_split_letters(s)
        return _normalize_spaces(s)
    if op_name == OP_NORMALIZE_SPACES:
        return _normalize_spaces(s)
    if op_name == OP_STRIP_CONTROL:
        s = _strip_control(s)
        return _normalize_spaces(s)
    return _normalize_spaces(s)


def _id2label_map(raw_map: dict) -> dict[int, str]:
    output: dict[int, str] = {}
    for k, v in dict(raw_map or {}).items():
        try:
            output[int(k)] = str(v)
        except Exception:
            continue
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Run action+clean line models on a text file.")
    parser.add_argument("--action-model-dir", required=True, help="Trained action classifier directory.")
    parser.add_argument("--clean-model-dir", required=True, help="Trained clean-op classifier directory.")
    parser.add_argument("--input-text", required=True, help="Input extracted text file.")
    parser.add_argument("--output-tsv", required=True, help="Output TSV with line-level decisions.")
    parser.add_argument("--output-text", required=True, help="Output cleaned text file.")
    parser.add_argument("--batch-size", type=int, default=64, help="Inference batch size.")
    args = parser.parse_args()

    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except Exception as exc:
        raise SystemExit(
            "Missing dependencies. Install with: pip install transformers torch"
        ) from exc

    lines = _normalize_lines(Path(args.input_text).read_text(encoding="utf-8", errors="ignore"))
    if not lines:
        raise SystemExit("No non-empty lines in input.")

    action_tok = AutoTokenizer.from_pretrained(args.action_model_dir, use_fast=True)
    action_model = AutoModelForSequenceClassification.from_pretrained(args.action_model_dir)
    action_model.eval()

    clean_tok = AutoTokenizer.from_pretrained(args.clean_model_dir, use_fast=True)
    clean_model = AutoModelForSequenceClassification.from_pretrained(args.clean_model_dir)
    clean_model.eval()

    action_id2label = _id2label_map(getattr(action_model.config, "id2label", {}))
    clean_id2label = _id2label_map(getattr(clean_model.config, "id2label", {}))

    action_texts = [text for _, text in lines]
    action_preds: list[int] = []
    action_scores: list[float] = []

    with torch.no_grad():
        for i in range(0, len(action_texts), int(args.batch_size)):
            chunk = action_texts[i:i + int(args.batch_size)]
            enc = action_tok(chunk, padding=True, truncation=True, max_length=192, return_tensors="pt")
            logits = action_model(**enc).logits
            probs = torch.softmax(logits, dim=-1)
            preds = torch.argmax(probs, dim=-1).tolist()
            confs = torch.max(probs, dim=-1).values.tolist()
            action_preds.extend(int(x) for x in preds)
            action_scores.extend(float(x) for x in confs)

    repair_indices = [
        idx
        for idx, pred in enumerate(action_preds)
        if action_id2label.get(int(pred), str(pred)).upper() == "REPAIR"
    ]
    repair_op_by_index: dict[int, tuple[str, float]] = {}
    if repair_indices:
        repair_texts = [action_texts[idx] for idx in repair_indices]
        clean_preds: list[int] = []
        clean_scores: list[float] = []
        with torch.no_grad():
            for i in range(0, len(repair_texts), int(args.batch_size)):
                chunk = repair_texts[i:i + int(args.batch_size)]
                enc = clean_tok(chunk, padding=True, truncation=True, max_length=192, return_tensors="pt")
                logits = clean_model(**enc).logits
                probs = torch.softmax(logits, dim=-1)
                preds = torch.argmax(probs, dim=-1).tolist()
                confs = torch.max(probs, dim=-1).values.tolist()
                clean_preds.extend(int(x) for x in preds)
                clean_scores.extend(float(x) for x in confs)
        for idx, pred_id, conf in zip(repair_indices, clean_preds, clean_scores):
            op_name = clean_id2label.get(int(pred_id), str(pred_id))
            repair_op_by_index[idx] = (op_name, float(conf))

    out_tsv = Path(args.output_tsv)
    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    output_lines: list[str] = []
    kept_count = 0
    repair_count = 0
    drop_count = 0

    with out_tsv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(
            [
                "line_id",
                "action_label",
                "action_score",
                "clean_op",
                "clean_op_score",
                "original_text",
                "cleaned_text",
            ]
        )

        for idx, ((line_id, text), action_id, action_score) in enumerate(zip(lines, action_preds, action_scores)):
            action_name = action_id2label.get(int(action_id), str(action_id))
            action_upper = action_name.upper()
            op_name = ""
            op_score = 0.0
            cleaned = _normalize_spaces(text)

            if action_upper == "DROP":
                drop_count += 1
            elif action_upper == "REPAIR":
                repair_count += 1
                op_name, op_score = repair_op_by_index.get(idx, ("NOOP", 0.0))
                cleaned = _clean_text_by_op(text, op_name)
                if cleaned:
                    output_lines.append(cleaned)
                    kept_count += 1
            else:
                kept_count += 1
                output_lines.append(cleaned)

            writer.writerow(
                [
                    int(line_id),
                    action_name,
                    f"{float(action_score):.6f}",
                    op_name,
                    f"{float(op_score):.6f}",
                    text,
                    cleaned,
                ]
            )

    out_text = Path(args.output_text)
    out_text.parent.mkdir(parents=True, exist_ok=True)
    out_text.write_text("\n".join(output_lines).strip() + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_tsv": str(out_tsv),
                "output_text": str(out_text),
                "total_lines": len(lines),
                "kept_lines": kept_count,
                "repair_lines": repair_count,
                "drop_lines": drop_count,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
