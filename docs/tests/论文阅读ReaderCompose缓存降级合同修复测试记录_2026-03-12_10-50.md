# 论文阅读 Reader Compose：缓存降级合同修复测试记录

## 背景

- 用户在 `/literature/85/read?page=1&kb=84` 触发 AI 阅读页面生成时看到：
  `AI 编排视图生成失败 network error；已降级为本地结构化文本。`
- 实际问题不是纯前端 `Network Error`。
- 后端 `/api/v1/literature/papers/85/reader/composed/cached` 在 fallback payload 返回时触发了 `ReaderComposeFetchResponse` 校验失败，导致降级链路自身 500。

## 根因

- fallback payload 缺少 schema 必填字段：
  - `payload.engine_version`
  - `payload.ui_plan.plan_id`
- 结果是：
  - compose service 已经产出 fallback payload
  - 但 API 响应层在构造 `ReaderComposeFetchResponse` 时直接抛异常
  - 前端收到的是失败态，因此“已降级为本地结构化文本”并没有真正完成

## 修复

- 在 `backend/app/services/literature_reader_compose_service.py`
  增强 `_ensure_payload_contract(...)`：
  - 自动补齐 `engine_version`
  - 自动补齐 `paper_id / page / status / pipeline_version / source_signature / build_mode / generated_at`
  - 自动修复 `ui_plan.plan_id`
- 在 `backend/app/api/literature.py`
  的 `get_reader_composed_page_cached(...)` 路由边界再次调用 `_ensure_payload_contract(...)`，
  防止旧缓存或异常 fallback payload 直接冲破响应 schema

## 自动化验证

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "get_reader_composed_page_cached_should_repair_malformed_fallback_payload or experience_plan or generative_plan or reader_experience_flow" -q
```

结果：

- `8 passed`

新增回归覆盖：

- `test_get_reader_composed_page_cached_should_repair_malformed_fallback_payload`

验证内容：

- malformed fallback payload 不再触发 `ReaderComposeFetchResponse` 校验失败
- cached compose route 能返回 `200`
- fallback payload 自动补齐：
  - `engine_version`
  - `ui_plan.plan_id`

额外检查：

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

- `Contract alignment guard passed.`
- `Broad exception guard passed.`

## 运行态确认

真实登录态下调用：

```bash
POST /api/v1/literature/papers/85/reader/composed/cached
payload = {"page": 1, "selected_kb_id": 84}
```

结果：

- 返回 `200`
- `payload.status = fallback`
- `payload.engine_version = reader_compose_v4`
- `payload.ui_plan.plan_id = runtime_repaired_p1`

## 备注

- 前端这次看到的“network error”属于泛化提示文案，不足以准确表达实际原因。
- 当前核心问题已从“fallback 500”修复为“fallback 可返回”；若后续仍需优化用户提示，应把真实错误类型和降级是否生效分开显示。
