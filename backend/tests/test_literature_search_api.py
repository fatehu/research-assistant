import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import literature as literature_api
from app.models.literature import Paper
from app.models.literature import PaperSearchHistory
from app.services.literature_service import OpenAlexService, PaperResult


class _FakeResult:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def all(self):
        return list(self._rows)

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self


class _SearchDB:
    def __init__(self, results):
        self._results = list(results)
        self.execute_calls = 0
        self.added = []
        self.committed = False

    async def execute(self, _query):
        self.execute_calls += 1
        if not self._results:
            return _FakeResult(rows=[])
        return self._results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.committed = True


class _SaveDB:
    def __init__(self, results, *, flush_error=None):
        self._results = list(results)
        self.execute_calls = 0
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.flush_error = flush_error

    async def execute(self, _query):
        self.execute_calls += 1
        if not self._results:
            return _FakeResult(row=None)
        return self._results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        if self.flush_error is not None:
            raise self.flush_error
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = 321

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, paper):
        now = datetime.utcnow()
        paper.created_at = now
        paper.updated_at = now
        paper.pdf_downloaded = False
        paper.is_read = False
        paper.influential_citation_count = 0


class _ImportDB:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


def _paper_result(
    *,
    source: str,
    external_id: str,
    title: str,
    doi: str | None = None,
    arxiv_id: str | None = None,
) -> PaperResult:
    return PaperResult(
        source=source,
        external_id=external_id,
        title=title,
        abstract="abstract",
        authors=[{"name": "Author"}],
        year=2024,
        venue="venue",
        citation_count=12,
        reference_count=3,
        url=f"https://example.com/{external_id}",
        pdf_url=None,
        arxiv_id=arxiv_id,
        doi=doi,
        fields_of_study=["AI"],
        raw_data={},
    )


def _saved_paper(*, paper_id: int, user_id: int, source: str, title: str, doi: str | None = None) -> Paper:
    now = datetime.utcnow()
    paper = Paper(
        id=paper_id,
        user_id=user_id,
        semantic_scholar_id=None,
        arxiv_id=None,
        doi=doi,
        pubmed_id=None,
        title=title,
        abstract="abstract",
        authors=[{"name": "Author"}],
        year=2024,
        venue="venue",
        citation_count=12,
        reference_count=3,
        influential_citation_count=0,
        url="https://example.com/paper",
        pdf_url=None,
        arxiv_url=None,
        pdf_path=None,
        pdf_downloaded=False,
        knowledge_base_id=None,
        document_id=None,
        fields_of_study=["AI"],
        tags=[],
        is_read=False,
        read_at=None,
        notes=None,
        rating=None,
        source=source,
        raw_data={},
        published_date=None,
        created_at=now,
        updated_at=now,
    )
    return paper


@pytest.mark.asyncio
async def test_search_papers_multi_uses_fused_service_and_batches_saved_lookup():
    class _FakeService:
        def __init__(self):
            self.multi_calls = 0
            self.search_calls = 0

        async def search_multi(self, **kwargs):
            self.multi_calls += 1
            assert kwargs["limit_per_source"] == 5
            assert kwargs["offset"] == 0
            return {
                "total": 24,
                "offset": 0,
                "has_more": True,
                "papers": [
                    _paper_result(source="semantic_scholar", external_id="s2-1", title="Semantic Paper"),
                    _paper_result(source="pubmed", external_id="pm-42", title="PubMed Paper", doi="10.1000/pm42"),
                ],
            }

        async def search(self, **kwargs):
            self.search_calls += 1
            return {"total": 0, "papers": []}

    db = _SearchDB(
        results=[
            _FakeResult(
                rows=[
                    (99, None, None, "pm-42", "10.1000/pm42", "PubMed Paper"),
                ]
            )
        ]
    )
    service = _FakeService()

    original_get_service = literature_api.get_literature_service
    literature_api.get_literature_service = lambda: service
    try:
        response = await literature_api.search_papers(
            query="transformer",
            source="multi",
            limit=5,
            offset=0,
            year_start=None,
            year_end=None,
            fields=None,
            open_access=False,
            db=db,
            current_user=SimpleNamespace(id=7),
        )
    finally:
        literature_api.get_literature_service = original_get_service

    assert service.multi_calls == 1
    assert service.search_calls == 0
    assert db.execute_calls == 1
    assert db.committed is True
    assert response.source == "multi"
    assert response.has_more is True
    assert response.total == 24
    assert response.papers[1].is_saved is True
    assert response.papers[1].saved_paper_id == 99
    assert len(db.added) == 1
    assert isinstance(db.added[0], PaperSearchHistory)
    assert db.added[0].source == "multi"


