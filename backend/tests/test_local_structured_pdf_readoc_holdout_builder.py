from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from app.services.local_structured_pdf.readoc_holdout_builder import (
    build_readoc_holdout_from_archives,
    discover_readoc_documents,
)


def test_discover_readoc_documents_reads_ground_truth_dirs(tmp_path: Path):
    source_root = tmp_path / "readoc"
    gt_dir = source_root / "arxiv_ground_truth"
    gt_dir.mkdir(parents=True)
    (gt_dir / "0705.4297.md").write_text("# doc", encoding="utf-8")

    docs = discover_readoc_documents(source_root=source_root)

    assert [doc.subset for doc in docs] == ["arxiv"]
    assert [doc.doc_id for doc in docs] == ["arxiv__0705.4297"]


def test_build_readoc_holdout_from_archives_extracts_matching_pdfs(tmp_path: Path):
    source_root = tmp_path / "readoc"
    output_root = tmp_path / "holdout"
    gt_dir = source_root / "arxiv_ground_truth"
    gt_dir.mkdir(parents=True)
    (gt_dir / "0705.4297.md").write_text("# first", encoding="utf-8")
    (gt_dir / "0706.0028.md").write_text("# second", encoding="utf-8")

    with ZipFile(source_root / "arxiv.zip", "w") as archive:
        archive.writestr("nested/0705.4297.pdf", b"%PDF-1.4 first")
        archive.writestr("0706.0028.pdf", b"%PDF-1.4 second")

    manifest = build_readoc_holdout_from_archives(
        source_root=source_root,
        output_root=output_root,
        subsets=("arxiv",),
        limit=1,
        seed=1,
        balance_by_subset=True,
    )

    assert int(manifest["document_count"]) == 1
    pdf_paths = list((output_root / "pdfs").glob("*.pdf"))
    markdown_paths = list((output_root / "markdown").glob("*.md"))
    assert len(pdf_paths) == 1
    assert len(markdown_paths) == 1
    loaded_manifest = json.loads((output_root / "holdout_manifest.json").read_text(encoding="utf-8"))
    assert loaded_manifest["documents"][0]["subset"] == "arxiv"


def test_build_readoc_holdout_can_fallback_to_direct_arxiv_download(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "readoc"
    output_root = tmp_path / "holdout"
    gt_dir = source_root / "arxiv_ground_truth"
    gt_dir.mkdir(parents=True)
    (gt_dir / "0705.4297.md").write_text("# first", encoding="utf-8")

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            from io import BytesIO

            self._buffer = BytesIO(self._payload)
            return self._buffer

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def _fake_urlopen(request, timeout=0):
        return _FakeResponse(b"%PDF-1.4 direct")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    manifest = build_readoc_holdout_from_archives(
        source_root=source_root,
        output_root=output_root,
        subsets=("arxiv",),
        limit=1,
        seed=1,
        balance_by_subset=True,
        allow_direct_arxiv_download=True,
    )

    assert int(manifest["document_count"]) == 1
    pdf_paths = list((output_root / "pdfs").glob("*.pdf"))
    assert len(pdf_paths) == 1
    assert pdf_paths[0].read_bytes() == b"%PDF-1.4 direct"


def test_build_readoc_holdout_falls_back_when_archive_is_corrupt(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "readoc"
    output_root = tmp_path / "holdout"
    gt_dir = source_root / "arxiv_ground_truth"
    gt_dir.mkdir(parents=True)
    (gt_dir / "0705.4297.md").write_text("# first", encoding="utf-8")
    (source_root / "arxiv.zip").write_bytes(b"not-a-zip")

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            from io import BytesIO

            self._buffer = BytesIO(self._payload)
            return self._buffer

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def _fake_urlopen(request, timeout=0):
        return _FakeResponse(b"%PDF-1.4 fallback")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    manifest = build_readoc_holdout_from_archives(
        source_root=source_root,
        output_root=output_root,
        subsets=("arxiv",),
        limit=1,
        seed=1,
        balance_by_subset=True,
        allow_direct_arxiv_download=True,
    )

    assert int(manifest["document_count"]) == 1
    pdf_path = next((output_root / "pdfs").glob("*.pdf"))
    assert pdf_path.read_bytes() == b"%PDF-1.4 fallback"
