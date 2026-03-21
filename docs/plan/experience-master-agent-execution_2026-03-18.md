# /experience Master-Agent Execution

Date: 2026-03-18

## Product Intent

- `/experience` is a reader-facing guided-reading artifact, not a plan viewer, debug surface, or `/read` reskin.
- The current page payload from `/read` remains the spine.
- Adjacent-page context is allowed to strengthen continuity, but it must come from VL analysis of rendered page images, not from text-only patching.
- External resources are allowed only when they materially improve understanding of the current page.
- `/workbench` keeps inspect/debug/runtime provenance. `/experience` does not.

## Guardrails

- The page must read like a finished teaching surface.
- The current page must remain primary; enrichment cannot displace it.
- Original evidence, AI explanation, adjacent-page continuity, and external resources must be visibly distinct.
- Internal runtime language must never reach the user-facing page.
- Multi-stage generation cannot leak as `beat`, `1/8`, `这一拍`, checkpoints, or tool traces.
- Persistence is part of product quality: re-entering the same page should prefer the finished artifact.
- Tool results must be transformed into UI value, not dumped as raw text.

## Current Failure Modes

- Default `/experience` entry for `paper=78 page=7 reader=curious_generalist` still behaves like regeneration instead of stable finished-result reuse.
- The cached route is not a true hot-cache path yet; it performs expensive adjacent-page work before checking for a finished experience artifact.
- Final page quality still leaks internal language and raw excerpt behavior in some states.
- The product boundary between `/experience` and `/workbench` is not fully enforced in the delivered experience.

## Acceptance Standard

- Opening `/experience` shows readable content immediately, without feeling like a full regeneration loop.
- Re-entering the same `paper/page/reader/user_intent` lands on a finished cached artifact, not a seed/provisional result.
- The opening section explains what the page is about without large raw excerpts.
- The page does not show `这一拍`, `beat`, `plan`, `checkpoint`, tool trace text, or error residue.
- The page does not dump long raw source text as explanation.
- External resources explain why they matter now and come from credible sources.
- Figure/table-heavy pages anchor the reader on the visual first, then bridge back to the body text.
- Adjacent-page continuity explains the bridge from previous/next pages rather than acting like pasted OCR.
- `/experience` contains no primary debug surface; provenance and runtime details belong to `/workbench`.
- Page 7 is not enough. The same standard must hold on at least:
  - a figure-heavy page
  - a body-text-heavy page
  - a terminology-heavy page

## Active Workstreams

1. Fix cached-route semantics so finished experience artifacts are checked before adjacent-page OCR/VL work.
2. Verify that adjacent-page VL analysis really receives rendered images at runtime.
3. Validate the final page, not just payloads, and keep iterating until the delivered artifact meets the standard above.

## Validated This Round

- `get_reader_experience_plan_cached()` no longer needs to build adjacent-page context before returning a completed experience cache hit.
- The hot-cache path now reconstructs `adjacent_page_context` and `page_dossier` from cached generative-plan metadata when a finished experience artifact already exists.
- Regression coverage now includes a hot-cache test that fails if `_build_experience_adjacent_page_context(...)` is called on the completed-cache path.
- Focused backend verification passed:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan" -q`
  - Result: `10 passed`

## Adjacent-Page VL Status

- The adjacent-page continuity path still uses rendered page images, not a text-only fallback.
- `_build_experience_adjacent_page_context(...)` resolves the local PDF, ensures/render-caches adjacent pages, finds the local JPG asset, and passes that local image path into `_extract_adjacent_page_reference_text(...)`.
- `_extract_adjacent_page_reference_text(...)` sends `image_paths=[...]` into `DashScopeMultimodalService.chat_json(...)`.
- `DashScopeMultimodalService.chat_json(...)` converts those local paths into `file://...` URIs and includes them in the multimodal request payload.
- The configured reader multimodal parser remains `reader_mm_parser_model`, whose default is `qwen3-vl-flash`.

## Remaining Evidence Gap

- Request-level proof now exists in test form:
  - `test_get_reader_experience_plan_should_send_adjacent_render_images_to_vl_parser`
  - file: `backend/tests/test_literature_reader_api.py`
