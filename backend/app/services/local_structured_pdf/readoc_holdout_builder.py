from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import random
import shutil
import urllib.request
from typing import Iterable
from zipfile import BadZipFile, ZipFile

from .external_holdout_builder import _sanitize_doc_id


@dataclass(frozen=True)
class ReadocDocument:
    subset: str
    doc_id: str
    markdown_path: Path


def discover_readoc_documents(*, source_root: Path) -> list[ReadocDocument]:
    source_root = Path(source_root).resolve()
    documents: list[ReadocDocument] = []
    for markdown_dir in sorted(source_root.glob("*_ground_truth")):
        if not markdown_dir.is_dir():
            continue
        subset = markdown_dir.name.removesuffix("_ground_truth")
        for markdown_path in sorted(markdown_dir.glob("*.md")):
            if not markdown_path.is_file():
                continue
            doc_id = _sanitize_doc_id(Path(subset) / markdown_path.stem)
            documents.append(
                ReadocDocument(
                    subset=subset,
                    doc_id=doc_id,
                    markdown_path=markdown_path,
                )
            )
    return documents


def build_readoc_holdout_from_archives(
    *,
    source_root: Path,
    output_root: Path,
    subsets: tuple[str, ...] = (),
    limit: int | None = None,
    seed: int = 42,
    balance_by_subset: bool = True,
    allow_direct_arxiv_download: bool = True,
) -> dict[str, object]:
    source_root = Path(source_root).resolve()
    output_root = Path(output_root).resolve()
    pdf_dir = output_root / "pdfs"
    markdown_dir = output_root / "markdown"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    markdown_dir.mkdir(parents=True, exist_ok=True)

    discovered = discover_readoc_documents(source_root=source_root)
    selected = _select_readoc_documents(
        documents=discovered,
        subsets=subsets,
        limit=limit,
        seed=seed,
        balance_by_subset=balance_by_subset,
    )

    archives = {
        archive.stem: archive
        for archive in sorted(source_root.glob("*.zip"))
        if archive.is_file()
    }
    extracted_documents: list[dict[str, object]] = []
    for document in selected:
        archive_path = archives.get(document.subset)
        target_pdf_path = pdf_dir / f"{document.doc_id}.pdf"
        if archive_path is not None:
            try:
                _extract_matching_pdf(
                    archive_path=archive_path,
                    source_stem=document.markdown_path.stem,
                    target_pdf_path=target_pdf_path,
                )
            except BadZipFile:
                if not (allow_direct_arxiv_download and document.subset == "arxiv"):
                    raise
                _download_arxiv_pdf(
                    arxiv_id=document.markdown_path.stem,
                    target_pdf_path=target_pdf_path,
                )
                archive_path = None
        elif allow_direct_arxiv_download and document.subset == "arxiv":
            _download_arxiv_pdf(
                arxiv_id=document.markdown_path.stem,
                target_pdf_path=target_pdf_path,
            )
        else:
            raise FileNotFoundError(
                f"Missing archive for subset {document.subset}: expected {document.subset}.zip"
            )
        shutil.copy2(document.markdown_path, markdown_dir / f"{document.doc_id}.md")
        extracted_documents.append(
            {
                "subset": document.subset,
                "doc_id": document.doc_id,
                "markdown_path": str(document.markdown_path),
                "archive_path": "" if archive_path is None else str(archive_path),
                "source_stem": document.markdown_path.stem,
                "pdf_source": "arxiv_direct" if archive_path is None and document.subset == "arxiv" else "archive",
            }
        )

    manifest = {
        "source_root": str(source_root),
        "output_root": str(output_root),
        "document_count": len(selected),
        "subsets": sorted({document.subset for document in selected}),
        "seed": int(seed),
        "balance_by_subset": bool(balance_by_subset),
        "documents": extracted_documents,
    }
    (output_root / "holdout_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _extract_matching_pdf(
    *,
    archive_path: Path,
    source_stem: str,
    target_pdf_path: Path,
) -> None:
    with ZipFile(archive_path) as archive:
        candidates = [
            member
            for member in archive.namelist()
            if Path(member).suffix.lower() == ".pdf" and Path(member).stem == source_stem
        ]
        if len(candidates) != 1:
            raise FileNotFoundError(
                f"Expected exactly one PDF named {source_stem}.pdf in {archive_path}, found {len(candidates)}"
            )
        with archive.open(candidates[0]) as source, target_pdf_path.open("wb") as target:
            shutil.copyfileobj(source, target)


def _download_arxiv_pdf(*, arxiv_id: str, target_pdf_path: Path) -> None:
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    request = urllib.request.Request(url, headers={"User-Agent": "local-structured-pdf/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, target_pdf_path.open("wb") as target:
        shutil.copyfileobj(response, target)


def _select_readoc_documents(
    *,
    documents: list[ReadocDocument],
    subsets: tuple[str, ...],
    limit: int | None,
    seed: int,
    balance_by_subset: bool,
) -> list[ReadocDocument]:
    normalized_subsets = tuple(str(value).strip() for value in subsets if str(value).strip())
    if normalized_subsets:
        allowed = set(normalized_subsets)
        documents = [document for document in documents if document.subset in allowed]

    if limit is None or limit <= 0 or len(documents) <= limit:
        return list(documents)

    if not balance_by_subset:
        rng = random.Random(seed)
        shuffled = list(documents)
        rng.shuffle(shuffled)
        return sorted(shuffled[:limit], key=lambda item: item.doc_id)

    grouped: dict[str, list[ReadocDocument]] = {}
    for document in documents:
        grouped.setdefault(document.subset, []).append(document)

    rng = random.Random(seed)
    ordered_groups = sorted(grouped.items(), key=lambda item: item[0])
    for _, items in ordered_groups:
        rng.shuffle(items)

    selected: list[ReadocDocument] = []
    while len(selected) < limit and any(items for _, items in ordered_groups):
        for _, items in ordered_groups:
            if not items or len(selected) >= limit:
                continue
            selected.append(items.pop())
    return sorted(selected, key=lambda item: item.doc_id)
