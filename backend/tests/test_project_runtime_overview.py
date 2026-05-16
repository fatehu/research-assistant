from types import SimpleNamespace

import pytest

from app.services.project_service import ProjectService


class _AsyncExecuteCommitDB:
    def __init__(self):
        self.executed = []
        self.committed = False

    async def execute(self, statement):
        self.executed.append(statement)
        return None

    async def commit(self):
        self.committed = True
        return None


@pytest.mark.asyncio
async def test_get_project_folder_tree_returns_project_root_tree(tmp_path, monkeypatch):
    service = ProjectService(db=None)
    project_dir = tmp_path / "projects" / "7"
    (project_dir / "reference" / "paper").mkdir(parents=True, exist_ok=True)
    (project_dir / "reference" / "paper" / "paper_interpretation.md").write_text("# summary", encoding="utf-8")

    async def _fake_get_project(*, project_id: int, user_id: int):
        return SimpleNamespace(id=project_id, user_id=user_id)

    monkeypatch.setattr(service, "get_project", _fake_get_project)
    monkeypatch.setattr(
        "app.services.project_service.get_project_root_dir",
        lambda project_id, ensure_exists=False: project_dir,
    )

    payload = await service.get_project_folder_tree(project_id=7, user_id=1)

    assert payload is not None
    assert payload["project_root"] == str(project_dir)
    assert payload["exists"] is True
    assert "reference/" in payload["tree"]
    assert "paper_interpretation.md" in payload["tree"]


@pytest.mark.asyncio
async def test_delete_project_removes_project_row_and_directory(tmp_path, monkeypatch):
    db = _AsyncExecuteCommitDB()
    service = ProjectService(db=db)
    project_dir = tmp_path / "projects" / "9"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "reference.json").write_text("{}", encoding="utf-8")

    async def _fake_get_project(*, project_id: int, user_id: int):
        return SimpleNamespace(id=project_id, user_id=user_id)

    monkeypatch.setattr(service, "get_project", _fake_get_project)
    monkeypatch.setattr(
        "app.services.project_service.get_project_root_dir",
        lambda project_id, ensure_exists=False: project_dir,
    )

    payload = await service.delete_project(project_id=9, user_id=1)

    assert payload is not None
    assert payload["deleted"] is True
    assert payload["deleted_project_root"] is True
    assert payload["project_root"] == str(project_dir)
    assert db.committed is True
    assert len(db.executed) == 3
    assert not project_dir.exists()
