import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.reranker_service import RerankerService


@pytest.mark.asyncio
async def test_reranker_predict_uses_configured_batch_size(monkeypatch):
    service = RerankerService()

    class _FakeModel:
        def __init__(self):
            self.calls = []

        def predict(self, pairs, **kwargs):
            self.calls.append({"pairs": pairs, "kwargs": kwargs})
            return [0.2 for _ in pairs]

    fake_model = _FakeModel()
    monkeypatch.setattr(service, "_load_model", lambda: None)
    service._model = fake_model
    service._loaded = True
    monkeypatch.setattr(settings, "reranker_batch_size", 3)

    ranked = await service.rerank(
        query="motif design",
        documents=["doc-a", "doc-b"],
        top_k=2,
    )

    assert ranked == [(0, 0.2), (1, 0.2)]
    assert len(fake_model.calls) == 1
    assert fake_model.calls[0]["kwargs"]["batch_size"] == 3
    assert fake_model.calls[0]["kwargs"]["show_progress_bar"] is False
    assert fake_model.calls[0]["kwargs"]["convert_to_numpy"] is True
