# Line Noise Classifier Fine-tuning

This folder contains a minimal training pipeline for a binary line-noise classifier:

- `label=1`: noisy line (drop)
- `label=0`: useful line (keep)

The pipeline is designed for PDF/OCR line filtering before chunking.

## 1) Build a bootstrap dataset

Use heuristic labeling to create an initial dataset from extracted text files:

```powershell
python scripts/line_noise_finetune/build_line_noise_dataset.py `
  --input "C:\Users\yui\Desktop\journal.pdig.0000198_pypdf_cleaned_v2.txt" `
  --output "tmp\line_noise_train.jsonl" `
  --max-negatives 2000
```

Output JSONL format:

```json
{"id":"journal.pdig.0000198_pypdf_cleaned_v2:49","text":"a1111111111","label":1,"source":"heuristic_hard"}
{"id":"journal.pdig.0000198_pypdf_cleaned_v2:120","text":"This study evaluates model performance.","label":0,"source":"heuristic_clean"}
```

Optional: create a manual annotation sheet first:

```powershell
python scripts/line_noise_finetune/generate_label_sheet.py `
  --input-text "C:\Users\yui\Desktop\journal.pdig.0000198_pypdf_cleaned_v2.txt" `
  --output-tsv "tmp\line_noise_manual.tsv"
```

## 2) Train a classifier (small encoder fine-tune)

Install optional training dependencies first:

```powershell
pip install transformers datasets evaluate accelerate scikit-learn torch
```

Run training:

```powershell
python scripts/line_noise_finetune/train_line_noise_classifier.py `
  --train-jsonl "tmp\line_noise_train.jsonl" `
  --output-dir "tmp\line-noise-model" `
  --model-name "distilbert-base-multilingual-cased" `
  --epochs 2 `
  --batch-size 16
```

Artifacts:

- `tmp/line-noise-model/` model weights/tokenizer
- `tmp/line-noise-model/training_summary.json`

## 3) Evaluate or run inference on a text file

```powershell
python scripts/line_noise_finetune/eval_line_noise_classifier.py `
  --model-dir "tmp\line-noise-model" `
  --input-text "C:\Users\yui\Desktop\journal.pdig.0000198_pypdf_cleaned_v2.txt" `
  --output-tsv "tmp\line_noise_pred.tsv"
```

TSV output columns:

- `line_id`
- `pred_label` (`0/1`)
- `score_noise`
- `text`

## Notes

- The bootstrap dataset is only a starting point. For robust production quality, add manual labels.
- Keep a held-out validation set from different PDFs.
- Favor high recall on `label=1` (noise) only if downstream has a safe recovery path.
