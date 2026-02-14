import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.mentor import get_mentor_activities


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, result_batches):
        self._batches = list(result_batches)

    async def execute(self, _query):
        if not self._batches:
            return _FakeScalarResult([])
        return _FakeScalarResult(self._batches.pop(0))


@pytest.mark.asyncio
async def test_mentor_activities_merge_and_sort_desc():
    now = datetime.utcnow()
    students = [
        SimpleNamespace(id=11, username="stu1", full_name="学生1", avatar=None, role="student"),
        SimpleNamespace(id=12, username="stu2", full_name="学生2", avatar=None, role="student"),
    ]
    conversations = [
        SimpleNamespace(id=1, title="对话A", user_id=11, updated_at=now - timedelta(hours=2)),
    ]
    notebooks = [
        SimpleNamespace(id="nb-1", title="Notebook B", user_id=12, updated_at=now - timedelta(hours=1)),
    ]
    knowledge_bases = [
        SimpleNamespace(id=21, name="KB C", user_id=11, updated_at=now - timedelta(hours=3)),
    ]
    papers = [
        SimpleNamespace(id=31, title="Paper D", user_id=12, updated_at=now - timedelta(minutes=30)),
    ]
    db = _FakeDB([students, conversations, notebooks, knowledge_bases, papers])
    mentor = SimpleNamespace(id=100)

    items = await get_mentor_activities(skip=0, limit=10, current_user=mentor, db=db)

    assert len(items) == 4
    assert items[0].type == "literature"
    assert items[1].type == "notebook"
    assert items[2].type == "conversation"
    assert items[3].type == "knowledge"
    assert items[0].student.username == "stu2"

