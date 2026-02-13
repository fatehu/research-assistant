import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import chinese_segmentation_service as seg


def test_segment_text_for_fts_chinese_with_mock_jieba(monkeypatch):
    class _FakeJieba:
        @staticmethod
        def cut(text):
            return ["人工智能", "发展", "趋势"]

    monkeypatch.setattr(seg, "_load_jieba", lambda: _FakeJieba())
    segmented = seg.segment_text_for_fts("人工智能发展趋势")
    assert segmented == "人工智能 发展 趋势"


def test_segment_text_for_fts_english_tokens():
    segmented = seg.segment_text_for_fts("machine-learning systems 2026")
    assert segmented == "machine learning systems 2026"


def test_segment_text_for_fts_empty():
    assert seg.segment_text_for_fts("") == ""


def test_segment_text_for_fts_fallback_when_jieba_unavailable(monkeypatch):
    monkeypatch.setattr(seg, "_load_jieba", lambda: None)
    raw = "中文检索测试"
    assert seg.segment_text_for_fts(raw) == raw