- That test exercises the real `get_reader_experience_plan()` path, does not monkeypatch `_build_experience_adjacent_page_context(...)`, intercepts `DashScopeMultimodalService.chat_json(...)`, and asserts:
  - `image_paths` is non-empty
  - the paths point at `reader_page_assets/{paper_id}/page_6.jpg` and `page_8.jpg`
  - the model is `qwen3-vl-flash`
  - the response `adjacent_page_context` includes pages 6 and 8
- Focused verification passed:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "send_adjacent_render_images_to_vl_parser" -q`
  - Result: `1 passed`

## Current Product Status

- The backend now serves a completed cached artifact for the default page-7 entry instead of falling back to `derived_seed`.
- The `/experience` contract-cleanup work is now versioned behind `v22` cache namespaces:
  - `lit:genplan:v22:...`
  - `lit:experience:v22:...`
- The frontend no longer double-fires `getCachedReaderExperiencePlan(...)` under dev/StrictMode; in-flight dedupe now keeps that path to one request per key.
- Final-page quality is improved in code and tests, but still needs browser-side revalidation against the new `v22` artifact.
- The remaining product bottleneck is no longer “missing cache semantics”; it is the real HTTP latency of `/experience/plan/cached`.

## Performance Status

- Function-level hot-cache path:
  - `get_reader_experience_plan_cached(...)` inside the backend container returns in about `0.221s`.
- Real HTTP path with the same token and payload:
  - before HTTP serialization fix:
    - terminal `curl`: about `21.8s`, `200`, payload size about `611KB`
    - browser `fetch`: about `17.8s`
  - after switching the `/experience/plan/cached` and `/experience/plan` HTTP routes to return raw `JSONResponse(content=payload)`:
    - terminal `curl`: about `0.88s`, `200`, payload size about `480KB`
- Conclusion:
  - the worst first-entry wait was not explained by cache misses alone
  - the biggest confirmed win came from bypassing FastAPI's nested response-model HTTP serialization on these routes
  - the performance problem is now materially reduced; remaining work is content quality, not the original 20-second cached-response bottleneck

## v26 Status

- The `/experience` cache namespace is now bumped to `v26`:
  - `lit:genplan:v26:...`
  - `lit:experience:v26:...`
- Focused regression status after the `v26` contract/cache updates:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  - Result: `68 passed`
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or reader_plan_cache_keys_should_use_v26_contract_namespace" -q`
  - Result: `12 passed`
- Browser acceptance on `http://localhost:3000/literature/78/experience?page=7&reader=curious_generalist` now shows two clearly different states:
  - cold start: no longer reuses the old dirty cache; it enters a fresh seed/background-generation state under `v26`
  - warm revisit: hits only the cached endpoint and reaches `已就绪` quickly, with the page no longer showing empty interaction placeholders or the earlier low-value public-link clutter

## Current Residual Blockers

- Cold-start completion is still too slow.
  - Backend logs show the full `/experience/plan` path can take more than two minutes before returning `200`, and one run logged `planner stage failed: TimeoutError` before deterministic repair.
  - Product implication: persistence is fixed for revisits, but the first-generation completion path still feels too slow.
- Seed-state copy is still not fully reader-facing.
  - The cold-start page still shows internal-sounding summary text such as `复用清洗后的正文阅读流作为主画布。`
- The completed `完整阅读本页内容` lane is still too close to `/read`.
  - Raw English figure caption text and long paragraph excerpts remain visually dominant in that section, even though the page now separates evidence better than before.

## Updated Working Assessment

- The original persistence failure is substantially fixed:
  - warm re-entry now behaves like a finished artifact instead of an obvious regeneration loop
- The remaining work is narrower and product-specific:
  - improve cold-start convergence behavior
  - replace engineering-style seed copy with reader-facing copy
  - further demote raw body/caption dumps inside the completed body-reading lane

## Backend Contract Tightening

- A fresh-probe failure mode was reproduced and then locked down in the runtime contract:
  - `plan.meta.tool_enrichment_packet.beat_packets.beat_explain.public_links` could still admit low-value marketing/search results such as `modelengine.csdn.net/...`
  - those snippets could then flow into `beat_explain.summary/supporting_points`
  - `explainer_cluster.display_summary` could therefore become a reader-visible off-topic explainer summary even when `teacher_narrative_spine` already existed in `meta`
