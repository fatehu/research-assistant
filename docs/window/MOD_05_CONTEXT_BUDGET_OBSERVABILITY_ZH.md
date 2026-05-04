> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 05: 上下文预算与退化观测统一

阶段：P3

状态：待讨论

## 目标

统一记录上下文预算、裁剪、模型压缩失败、stale 跳过和确定性裁剪的触发原因，方便后续诊断 agent 是否因为窗口压力退化。

## 不改什么

- 不改变 P0 的 tool ledger 摘要策略。
- 不改变当前模型选择。
- 不引入新的外部观测系统。

## 推荐方向

在 context debug / history event / logs 中区分：

- deterministic truncation
- model compaction
- compaction skipped / stale
- stale compaction skipped
- qwen compaction failure
- token budget overflow

记录关键指标：

- input token estimate before/after
- effective budget
- compacted message count
- qwen latency
- qwen failure count
- skipped count

## 参考实现

- Gemini CLI：tool output masking / truncation 有明确 telemetry event。
- OpenCode：session compaction 有事件和状态。
- Continue：auto compaction 有成功/失败处理。

## 验收标准

- 能从日志或 debug payload 判断本轮是否触发压缩。
- 能区分模型压缩失败、stale 跳过和本地确定性裁剪。
- 不增加主链额外模型调用。

## 实施记录

待实施后回填。

## 验证结果

待实施后回填。

## 遗留问题

待实施后回填。
