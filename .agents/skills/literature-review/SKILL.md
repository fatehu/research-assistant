---
name: literature-review
description: Build a literature review from a clear research topic by searching papers, downloading PDFs into a review workspace, converting full PDFs to Markdown, writing per-paper reviews, and synthesizing a final Markdown review.
---

# Literature Review

Use this skill when the user wants a paper survey, literature review, related work draft, or topic-level synthesis across multiple papers.

The user must provide a clear review topic. If the topic is vague, ask one short clarification before searching.

Default target: collect and review 12 readable full-text papers unless the user specifies another number.

Do not use Project, paper reproduction, or knowledge-base ingestion for this workflow unless the user explicitly asks. This workflow owns only:

- `/app/uploads/literature_reviews/{literature_review_id}/pdf/`
- `/app/uploads/literature_reviews/{literature_review_id}/md/`
- `/app/uploads/literature_reviews/{literature_review_id}/review/`

## Workflow

1. Start or resume a review workspace with `literature_review_start`.
2. Search with `literature_search`.
   - Prefer `source=auto`.
   - Use `max_results`, `offset`, `page_token`, `year_start`, `year_end`, `fields`, `open_access`, `sort_by`, and `sort_order` as needed.
   - Prefer candidates with `pdf_url` or an `arxiv_id`.
3. Download candidate PDFs with `literature_review_download_pdf`.
   - Pass through the metadata returned by `literature_search` exactly when available: `title`, `abstract`, `authors`, `year`, `venue`, `source`, `external_id`, `doi`, `url`, `pdf_url`, `arxiv_id`, `citation_count`, `reference_count`, and `fields_of_study`.
   - Do not invent missing metadata. Missing DOI/link/abstract fields should stay missing so downstream review files can mark them as `未提供`.
4. Convert each downloaded PDF with `literature_review_pdf_to_markdown`.
   - The complete Markdown is written to `md/`; this tool returns `md_path`, `report_path`, page count, and character count.
   - Pass the returned `md_path` or `paper_key` to `review_writer mode=paper`.
   - Do not put full papers into the chat context.
   - Do not use this tool for whole-paper translation. If the user asks for full translation, update the relevant artifact/review area with a clear limitation note instead of attempting to translate the full paper in one model turn.
5. For each converted paper, call `review_writer` with `mode=paper`.
   - This writes one per-paper Markdown review under `review/`.
   - The per-paper review includes a platform-generated fixed metadata block for search abstract, links, DOI, and citation formats. Treat it as grounded source metadata, not model-written content.
6. When inspecting existing review outputs, use `literature_review_read`.
   - The user or previous tool result must provide a `literature_review_id` such as `review-20260425053243-85976a3c`.
   - This tool is only for generated review Markdown under `review/*.md`, including `review/final.md`.
   - Default `mode=list` returns existing review paths and includes the paper identity metadata for each single-paper review: title, authors, year, venue, DOI/link, and citation format.
   - After choosing one path, call `mode=read` with `relative_path`; this returns the selected review Markdown in full.
   - Do not use this tool for full paper Markdown under `md/*.md` or PDF-to-Markdown parse reports under `md/*.json`.
   - Prefer these finished review Markdown files when answering summary, comparison, and synthesis questions, because they already contain curated metadata, abstracts, links, and citations.
7. If you need evidence from Markdown, use `literature_review_search_zoekt`.
   - Use `scope=paper` for full paper Markdown under `md/*.md`; those papers are usually English, so use English search terms.
   - Use `scope=review` for generated review Markdown under `review/*.md`; Chinese search terms are suitable here when the review was written in Chinese.
   - Use `scope=all` only when you intentionally want both full paper evidence and generated review passages.
   - Do not use Project Zoekt or project file tools for literature review Markdown.
   - Search results include paper identity metadata such as title, authors, DOI/link, and citation format from the review manifest.
   - Use Zoekt for targeted source checking, excerpt translation, and locating specific claims. Do not try to read or translate an entire paper through tool output.
8. After enough per-paper reviews exist, call `review_writer` with `mode=final`.
   - This writes `review/final.md`.
   - The final review includes a platform-generated reference catalog with links and citations from the collected metadata.
   - Report the final path and summarize the final review to the user.

## Rules

- Do not fabricate inaccessible papers. If a result has no downloadable PDF, skip it or report it as metadata-only.
- Do not claim the review is complete until `review_writer mode=final` succeeds.
- If fewer than the target number of PDFs can be downloaded or read, report the count and ask whether to lower the target or continue searching.
- Keep intermediate outputs file-based. Use tool observations for paths, counts, and status.
- For prior review artifacts, list review Markdown first and read only the selected file. Use Zoekt for targeted evidence instead of reading every Markdown file into context.
- Whole-paper translation is outside this workflow's single-turn output budget. For such requests, write a limitation note into the relevant document artifact block or review section, and offer targeted excerpt translation based on Zoekt hits.