@pytest.mark.asyncio
async def test_save_paper_persists_pubmed_identifier(monkeypatch):
    db = _SaveDB(
        results=[
            _FakeResult(row=None),
            _FakeResult(row=None),
        ]
    )

    async def _fake_ensure_paper_entity(_db, paper):
        paper.paper_entity_id = 11
        return SimpleNamespace(id=11)

    monkeypatch.setattr(literature_api, "_ensure_paper_entity", _fake_ensure_paper_entity)

    response = await literature_api.save_paper(
        request=literature_api.SavePaperFromSearchRequest(
            source="pubmed",
            external_id="pmid-123",
            title="PubMed Indexed Paper",
            abstract="abstract",
            authors=[{"name": "Author"}],
            doi="10.1000/pubmed",
            fields_of_study=["Medicine"],
        ),
        db=db,
        current_user=SimpleNamespace(id=5),
    )

    saved_paper = db.added[0]
    assert saved_paper.pubmed_id == "pmid-123"
    assert saved_paper.semantic_scholar_id is None
    assert saved_paper.source == "pubmed"
    assert response.source == "pubmed"
    assert response.doi == "10.1000/pubmed"


@pytest.mark.asyncio
async def test_save_paper_returns_existing_paper_idempotently(monkeypatch):
    db = _SaveDB(results=[])
    existing = _saved_paper(
        paper_id=88,
        user_id=5,
        source="semantic_scholar",
        title="Already Saved Paper",
    )
    existing.semantic_scholar_id = "s2-existing"
    added_calls = []

    async def _fake_find_existing(_db, *, user_id, request):
        assert user_id == 5
        assert request.external_id == "s2-existing"
        return existing

    async def _fake_add_to_collections(_db, *, paper_id, user_id, collection_ids):
        added_calls.append((paper_id, user_id, list(collection_ids)))
        return [7]

    async def _fake_load_collection_ids(_db, paper_id):
        assert paper_id == 88
        return [2, 7]

    monkeypatch.setattr(literature_api, "_find_existing_paper_for_request", _fake_find_existing)
    monkeypatch.setattr(literature_api, "_add_paper_to_collections_if_missing", _fake_add_to_collections)
    monkeypatch.setattr(literature_api, "_load_collection_ids_for_paper", _fake_load_collection_ids)

    response = await literature_api.save_paper(
        request=literature_api.SavePaperFromSearchRequest(
            source="semantic_scholar",
            external_id="s2-existing",
            title="Already Saved Paper",
            collection_ids=[7],
        ),
        db=db,
        current_user=SimpleNamespace(id=5),
    )

    assert response.id == 88
    assert response.collection_ids == [2, 7]
    assert added_calls == [(88, 5, [7])]
    assert db.committed is True


@pytest.mark.asyncio
async def test_save_paper_recovers_from_unique_violation(monkeypatch):
    db = _SaveDB(
        results=[],
        flush_error=IntegrityError("INSERT INTO papers ...", {}, Exception("duplicate key value")),
    )
    existing = _saved_paper(
        paper_id=98,
        user_id=5,
        source="semantic_scholar",
        title="Recovered Existing Paper",
    )
    existing.semantic_scholar_id = "s2-race"
    find_calls = {"count": 0}
    added_calls = []

    async def _fake_find_existing(_db, *, user_id, request):
        find_calls["count"] += 1
        assert user_id == 5
        assert request.external_id == "s2-race"
        if find_calls["count"] == 1:
            return None
        return existing

    async def _fake_add_to_collections(_db, *, paper_id, user_id, collection_ids):
        added_calls.append((paper_id, user_id, list(collection_ids)))
        return [7]

    async def _fake_load_collection_ids(_db, paper_id):
        assert paper_id == 98
        return [7]

    async def _fake_ensure_paper_entity(_db, paper):
        paper.paper_entity_id = 11
        return SimpleNamespace(id=11)

    monkeypatch.setattr(literature_api, "_find_existing_paper_for_request", _fake_find_existing)
    monkeypatch.setattr(literature_api, "_add_paper_to_collections_if_missing", _fake_add_to_collections)
    monkeypatch.setattr(literature_api, "_load_collection_ids_for_paper", _fake_load_collection_ids)
    monkeypatch.setattr(literature_api, "_ensure_paper_entity", _fake_ensure_paper_entity)

    response = await literature_api.save_paper(
        request=literature_api.SavePaperFromSearchRequest(
            source="semantic_scholar",
            external_id="s2-race",
            title="Recovered Existing Paper",
            collection_ids=[7],
        ),
        db=db,
        current_user=SimpleNamespace(id=5),
    )

    assert response.id == 98
    assert response.collection_ids == [7]
    assert db.rolled_back is True
    assert db.committed is True
    assert added_calls == [(98, 5, [7])]
    assert find_calls["count"] == 2


