## 本轮目标

把 `/experience` 的输入层从“当前页 + 弱邻页文本参考”提升为：

- 当前页 compose / enrichment / assets 摘要
- 前后页 `VL-flash` 结构化上下文
- 可在 `/workbench` 直接查看的 `page_dossier`

## 检查项

### 后端

执行：

```bash
python3 -m py_compile backend/app/api/literature.py backend/app/services/generative_reader_agent_runtime.py backend/app/schemas/literature.py
```

结果：

- 通过

### 前端

执行：

```bash
cd frontend && npx tsc --noEmit
npm --prefix frontend run lint -- --quiet
```

结果：

- 通过

## 观察点

### `/experience`

- generative/runtime 调用现在会携带：
  - `adjacent_page_context`
  - `page_dossier`

### `/workbench`

- 右侧新增：
  - `Page Dossier`
  - `Adjacent Page Context`

应能看到：

- 邻页摘要
- 邻页 body_text
- figure/table/equation 描述
- continuation hints

## 已知未完成

- 还没有把 `/experience` 改成 staged runtime
- 还没有新增更自由的 block/layout families
- 这轮主要解决的是“输入层不够强”和“不可观察”的问题

## 后续补充检查

新增检查点：

- runtime `build_experience_plan(...)` 的 `meta` 必须保留：
  - `resource_strategy`
  - `used_tools`
  - `tool_trace_summary`
  - `adjacent_page_context`
- `/experience` 页面生成细节应能看到：
  - 资源策略
  - 邻页参考
  - Tool Trace
- `/workbench` 应能看到：
  - dossier 摘要
  - 原始 dossier JSON
  - Tool Trace
