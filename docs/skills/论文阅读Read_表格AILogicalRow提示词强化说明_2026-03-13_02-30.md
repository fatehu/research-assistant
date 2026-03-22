## 背景

`layout_uid_v1` 的 AI logical-row 重建已经真实进入 live payload，但当前系统提示词仍偏通用：

- 强调了 `physical rows -> logical rows`
- 强调了 exact-once
- 强调了不要改几何和文本

但没有明确强化学术 benchmark 表里最常见的模式：

- `value row + uncertainty row`
- 多行表头
- 首列为空但后续列继续同一 benchmark 的 continuation row

这会导致 AI 虽然能产出 `logical_rows`，但质量不够稳定。

## 本次目标

只加强 `/read` 表格 AI logical-row 的语义提示，不改：

- 全局 evidence preview 主链
- DocMind 几何真值来源
- 现有 `uniqueId -> blocks[].pos` 高光链

## 设计原则

1. AI 只做 logical row grouping
   - 不改文本
   - 不改几何
   - 不改 layout ownership

2. Prompt 需要显式识别以下模式
   - multi-line header
   - value row + uncertainty row
   - value row + continuation values row
   - blank first-column continuation row

3. Payload 需要带轻量 pairing hints
   - 是否存在 `±`
   - 是否首列为空
   - 是否行内数值占比高

4. Validator 仍然保持 strict exact-once
   - 不允许 AI 因为更激进的 grouping 而跳过物理行

## 回退边界

如果这轮强化导致 AI 行配对更差，只回退：

- `_layout_uid_table_logical_row_system_prompt()`
- `_build_layout_uid_table_logical_row_prompt_payload()`
- 对应测试

不要回退：

- live payload 持久化 `logical_rows`
- 现有 table materialization
- evidence/highlight 主链
