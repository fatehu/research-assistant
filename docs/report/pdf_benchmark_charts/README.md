# PDF Benchmark Charts

Generated from local `opendataloader-bench` `evaluation.json` files.

Included reports are full 200-document runs with zero failed documents and zero missing predictions.

## Charts

- [README-style overview](readme_benchmark_overview.svg)
- [Quality comparison](quality_comparison.svg)
- [Speed comparison](speed_comparison.svg)
- [local-structured-pdf vs opendataloader delta](local-structured-pdf_vs_opendataloader_delta.svg)

## Summary

| Engine | Docs | Overall | Reading order | Table | Heading | Seconds/doc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| opendataloader-hybrid | 200 | 0.909 | 0.935 | 0.928 | 0.828 | 0.433 |
| docling | 200 | 0.877 | 0.899 | 0.887 | 0.802 | 0.725 |
| marker | 200 | 0.861 | 0.890 | 0.808 | 0.796 | 53.932 |
| local-structured-pdf | 200 | 0.848 | 0.903 | 0.596 | 0.769 | 0.509 |
| opendataloader | 200 | 0.844 | 0.913 | 0.494 | 0.761 | 0.055 |
| mineru | 200 | 0.831 | 0.857 | 0.873 | 0.743 | 5.962 |
| pymupdf4llm | 200 | 0.732 | 0.885 | 0.401 | 0.412 | 0.091 |
| markitdown | 200 | 0.583 | 0.879 | 0.000 | 0.000 | 0.041 |

## Notes

- These charts compare local recorded runs, not the live upstream leaderboard.
- `online_mm_eval` is excluded by default because the local record only covers 2 documents.
- Raw generated benchmark outputs under `backend/eval/results/**` are ignored by Git; this report is the portable summary.