@pytest.mark.asyncio
async def test_import_paper_by_link_returns_existing_paper_and_updates_collections(monkeypatch):
    db = _ImportDB()
    existing = _saved_paper(
        paper_id=88,
        user_id=5,
        source="manual",
        title="Imported Existing Paper",
        doi="10.1000/existing",
    )
    added_calls = []

    async def _fake_resolve(_service, raw_link):
        assert raw_link == "https://doi.org/10.1000/existing"
        return (
            _paper_result(
                source="manual",
                external_id="manual-existing",
                title="Imported Existing Paper",
                doi="10.1000/existing",
            ),
            "doi",
            "https://doi.org/10.1000/existing",
        )

    async def _fake_find_existing(_db, *, user_id, request):
        assert user_id == 5
        assert request.doi == "10.1000/existing"
        return existing

    async def _fake_add_to_collections(_db, *, paper_id, user_id, collection_ids):
        added_calls.append((paper_id, user_id, list(collection_ids)))
        return [7]

    async def _fake_load_collection_ids(_db, paper_id):
        assert paper_id == 88
        return [2, 7]

    monkeypatch.setattr(literature_api, "_resolve_paper_from_link", _fake_resolve)
    monkeypatch.setattr(literature_api, "_find_existing_paper_for_request", _fake_find_existing)
    monkeypatch.setattr(literature_api, "_add_paper_to_collections_if_missing", _fake_add_to_collections)
    monkeypatch.setattr(literature_api, "_load_collection_ids_for_paper", _fake_load_collection_ids)
    monkeypatch.setattr(literature_api, "get_literature_service", lambda: SimpleNamespace())

    response = await literature_api.import_paper_by_link(
        request=literature_api.ImportPaperByLinkRequest(
            link="https://doi.org/10.1000/existing",
            collection_ids=[7],
        ),
        db=db,
        current_user=SimpleNamespace(id=5),
    )

    assert response.already_exists is True
    assert response.resolved_source == "doi"
    assert response.normalized_link == "https://doi.org/10.1000/existing"
    assert response.paper.id == 88
    assert response.paper.collection_ids == [2, 7]
    assert added_calls == [(88, 5, [7])]
    assert db.committed is True


