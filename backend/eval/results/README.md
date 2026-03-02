# Reader Eval Results

Evaluation artifacts are written to:

- `backend/eval/results/<ts>/summary.json`
- `backend/eval/results/<ts>/per_page_detail.json`
- `backend/eval/results/<ts>/report.md`

Use:

```bash
python backend/scripts/reader_eval/run_simplified_eval.py \
  --manifest backend/eval/manifests/reader_simplified_eval_v1.json \
  --out backend/eval/results/{ts}
```

For live pipeline evidence:

```bash
python backend/scripts/reader_eval/run_simplified_eval.py \
  --live \
  --base-url http://localhost:8888 \
  --manifest backend/eval/manifests/reader_simplified_eval_v1.json \
  --out backend/eval/results/{ts}
```
