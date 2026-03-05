"""
Fine-tune a line classifier with Hugging Face Trainer.

Input JSONL rows must contain:
  - text: str
  - label: int (0..N-1 or sparse non-negative ints)
Optional fields:
  - label_name: str
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
from typing import Any


def _require_training_deps() -> None:
    try:
        import datasets  # noqa: F401
        import transformers  # noqa: F401
        import sklearn  # noqa: F401
    except Exception as exc:
        raise SystemExit(
            "Missing training dependencies. Install with: "
            "pip install transformers datasets evaluate accelerate scikit-learn torch"
        ) from exc


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], dict[int, str]]:
    rows: list[dict[str, Any]] = []
    label_name_map: dict[int, str] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            text = str(obj.get("text") or "").strip()
            try:
                label_raw = int(obj.get("label", -1))
            except Exception:
                continue
            if not text or label_raw < 0:
                continue
            label_name = str(obj.get("label_name") or f"label_{label_raw}").strip() or f"label_{label_raw}"
            label_name_map[label_raw] = label_name
            rows.append({"text": text, "label_raw": label_raw})
    return rows, label_name_map


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a line text classifier.")
    parser.add_argument("--train-jsonl", required=True, help="Input JSONL with text+label.")
    parser.add_argument("--output-dir", required=True, help="Output model directory.")
    parser.add_argument(
        "--model-name",
        default="distilbert-base-multilingual-cased",
        help="HF model name for sequence classification base.",
    )
    parser.add_argument("--epochs", type=float, default=2.0, help="Training epochs.")
    parser.add_argument("--batch-size", type=int, default=16, help="Train/eval batch size.")
    parser.add_argument("--lr", type=float, default=2e-5, help="Learning rate.")
    parser.add_argument("--max-length", type=int, default=192, help="Tokenizer max length.")
    parser.add_argument("--eval-ratio", type=float, default=0.1, help="Validation ratio.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    args = parser.parse_args()

    _require_training_deps()

    from datasets import Dataset
    import numpy as np
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
        DataCollatorWithPadding,
        Trainer,
        TrainingArguments,
    )

    rows, label_name_map_raw = _load_jsonl(Path(args.train_jsonl))
    if len(rows) < 20:
        raise SystemExit("Dataset too small. Need at least 20 valid rows.")

    raw_labels = sorted({int(r["label_raw"]) for r in rows})
    if len(raw_labels) < 2:
        raise SystemExit("Need at least 2 distinct labels.")
    raw_to_new = {raw: idx for idx, raw in enumerate(raw_labels)}
    id2label = {
        idx: label_name_map_raw.get(raw, f"label_{raw}")
        for raw, idx in raw_to_new.items()
    }
    label2id = {v: k for k, v in id2label.items()}

    normalized_rows = [
        {"text": str(r["text"]), "label": int(raw_to_new[int(r["label_raw"])])}
        for r in rows
    ]

    labels = [r["label"] for r in normalized_rows]
    train_rows, eval_rows = train_test_split(
        normalized_rows,
        test_size=max(0.05, min(0.5, float(args.eval_ratio))),
        random_state=int(args.seed),
        stratify=labels,
    )

    train_ds = Dataset.from_list(train_rows)
    eval_ds = Dataset.from_list(eval_rows)

    tokenizer = AutoTokenizer.from_pretrained(args.model_name, use_fast=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name,
        num_labels=len(id2label),
        id2label=id2label,
        label2id=label2id,
    )

    def preprocess(batch):
        return tokenizer(
            batch["text"],
            truncation=True,
            max_length=int(args.max_length),
        )

    train_ds = train_ds.map(preprocess, batched=True)
    eval_ds = eval_ds.map(preprocess, batched=True)

    def compute_metrics(eval_pred):
        logits, labels_arr = eval_pred
        preds = np.argmax(logits, axis=-1)
        result: dict[str, float] = {
            "accuracy": float(accuracy_score(labels_arr, preds)),
        }
        if len(id2label) == 2:
            result["f1"] = float(f1_score(labels_arr, preds, average="binary", zero_division=0))
            result["precision"] = float(precision_score(labels_arr, preds, average="binary", zero_division=0))
            result["recall"] = float(recall_score(labels_arr, preds, average="binary", zero_division=0))
        else:
            result["f1"] = float(f1_score(labels_arr, preds, average="macro", zero_division=0))
            result["precision"] = float(precision_score(labels_arr, preds, average="macro", zero_division=0))
            result["recall"] = float(recall_score(labels_arr, preds, average="macro", zero_division=0))
            result["f1_weighted"] = float(f1_score(labels_arr, preds, average="weighted", zero_division=0))
        return result

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    training_kwargs = {
        "output_dir": str(out_dir),
        "learning_rate": float(args.lr),
        "per_device_train_batch_size": int(args.batch_size),
        "per_device_eval_batch_size": int(args.batch_size),
        "num_train_epochs": float(args.epochs),
        "save_strategy": "epoch",
        "load_best_model_at_end": True,
        "metric_for_best_model": "f1",
        "greater_is_better": True,
        "logging_steps": 20,
        "report_to": [],
        "seed": int(args.seed),
    }
    ta_sig = inspect.signature(TrainingArguments.__init__)
    if "evaluation_strategy" in ta_sig.parameters:
        training_kwargs["evaluation_strategy"] = "epoch"
    else:
        training_kwargs["eval_strategy"] = "epoch"

    training_args = TrainingArguments(**training_kwargs)

    trainer_kwargs = {
        "model": model,
        "args": training_args,
        "train_dataset": train_ds,
        "eval_dataset": eval_ds,
        "data_collator": DataCollatorWithPadding(tokenizer=tokenizer),
        "compute_metrics": compute_metrics,
    }
    trainer_sig = inspect.signature(Trainer.__init__)
    if "tokenizer" in trainer_sig.parameters:
        trainer_kwargs["tokenizer"] = tokenizer
    elif "processing_class" in trainer_sig.parameters:
        # transformers>=5 replaces tokenizer arg with processing_class.
        trainer_kwargs["processing_class"] = tokenizer

    trainer = Trainer(**trainer_kwargs)

    trainer.train()
    eval_metrics = trainer.evaluate()

    trainer.save_model(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))

    summary = {
        "model_name": args.model_name,
        "output_dir": str(out_dir),
        "num_labels": len(id2label),
        "raw_to_new_label_map": {str(k): int(v) for k, v in raw_to_new.items()},
        "id2label": {str(k): str(v) for k, v in id2label.items()},
        "train_rows": len(train_rows),
        "eval_rows": len(eval_rows),
        "epochs": float(args.epochs),
        "batch_size": int(args.batch_size),
        "learning_rate": float(args.lr),
        "metrics": {k: float(v) for k, v in eval_metrics.items() if isinstance(v, (int, float))},
    }
    (out_dir / "training_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