@pytest.mark.asyncio
async def test_import_paper_by_link_builds_save_request_for_new_paper(monkeypatch):
    captured = {}

    async def _fake_resolve(_service, raw_link):
        assert raw_link == "10.1000/new-paper"
        return (
            _paper_result(
                source="manual",
                external_id="manual-new",
                title="Brand New Paper",
                doi="10.1000/new-paper",
            ),
            "doi",
            "https://doi.org/10.1000/new-paper",
        )

    async def _fake_find_existing(_db, *, user_id, request):
        assert user_id == 9
        captured["pre_save_request"] = request
        return None

    async def _fake_save_paper(*, request, db, current_user):
        captured["save_request"] = request
        captured["save_user_id"] = current_user.id
        return literature_api.PaperResponse(
            id=321,
            user_id=current_user.id,
            semantic_scholar_id=None,
            arxiv_id=None,
            doi=request.doi,
            title=request.title,
            abstract=request.abstract,
            authors=request.authors,
            year=request.year,
            venue=request.venue,
            citation_count=request.citation_count,
            reference_count=request.reference_count,
            url=request.url,
            pdf_url=request.pdf_url,
            arxiv_url=None,
            pdf_path=None,
            pdf_downloaded=False,
            knowledge_base_id=None,
            document_id=None,
            influential_citation_count=0,
            fields_of_study=request.fields_of_study,
            tags=[],
            is_read=False,
            read_at=None,
            notes=None,
            rating=None,
            source=request.source,
            published_date=None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            collection_ids=request.collection_ids,
        )

    monkeypatch.setattr(literature_api, "_resolve_paper_from_link", _fake_resolve)
    monkeypatch.setattr(literature_api, "_find_existing_paper_for_request", _fake_find_existing)
    monkeypatch.setattr(literature_api, "save_paper", _fake_save_paper)
    monkeypatch.setattr(literature_api, "get_literature_service", lambda: SimpleNamespace())

    response = await literature_api.import_paper_by_link(
        request=literature_api.ImportPaperByLinkRequest(
            link="10.1000/new-paper",
            collection_ids=[4],
        ),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=9),
    )

    save_request = captured["save_request"]
    assert captured["save_user_id"] == 9
    assert save_request.source == "manual"
    assert save_request.external_id == "manual-new"
    assert save_request.collection_ids == [4]
    assert save_request.raw_data["imported_link"] == "https://doi.org/10.1000/new-paper"
    assert response.already_exists is False
    assert response.paper.id == 321


@pytest.mark.asyncio
async def test_resolve_paper_from_link_supports_arxiv_doi_without_redirect(monkeypatch):
    requested_ids = []

    class _NullResolver:
        async def get_paper_by_doi(self, _doi):
            return None

    class _FakeArxiv:
        async def get_paper(self, arxiv_id):
            requested_ids.append(arxiv_id)
            return _paper_result(
                source="arxiv",
                external_id="1706.03762v7",
                title="Attention Is All You Need",
                arxiv_id="1706.03762v7",
            )

    class _UnexpectedHttpClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("unexpected redirect fetch")

    monkeypatch.setattr(literature_api.httpx, "AsyncClient", _UnexpectedHttpClient)

    paper, resolved_source, normalized_link = await literature_api._resolve_paper_from_link(
        SimpleNamespace(
            openalex=_NullResolver(),
            crossref=_NullResolver(),
            arxiv=_FakeArxiv(),
            pubmed=SimpleNamespace(get_paper=None),
            s2=SimpleNamespace(get_paper=None),
        ),
        "https://doi.org/10.48550/arXiv.1706.03762",
    )

    assert requested_ids == ["1706.03762"]
    assert resolved_source == "arxiv"
    assert normalized_link == "https://arxiv.org/abs/1706.03762"
    assert paper.title == "Attention Is All You Need"


def test_openalex_parse_work_handles_missing_primary_source():
    service = OpenAlexService()
    paper = service._parse_work(
        {
            "id": "https://openalex.org/W2626778328",
            "title": "Attention Is All You Need",
            "publication_year": 2017,
            "ids": {
                "doi": "https://doi.org/10.48550/arxiv.1706.03762",
            },
            "doi": "https://doi.org/10.65215/2q58a426",
            "primary_location": {
                "landing_page_url": "https://arxiv.org/abs/1706.03762",
                "source": None,
            },
            "open_access": None,
            "concepts": [],
            "authorships": [],
            "referenced_works": [],
            "cited_by_count": 123,
        }
    )

    assert paper.external_id == "W2626778328"
    assert paper.doi == "10.48550/arxiv.1706.03762"
    assert paper.url == "https://arxiv.org/abs/1706.03762"
    assert paper.venue is None


def test_build_save_request_from_paper_result_infers_arxiv_pdf_fields():
    request = literature_api._build_save_request_from_paper_result(  # pylint: disable=protected-access
        _paper_result(
            source="openalex",
            external_id="W1",
            title="Attention Is All You Need",
            doi="10.48550/arXiv.1706.03762",
        ),
        imported_link="https://doi.org/10.48550/arXiv.1706.03762",
    )

    assert request.arxiv_id == "1706.03762"
    assert request.pdf_url == "https://arxiv.org/pdf/1706.03762"
