"""
Build two datasets for PDF line processing:

1) Action classifier dataset (KEEP / REPAIR / DROP)
2) Repair-op classifier dataset (which deterministic cleaner to apply)
"""
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Iterable

ACTION_KEEP = 0
ACTION_REPAIR = 1
ACTION_DROP = 2

OP_JOIN_FRAGMENTS = 0
OP_NORMALIZE_SPACES = 1
OP_STRIP_CONTROL = 2
OP_NOOP = 3

ACTION_LABEL_NAMES = {
    ACTION_KEEP: "KEEP",
    ACTION_REPAIR: "REPAIR",
    ACTION_DROP: "DROP",
}

OP_LABEL_NAMES = {
    OP_JOIN_FRAGMENTS: "JOIN_FRAGMENTS",
    OP_NORMALIZE_SPACES: "NORMALIZE_SPACES",
    OP_STRIP_CONTROL: "STRIP_CONTROL",
    OP_NOOP: "NOOP",
}

_SPLIT_LETTER_SEQ_RE = re.compile(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b")
_UPPER_FRAG_RE = re.compile(r"\b([A-Z]{3,})\s+([A-Z]{1,3})\b")
_NOISE_REPEAT_RE = re.compile(r"([a-zA-Z])\1{4,}|([0-9])\2{4,}", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f\u200b\u200c\u200d\ufeff]")


def normalize_lines(text: str) -> list[tuple[int, str]]:
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


def iter_input_files(patterns: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for item in patterns:
        p = Path(item)
        if p.exists() and p.is_file():
            files.append(p)
            continue
        for candidate in sorted(Path().glob(item)):
            if candidate.is_file():
                files.append(candidate)
    seen: set[str] = set()
    dedup: list[Path] = []
    for f in files:
        key = str(f.resolve())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(f)
    return dedup


def _strip_control(text: str) -> str:
    return _CONTROL_RE.sub("", str(text or ""))


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


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


def clean_with_op(text: str, op_label: int) -> str:
    s = str(text or "")
    if op_label == OP_JOIN_FRAGMENTS:
        s = _join_split_letters(s)
        s = _normalize_spaces(s)
        return s
    if op_label == OP_NORMALIZE_SPACES:
        return _normalize_spaces(s)
    if op_label == OP_STRIP_CONTROL:
        s = _strip_control(s)
        s = _normalize_spaces(s)
        return s
    return _normalize_spaces(s)


def _is_mostly_symbols_or_digits(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    alpha = sum(1 for ch in s if ch.isalpha())
    digit = sum(1 for ch in s if ch.isdigit())
    punct = sum(1 for ch in s if not ch.isalnum() and not ch.isspace())
    n = max(1, len(s))
    if alpha == 0 and (digit + punct) / n >= 0.65:
        return True
    if digit / n > 0.55 and alpha / n < 0.25:
        return True
    return False


def is_hard_drop_line(text: str) -> bool:
    s = _normalize_spaces(text)
    if not s:
        return True
    if s == "\ufffd":
        return True
    if re.fullmatch(r"(?:page\s*)?\d+(?:\s*/\s*\d+)?", s, re.IGNORECASE):
        return True
    if re.fullmatch(r"[-_=*~·•\s]{2,}", s):
        return True
    if re.fullmatch(r"[a-zA-Z]?\d{4,}", s):
        return True
    if re.fullmatch(r"(?:[a-zA-Z]\d+){1,3}", s):
        return True
    if _NOISE_REPEAT_RE.search(s):
        return True
    if "�" in s and len(s) <= 6:
        return True
    if _is_mostly_symbols_or_digits(s):
        return True
    return False


def _detect_repair_op(text: str) -> tuple[int, str]:
    original = str(text or "")
    compact = _normalize_spaces(original)
    if compact != original and not _SPLIT_LETTER_SEQ_RE.search(original):
        return OP_NORMALIZE_SPACES, clean_with_op(original, OP_NORMALIZE_SPACES)
    if _SPLIT_LETTER_SEQ_RE.search(compact) or _UPPER_FRAG_RE.search(compact):
        cleaned = clean_with_op(compact, OP_JOIN_FRAGMENTS)
        if cleaned != compact:
            return OP_JOIN_FRAGMENTS, cleaned
    if _CONTROL_RE.search(original):
        cleaned = clean_with_op(original, OP_STRIP_CONTROL)
        if cleaned != original:
            return OP_STRIP_CONTROL, cleaned
    return OP_NOOP, _normalize_spaces(original)


def _to_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _augment_split_letters(text: str) -> str:
    words = [w for w in text.split() if len(w) >= 6 and w.isalpha()]
    if not words:
        return text
    target = random.choice(words)
    split = " ".join(list(target))
    return text.replace(target, split, 1)


def _augment_extra_spaces(text: str) -> str:
    parts = text.split()
    if len(parts) < 2:
        return text
    idx = random.randint(0, len(parts) - 2)
    parts[idx] = parts[idx] + "   "
    return " ".join(parts)


def _augment_control_char(text: str) -> str:
    if not text:
        return text
    pos = random.randint(1, max(1, len(text) - 1))
    return text[:pos] + "\u200b" + text[pos:]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build datasets for action+repair line models.")
    parser.add_argument("--input", nargs="+", required=True, help="Input text files or globs.")
    parser.add_argument("--action-output", required=True, help="Output JSONL for action classifier.")
    parser.add_argument("--clean-output", required=True, help="Output JSONL for repair-op classifier.")
    parser.add_argument("--max-keep", type=int, default=9000, help="Max KEEP rows for action dataset.")
    parser.add_argument("--augment-repair", type=int, default=1800, help="Synthetic REPAIR rows.")
    parser.add_argument("--augment-drop", type=int, default=800, help="Synthetic DROP rows.")
    parser.add_argument("--clean-noop", type=int, default=1200, help="NOOP rows in clean dataset.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    args = parser.parse_args()

    random.seed(int(args.seed))
    files = iter_input_files(args.input)
    if not files:
        raise SystemExit("No input files found.")

    action_keep: list[dict] = []
    action_repair: list[dict] = []
    action_drop: list[dict] = []
    clean_rows: list[dict] = []

    for file in files:
        stem = file.stem
        text = file.read_text(encoding="utf-8", errors="ignore")
        for line_id, line in normalize_lines(text):
            norm = _normalize_spaces(line)
            row_id = f"{stem}:{line_id}"
            if is_hard_drop_line(norm):
                action_drop.append(
                    {
                        "id": row_id,
                        "text": norm,
                        "label": ACTION_DROP,
                        "label_name": ACTION_LABEL_NAMES[ACTION_DROP],
                        "source": "heuristic_drop",
                    }
                )
                continue

            op_label, cleaned = _detect_repair_op(norm)
            if op_label != OP_NOOP and cleaned and cleaned != norm:
                action_repair.append(
                    {
                        "id": row_id,
                        "text": norm,
                        "label": ACTION_REPAIR,
                        "label_name": ACTION_LABEL_NAMES[ACTION_REPAIR],
                        "source": "heuristic_repair",
                    }
                )
                clean_rows.append(
                    {
                        "id": row_id,
                        "text": norm,
                        "target_text": cleaned,
                        "label": op_label,
                        "label_name": OP_LABEL_NAMES[op_label],
                        "source": "heuristic_repair",
                    }
                )
                continue

            action_keep.append(
                {
                    "id": row_id,
                    "text": norm,
                    "label": ACTION_KEEP,
                    "label_name": ACTION_LABEL_NAMES[ACTION_KEEP],
                    "source": "heuristic_keep",
                }
            )

    if len(action_keep) > max(0, int(args.max_keep)):
        action_keep = random.sample(action_keep, int(args.max_keep))

    keep_pool = [r["text"] for r in action_keep if len(r["text"]) >= 20]
    for idx in range(max(0, int(args.augment_repair))):
        if not keep_pool:
            break
        base = random.choice(keep_pool)
        mode = idx % 3
        if mode == 0:
            noisy = _augment_split_letters(base)
            op_label = OP_JOIN_FRAGMENTS
        elif mode == 1:
            noisy = _augment_extra_spaces(base)
            op_label = OP_NORMALIZE_SPACES
        else:
            noisy = _augment_control_char(base)
            op_label = OP_STRIP_CONTROL
        noisy = str(noisy or "").strip()
        target = _normalize_spaces(base)
        if not noisy or noisy == target:
            continue
        sid = f"synthetic_repair:{idx + 1}"
        action_repair.append(
            {
                "id": sid,
                "text": noisy,
                "label": ACTION_REPAIR,
                "label_name": ACTION_LABEL_NAMES[ACTION_REPAIR],
                "source": "synthetic_repair",
            }
        )
        clean_rows.append(
            {
                "id": sid,
                "text": noisy,
                "target_text": target,
                "label": op_label,
                "label_name": OP_LABEL_NAMES[op_label],
                "source": "synthetic_repair",
            }
        )

    synthetic_drop_seeds = [
        "a1111111111",
        "***",
        "------",
        "1 / 12",
        "32424212",
        "�",
        "0 0 0 0 0",
        "x999999",
    ]
    for idx in range(max(0, int(args.augment_drop))):
        text = synthetic_drop_seeds[idx % len(synthetic_drop_seeds)]
        action_drop.append(
            {
                "id": f"synthetic_drop:{idx + 1}",
                "text": text,
                "label": ACTION_DROP,
                "label_name": ACTION_LABEL_NAMES[ACTION_DROP],
                "source": "synthetic_drop",
            }
        )

    for idx in range(max(0, int(args.clean_noop))):
        if not keep_pool:
            break
        text = _normalize_spaces(random.choice(keep_pool))
        clean_rows.append(
            {
                "id": f"synthetic_noop:{idx + 1}",
                "text": text,
                "target_text": text,
                "label": OP_NOOP,
                "label_name": OP_LABEL_NAMES[OP_NOOP],
                "source": "synthetic_noop",
            }
        )

    action_rows = action_keep + action_repair + action_drop
    random.shuffle(action_rows)
    random.shuffle(clean_rows)

    action_out = Path(args.action_output)
    clean_out = Path(args.clean_output)
    _to_jsonl(action_out, action_rows)
    _to_jsonl(clean_out, clean_rows)

    action_counts = {name: 0 for name in ACTION_LABEL_NAMES.values()}
    for row in action_rows:
        action_counts[str(row["label_name"])] = action_counts.get(str(row["label_name"]), 0) + 1

    clean_counts = {name: 0 for name in OP_LABEL_NAMES.values()}
    for row in clean_rows:
        clean_counts[str(row["label_name"])] = clean_counts.get(str(row["label_name"]), 0) + 1

    print(
        json.dumps(
            {
                "input_files": len(files),
                "action_output": str(action_out),
                "action_total": len(action_rows),
                "action_counts": action_counts,
                "clean_output": str(clean_out),
                "clean_total": len(clean_rows),
                "clean_counts": clean_counts,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
