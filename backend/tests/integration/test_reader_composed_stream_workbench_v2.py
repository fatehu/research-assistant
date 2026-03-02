import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.api import literature as literature_api  # noqa: E402
from app.services.literature_reader_compose_service import ReaderComposeBuildMeta  # noqa: E402


class _FakeDB:
    async def execute(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_reader_composed_stream_workbench_v2_done_payload(monkeypatch):
    fake_payload = {
        "paper_id": 78,
        "page": 1,
        "status": "done",
        "degraded_reason": "",
        "pipeline_version": "simplified_v2",
        "engine_version": "reader_compose_v3",
        "source_signature": "sig-demo",
        "build_mode": "compose_agent_simplified",
        "ui_plan": {
            "plan_id": "plan_demo",
            "components": [
                {
                    "id": "n1",
                    "type": "ParagraphProse",
                    "props": {"text": "demo"},
                    "children": [],
                    "source_anchor_refs": [],
                    "source_atom_ids": ["p1:lA:b1"],
                }
            ],
            "layout": {},
            "style_tokens": {},
            "trace_meta": {},
        },
        "assets": [],
        "quality_report": {"overall": 0.9},
        "iteration_trace": [],
        "main_block_ids": ["p1_b1"],
        "aux_block_ids": [],
        "validation_report": {
            "passed": True,
            "gates": {
                "id_integrity": {"passed": True, "errors": []},
                "full_coverage": {"passed": True, "errors": []},
                "whitelist_only": {"passed": True, "errors": []},
                "ownership_unchanged": {"passed": True, "errors": []},
                "non_empty_plan_for_non_empty_input": {"passed": True, "errors": []},
                "source_text_immutable": {"passed": True, "errors": []},
            },
            "errors": [],
        },
        "asset_policy": {},
        "generated_at": "2026-03-02T00:00:00Z",
    }

    class _FakeComposeService:
        async def build_or_get_composed_payload(self, **_kwargs):
            return fake_payload, ReaderComposeBuildMeta(
                cache_hit=False,
                cache_layer="none",
                build_mode="compose_agent_simplified",
                source_signature="sig-demo",
                source_sig_hash="sig-hash",
                iterations=1,
                degraded=False,
                stop_reason="simplified_pipeline",
            )

    async def _fake_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, user_id=1, title="demo")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    response = await literature_api.stream_reader_composed_page(
        paper_id=78,
        payload=SimpleNamespace(
            page=1,
            selected_kb_id=None,
            force_refresh=False,
            regenerate=False,
            latency_budget_ms=None,
            quality_target=None,
            style_intent=None,
            theme_mode="light",
            detail_level="standard",
            compare_mode=False,
            citation_tldr=False,
        ),
        request=_FakeRequest(),
        db=_FakeDB(),
        current_user=SimpleNamespace(id=1),
    )

    done_payload = None
    async for chunk in response.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if str(event.get("event") or "") == "done":
                done_payload = dict(event.get("data") or {})
                break
        if done_payload is not None:
            break

    assert isinstance(done_payload, dict)
    assert str(done_payload.get("status") or "") == "done"
    assert str(done_payload.get("degraded_reason") or "") == ""
    assert isinstance(done_payload.get("validation_report"), dict)
    payload = dict(done_payload.get("payload") or {})
    assert str(payload.get("pipeline_version") or "") == "simplified_v2"
    assert str(payload.get("status") or "") == "done"
    assert str(payload.get("degraded_reason") or "") == ""
    assert isinstance(payload.get("validation_report"), dict)
    assert list(payload.get("main_block_ids") or []) == ["p1_b1"]
    assert list(payload.get("aux_block_ids") or []) == []
    assert len(list((payload.get("ui_plan") or {}).get("components") or [])) == 1


@pytest.mark.asyncio
async def test_reader_composed_inline_query_disabled_contract(monkeypatch):
    class _FakeComposeService:
        async def build_inline_answer_card(self, **_kwargs):
            return {
                "disabled": True,
                "disabled_reason": "inline_query_missing_source_anchor_refs",
                "message": "Inline query source anchors are required.",
            }

    async def _fake_owned(_db, _user, _paper_id):
        return SimpleNamespace(id=78, user_id=1, title="demo")

    monkeypatch.setattr(literature_api, "_get_owned_paper_or_404", _fake_owned)
    monkeypatch.setattr(literature_api, "get_literature_reader_compose_service", lambda: _FakeComposeService())

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    response = await literature_api.stream_reader_composed_inline_query(
        paper_id=78,
        payload=SimpleNamespace(
            page=1,
            node_id="n1",
            question="what is this",
            scope="section",
            selected_kb_id=None,
            style_intent=None,
            theme_mode="light",
            detail_level="standard",
            compare_mode=False,
            citation_tldr=False,
        ),
        request=_FakeRequest(),
        db=_FakeDB(),
        current_user=SimpleNamespace(id=1),
    )

    disabled_event = None
    done_event = None
    async for chunk in response.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if str(event.get("event") or "") == "disabled":
                disabled_event = dict(event.get("data") or {})
            if str(event.get("event") or "") == "done":
                done_event = dict(event.get("data") or {})
        if disabled_event is not None and done_event is not None:
            break

    assert isinstance(disabled_event, dict)
    assert bool(disabled_event.get("disabled")) is True
    assert str(disabled_event.get("disabled_reason") or "") == "inline_query_missing_source_anchor_refs"
    assert isinstance(done_event, dict)
    assert bool(done_event.get("disabled")) is True