- The backend runtime was tightened in three layers inside `backend/app/services/generative_reader_agent_runtime.py`:
  - public-link normalization now rejects obvious low-value/hype inputs earlier:
    - `csdn.net` added to low-value domains
    - `youtube.com/shorts/...` is rejected
    - hype/marketing copy is filtered before it becomes a normalized public link
  - beat-packet reader copy generation now applies stricter source/objective gates:
    - `term_explain` / `method_background` no longer let weak public-web snippets become primary candidate summaries
    - `why_it_matters` still allows authoritative background-support copy, so existing background-support behavior stays intact
  - final visible summary repair now leans harder on `teacher_narrative_spine`:
    - `teacher_narrative_spine` now carries `focus_guidance` and `anchor_terms`
    - `focus_stage` uses `focus_guidance` as the preferred repair summary
    - `explainer_cluster` / `supporting_resources` require stronger anchor alignment before raw beat/section summaries can remain primary
    - hype/marketing summaries are explicitly rejected from visible reader-facing copy
- New focused regression coverage was added in `backend/tests/test_generative_reader_agent_runtime.py`:
  - generic focus filler should be overridden by `teacher_narrative_spine.focus_guidance`
  - marketing links such as `modelengine.csdn.net` and `youtube.com/shorts/...` must not survive into the compacted experience `beat_packets`
  - `explainer_cluster.display_summary` must fall back to `teacher_narrative_spine.term_guidance` instead of using the polluted marketing snippet

## Latest Regression Status

- After the contract-tightening patch:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  - Result: `75 passed`
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan" -q`
  - Result: `11 passed`

## Remaining Acceptance Work

- The next required check is product-side again, not unit-only:
  - regenerate a fresh default `/experience` artifact for `paper=78 page=7 reader=curious_generalist`
  - confirm `teacher_narrative_spine` now dominates visible `hero / focus / explainer / support` copy
  - confirm `beat_explain` no longer exposes the `Open Evidence` / CSDN / YouTube Shorts contamination path in the fresh artifact

## v29 Execution Status

- The `/experience` contract and cache namespace are now bumped to `v29`:
  - `lit:genplan:v29:...`
  - `lit:experience:v29:...`
- Focused regression status after the latest runtime / renderer / loader updates:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  - Result: `79 passed`
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or reader_plan_cache_keys_should_use" -q`
  - Result: `12 passed`
  - `cd frontend && npm run lint`
  - Result: passes with one pre-existing warning in `frontend/src/pages/literature/readerSurfaceLoader.ts`
  - `cd frontend && npm run build`
  - Result: passes

## Final Product Checks

- Warm revisit for `http://localhost:3000/literature/78/experience?page=7&reader=curious_generalist` now behaves like a finished artifact:
  - browser network shows only `POST /experience/plan/cached [200]`
  - the page reaches `已就绪` immediately
  - the repeated hero mini `阅读切入点` is suppressed when it is just the prefix of the hero summary
  - duplicate support/context beats such as `why_it_matters` vs `context_bridge` are collapsed when they carry the same reader-facing title and summary
- Cold start for a fresh intent now behaves more cleanly:
  - browser network shows one `POST /experience/plan/cached [200]` and one `POST /experience/plan`
  - the loader no longer starts the 6-second cached poll loop before the initial fresh `/experience/plan` request resolves
  - during `showing_seed`, the page now keeps only the primary reading spine instead of surfacing half-baked support/context/question layers

## Latest Delivered State

- Final warm page quality on the default URL is now materially closer to the intended teaching surface:
  - hero focuses on a reader-facing summary of `Fig 3`
  - focus beat explains what to inspect in the figure before returning to the paper
  - reading-flow beat keeps正文 as inspectable evidence instead of dumping it by default
  - term help is contextual and reader-facing
  - support lane is reduced to one authoritative context module instead of duplicated slabs
- The adjacent-page path remains image-grounded through `qwen3-vl-flash`; no evidence was found of a silent regression back to text-only adjacent parsing.

## Residual Risks

- Cold-start seed copy is cleaner than before, but still not at the same narrative quality as the final warm artifact.
- The support-lane summary itself is still somewhat template-like (`只在正文需要时补一层 ... 背景`); the worst duplication is gone, but that sentence can still be improved if a future pass wants a more natural voice.

