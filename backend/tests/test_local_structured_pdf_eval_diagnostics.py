from __future__ import annotations

from app.services.local_structured_pdf.eval_diagnostics import compare_evaluation_reports


def test_compare_evaluation_reports_summarizes_lowest_docs_and_deltas():
    baseline = {
        "metrics": {
            "score": {
                "overall_mean": 0.8,
                "nid_mean": 0.81,
                "teds_mean": 0.7,
                "mhs_mean": 0.75,
                "mhs_s_mean": 0.8,
            }
        },
        "documents": [
            {"document_id": "a", "scores": {"overall": 0.5, "nid": 0.4, "teds": None, "mhs": 0.7}},
            {"document_id": "b", "scores": {"overall": 0.9, "nid": 0.95, "teds": 0.7, "mhs": 0.8}},
        ],
    }
    candidate = {
        "metrics": {
            "score": {
                "overall_mean": 0.82,
                "nid_mean": 0.8,
                "teds_mean": 0.76,
                "mhs_mean": 0.79,
                "mhs_s_mean": 0.83,
            }
        },
        "documents": [
            {"document_id": "a", "scores": {"overall": 0.55, "nid": 0.45, "teds": None, "mhs": 0.72}},
            {"document_id": "b", "scores": {"overall": 0.91, "nid": 0.92, "teds": 0.76, "mhs": 0.86}},
        ],
    }

    result = compare_evaluation_reports(
        reports=[("baseline", baseline), ("candidate", candidate)],
        top_k=1,
    )

    assert result["reports"][0]["lowest_by_metric"]["nid"][0]["document_id"] == "a"
    assert result["reports"][0]["weakest_metric_counts"] == {"nid": 1, "teds": 1}
    assert result["deltas"][0]["summary_delta"]["overall_mean"] == 0.019999999999999907
