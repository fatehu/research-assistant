# 论文阅读 Generative UI 结构保真与 Agent 回退实现说明

时间：2026-02-24 18:53

## 本次目标
- 解决文本模式结构丢失（例如 `Introduction` 并入正文）。
- 把阅读页主链路切换为“后端结构化 payload + 前端流式渲染”。
- 落地共享缓存（Redis + DB）、邻页预读、模式记忆与风格模板白名单。

## 后端实现
- 新增表与模型：
  - `paper_reader_page_caches`（共享页缓存）
  - Alembic：`019_reader_generative_page_cache.py`
  - 模型：`PaperReaderPageCache`
- 新增 Schema：
  - `ReaderGenerativeRequest`
  - `ReaderGenerativeBlock / Section / Asset`
  - `ReaderGenerativePageResponse`
  - `ReaderGenerativePrefetchRequest / Response`
- 新增服务：
  - `backend/app/services/literature_reader_service.py`
  - 核心能力：
    - `build_or_get_page_payload`
    - `parse_page_structure`
    - `repair_structure_with_agent`
    - `collect_page_assets`
    - `queue_prefetch / prefetch_pages`
  - 关键策略：
    - 解析主干 + 低置信度 Agent 回退（阈值 `0.68`）
    - 回退结果必须通过 `source_anchor(page,start_char,end_char)` 校验
    - Redis 热缓存 + DB 持久缓存 + Redis 锁防并发重建
    - `source_signature` 绑定 PDF 文件状态 + parser_version + KB 更新时间
- 新增 API：
  - `POST /api/v1/literature/papers/{paper_id}/reader/generative/stream`
  - `POST /api/v1/literature/papers/{paper_id}/reader/generative/prefetch`
- 扩展状态流：
  - `/api/v1/literature/events/stream` 支持 `reader_page_ready`

## 前端实现
- `frontend/src/services/api.ts`
  - 新增 generative reader 类型定义
  - 新增 `streamReaderGenerative`
  - 新增 `prefetchReaderGenerative`
  - 扩展 `streamStatusEvents` 事件类型（含 `reader_page_ready`）
- `frontend/src/pages/literature/generativeStyles.ts`
  - 模板白名单：
    - `journal_classic`
    - `clinical_brief`
    - `preprint_modern`
  - Agent/后端仅返回 `style_key`，前端按模板 token 渲染
- `frontend/src/pages/literature/PaperReaderPage.tsx`
  - 生成式面板改为后端 payload 主链路，保留本地提取兜底
  - 流式事件按 `start -> skeleton -> chunk* -> assets -> done` 增量渲染
  - 页切换触发预读窗口：`[p-1, p+1, p+2]`
  - 模式与风格记忆到 `readerSession.last_anchor`：
    - `reader_mode`
    - `style_key`
  - 接收 `reader_page_ready` 做静默预热标记

## 影响文件
- `backend/alembic/versions/019_reader_generative_page_cache.py`
- `backend/app/models/literature.py`
- `backend/app/models/__init__.py`
- `backend/app/schemas/literature.py`
- `backend/app/services/literature_reader_service.py`
- `backend/app/api/literature.py`
- `backend/tests/test_literature_reader_generative.py`
- `frontend/src/services/api.ts`
- `frontend/src/pages/literature/generativeStyles.ts`
- `frontend/src/pages/literature/PaperReaderPage.tsx`

## 回滚点
- 关闭前端生成式模式可直接回退 PDF 模式。
- 后端 generative API 异常时，前端会自动降级到本地提取展示。