## Annotated Reading Pass

- `/experience` has now been pushed one step closer to an annotated-reading surface instead of a `cards + collapsed evidence` surface.
- The renderer no longer treats the current page only as hidden evidence drawers:
  - primary guided beats are grouped into reading segments
  - `concept_bridge / why_it_matters / context_bridge` beats are attached to the nearest preceding primary segment instead of floating as separate top-level slabs
  - source evidence is now previewed inline as `本页摘录`, with the full raw evidence demoted behind an explicit expand action
- Adjacent-page continuity is now treated as bridge copy inside the segment flow instead of a dedicated top-level continuity block.
- The renderer also filters obvious low-signal continuity and English residue before surfacing it.

## Current Acceptance Read

- Directionally, this is the right architecture shift:
  - the current page is back in the main reading path
  - the page is closer to `讲读 + 摘录 + 边注`
  - adjacent continuity is no longer meant to sit as a detached shelf
- This is still not signed off as complete:
  - primary reading segments still use `Card` shells, so the final page may still read as blocky rather than like one continuous teaching surface
  - body/figure excerpts are now previewed and clamped, but the remaining visual chrome still needs product-side review
  - warm/cold browser acceptance on the latest renderer revision is still pending because local Vite surfaces were repeatedly interrupted by unrelated filesystem `EIO` overlays

## Environment Blockers Seen During This Pass

- Browser-side acceptance was blocked by infrastructure noise, not only by product behavior:
  - Docker Vite surfaced `EIO` overlays while reading unrelated frontend files
  - a local fallback Vite dev instance on `3002` hit the same class of `EIO` errors under `/mnt/d`
- Local host/container test environments also showed unrelated verification instability:
  - local `.venv-incremental` pytest runs failed during import collection because of `/mnt/d` I/O errors
  - containerized `test_literature_reader_api.py -k "experience_plan"` failed in `pytest-asyncio` fixture setup with `OSError: could not get source code`
- Backend runtime-focused verification still succeeded in-container for the tightened continuity/display-copy path:
  - `docker exec research_backend python -m pytest /app/tests/test_generative_reader_agent_runtime.py -k "adjacent_continuity or continuity or display_copy_contract or fallback_hero_copy_reader_facing" -q`
  - Result: `5 passed`

## Annotated Reading Surface Iteration

- The current pass tightened the frontend renderer toward an annotated-reading surface instead of a card stack:
  - `figure_walkthrough` and `body_segment` now render as primary reading segments
  - the visible segment tag is now semantic (`图解主线` / `正文主线`) instead of internal numbering (`阅读段 1/2`)
  - source material is shown as inline `本页摘录`, while the full raw evidence is demoted to a secondary collapse
- Source evidence is no longer treated as one undifferentiated dump:
  - body segments now strip repeated `FigurePanel` nodes when the figure has already anchored the page
  - `ParagraphProse` and `FigurePanel.caption` previews are clamped into shorter excerpts
  - long/full raw content remains available under `查看证据与来源`
- Reader-facing cleanup applied in the renderer:
  - English-heavy `continuity_note` rows are now suppressed instead of leaking to the final page
  - low-signal continuity copy and machine-like bridge text are filtered before rendering
  - support material remains attached beneath the relevant segment instead of becoming a detached top-level slab

## Latest Acceptance Notes

- Warm-page browser acceptance before the frontend dev-server filesystem failure confirmed:
  - `阅读段 1/2` was removed from the visible page
  - repeated figure content stopped reappearing in the body segment
  - English continuity lines no longer surfaced as a visible “承接提示”
- The default page still has one product-quality gap that was being actively tightened:
  - the first body excerpt could still open with an orphaned English fragment (`adjudicator, as a second-year medical student ...`)
  - a follow-up renderer pass added fragment dropping for short lowercase lead excerpts, but the browser could not be revalidated afterward because the frontend dev server became unstable
- The figure-reading lane is improved but not finished:
  - the figure caption preview is now shortened
  - the page still reads more like “explained excerpts” than a fully polished teacher-led narrative

## Infra Noise During Acceptance

- Browser-side acceptance hit an environment blocker unrelated to the `/experience` logic:
  - both Docker Vite (`frontend` at `:3000`) and a local Vite dev server failed with repeated `EIO: i/o error` reads under the workspace filesystem
  - examples included failures opening `/app/src/main.tsx`, `/app/src/components/common/AntdMessageBridge.tsx`, and local Babel/Tailwind dependency reads
- What was still verified despite that:
  - targeted frontend checks passed locally:
    - `npx eslint src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/PaperReaderExperiencePage.tsx`
    - `npx tsc --noEmit`
  - the last stable browser snapshot before the Vite `EIO` crash already showed the main renderer improvements above
- What remains to re-check once the frontend runtime is stable again:
  - confirm the new fragment-dropping logic removes the stray lead English fragment from the body excerpt
  - confirm the updated warm page still reaches `已就绪` on the cached path
  - confirm a fresh cold-start seed does not regress back to raw long-form body dumps

## Host Runtime Note

- The current `frontend` Docker service is bind-mounted from the host workspace:
  - `./frontend:/app` in `docker-compose.yml`
- Product implication:
  - the repeated Vite `EIO` overlays seen during this pass should be treated as host-filesystem instability on `/mnt/d`, not as direct evidence that the latest `/experience` renderer/runtime changes regressed
- Master-agent handling in this pass:
  - frontend and backend sub-agents were closed after review
  - only the exact session files created in this pass were deleted
  - final product acceptance remains open until the frontend runtime can render the updated page without the host I/O fault

## Final Live Acceptance Pass

- Acceptance was completed against the ext4-backed local frontend runtime at:
  - `http://localhost:3000/literature/78/experience?page=7&reader=curious_generalist`
- Verified warm-page results after the final frontend/backend pass:
  - the figure lane no longer renders duplicate slabs (`图解导读` + `交互图解`); only the interactive figure guide remains visible
  - the figure-segment summary no longer leaks scaffold text such as `这一段正文包含本页的重要结论...`
  - the body lane keeps the lighter quote-style excerpt instead of dumping a long raw paragraph directly into the main flow
  - full raw figure/body evidence remains available behind `查看证据与来源`
  - the empty fallback `延伸参考` line is no longer rendered once no reader-worthy support module survives
- Verified cached backend payload on the same default route:
  - `experience_cache_layer=redis`
  - `generative_plan_cache_layer=redis`
  - weak support links (`doi.org ... g003`, `celap.org.cn`, `lib.smu.edu.cn`) are no longer present in `plan.supporting_resources`
  - the remaining top-level support module is only the figure-side explainer (`FigureExplainPanel`)
- Final focused verification in this pass:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -k "keep_only_reader_worthy_support_resources or allow_zero_supporting_resources_when_only_weak_links_exist or drop_generic_fallback_support_card_without_links or finalize_plan_should_drop_weak_or_raw_supporting_resources_when_no_reader_worthy_links or validate_experience_plan_contract_should_strip_stale_weak_supporting_resources" -q`
  - Result: `5 passed`
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -k "promote_page_brief_to_page_sections or drop_generic_fallback_support_card_without_links or validate_experience_plan_contract_should_strip_stale_weak_supporting_resources" -q`
  - Result: `3 passed`
  - `cd frontend && npm run lint -- src/pages/literature/GenerativeExperienceRenderer.tsx`
  - Result: passes with the pre-existing `readerSurfaceLoader.ts` hook warning only

## Surface Persistence Pass

- The remaining product gap after the content cleanup was not only backend cache quality; it was entry behavior:
  - the default URL still briefly fell back to `正在准备体验页` on every revisit because the frontend cleared state before waiting for `/experience/plan/cached`
- The `/experience` loader now persists the last non-draft reader surface for the exact `paper/page/kb/reader/user_intent` tuple in `sessionStorage`, then rehydrates it on the next mount before network validation:
  - file: `frontend/src/pages/literature/readerSurfaceLoader.ts`
  - persisted payload includes the last usable `composePayload`, `experienceResponse`, and cache-state metadata
  - revisit behavior now becomes “show the last readable artifact immediately, then validate in the background”
- Verified frontend checks after this pass:
  - `cd frontend && npx eslint src/pages/literature/readerSurfaceLoader.ts src/pages/literature/GenerativeExperienceRenderer.tsx`
  - Result: passes
  - `cd frontend && npx tsc --noEmit`
  - Result: passes
- Browser verification after syncing the updated frontend into the ext4 runtime:
  - on revisiting the same default URL, the page still flashes the initial shell before React hydration, but within about `1s` it rehydrates into the last readable surface instead of waiting for a fresh `/plan` cycle to finish
  - the rehydrated surface already contains the cleaned figure summary, deduped figure guidance, lighter quote-style evidence, and no weak support-resource clutter
  - the revisit network trace for this pass shows only:
    - `POST /api/v1/literature/papers/78/experience/plan/cached [200]`
- Current nuanced status after the persistence pass:
  - backend `cached` for the default route is still a `fallback` artifact in the data contract (`plan.status=fallback`, `generative_plan.status=fallback`)
  - however, the user-visible revisit experience is now materially improved because the loader rehydrates the last readable surface instead of blocking on that cached fallback response
  - further work is still justified if the goal is to turn the default backend artifact itself from `fallback` into a stable `done` plan, but the revisit UX is no longer “empty page first, wait for regeneration”

## Content-First Live Acceptance Pass

- The remaining mismatch after the persistence work was confirmed on the live route itself:
  - `/experience` still looked too much like a manuscript/slot/evidence page
  - long raw English figure captions and raw body paragraphs were visible by default
  - later, after reducing the raw surface, the opposite bug appeared: `explainer_cluster` and `supporting_resources` existed in the plan but were not rendered into the live DOM
- Runtime contract was tightened again in `backend/app/services/generative_reader_agent_runtime.py`:
  - the page-generation prompt no longer forces `/experience` into “do not rewrite body / only target existing enrichment targets / guided beats are the primary guide”
  - the runtime now prefers authored Chinese section copy that is current-page anchored, bridge-only for adjacent pages, and less source-dump heavy
  - `focus_stage` / `reading_flow` section summaries were made more specific so the page can lean on authored explanation instead of raw excerpts
- Frontend rendering was reworked again in:
  - `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`
  - `frontend/src/pages/literature/PaperReaderExperiencePage.tsx`
  - `frontend/src/pages/literature/readerSurfaceLoader.ts`
  - `frontend/src/pages/literature/experienceReaderPrimitives.ts`
- Key frontend outcomes from this pass:
  - `/experience` now renders in a content-first mode (`阅读体验` / `刷新体验`) instead of the old `最终讲读稿` framing
  - raw figure/body material is demoted to optional reference layers by default
  - focus/body sections show lighter narrative copy plus short teaser excerpts instead of dumping the full figure caption or full body paragraphs into the primary surface
  - the renderer now keeps sidebar sections visible even when `guided_beats` exist; this fixes the bug where glossary/background/resource sections were silently dropped from the live DOM
  - reader-facing primitives are populated instead of hard-coded empties
- A dedicated `frontend/.dockerignore` was added so preview builds no longer drag local artifacts (`node_modules`, `.vite`, `dist`, etc.) into the Docker build context.
- Because the compose-mounted Vite dev server on `/app` kept hitting broad `EIO` file-read errors, final live acceptance in this pass used a standalone frontend preview image built from the cleaned frontend context and served on port `3000`.
- Exact live acceptance route:
  - `http://localhost:3000/literature/78/experience?page=7&reader=curious_generalist`
- Final live observations for that exact route:
  - top-level UI now reads `阅读体验` / `体验已就绪` / `刷新体验`
  - main lane is Chinese-first and no longer exposes the old manuscript/slot chrome
  - figure and body evidence are still available, but behind `可选参考` expanders
  - the sidebar now correctly renders:
    - `读懂关键术语`
    - `读到这里再补背景`
    - a trusted external link: `USMLE 官方说明`
  - the route now behaves much closer to the intended product boundary:
    - `/experience` shows a guided, readable surface
    - `/workbench` remains the place for provenance/debug inspection
- Verification run after the final pass:
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  - Result: `106 passed`
  - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan" -q`
  - Result: `21 passed, 27 deselected`
  - `cd frontend && npx eslint src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/PaperReaderExperiencePage.tsx src/pages/literature/readerSurfaceLoader.ts src/pages/literature/experienceReaderPrimitives.ts`
  - Result: passes
  - `cd frontend && npx tsc --noEmit`
  - Result: passes
