# Generative UI Experience 验收清单

Last updated: 2026-03-11
Scope: `/literature/:paperId/experience`

## 自动化验收

后端最小回归：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan" -q
```

前端静态检查：

```bash
npm --prefix frontend run lint
npm --prefix frontend run build
```

本轮通过标准：

- runtime tests 通过
- literature reader API 中 `experience_plan` / `generative_plan` 相关测试通过
- frontend `lint` 无 error
- frontend `build` 至少应完成 `tsc -b` 且生成 `frontend/dist/index.html`

## 手动验收

以下验收默认围绕单论文页面样本进行，例如：

- `http://localhost:3000/literature/78/read?page=7`
- `http://localhost:3000/literature/78/experience?page=7`

### 1. 产品边界

- [ ] `/read` 仍然是原阅读器，不出现把 generative experience 强塞回阅读器主链路的情况
- [ ] `/read/workbench` 仍然是调试页，不把它当成最终用户页面
- [ ] `/experience` 明显是新的展开式阅读页面，不像调试器卡片堆

### 2. 页面结构

- [ ] `/experience` 首屏有清晰 hero，不是直接堆正文卡片
- [ ] 页面主区/侧栏/底部的分布与当前 `layout_variant` 一致
- [ ] 不同 section 的顺序看起来由 plan 决定，不是固定写死双栏壳层
- [ ] 当 plan 没有 sidebar section 时，页面应退化成单主栏，而不是因为辅助卡片硬撑出侧栏

### 3. 内容归属

- [ ] `focus_stage` 里的资源/交互/图解，围绕当前焦点 target 组织
- [ ] `supporting_resources` 是帮助理解正文的外部资源，不是 DOI 或泛链接堆砌
- [ ] `explainer_cluster` 聚焦术语/概念解释，而不是复读正文
- [ ] `question_lab` 更像继续探索入口，而不是调试信息展示

### 4. 状态与回退

- [ ] 首次打开时，若 full plan 未完成，可先看到基础体验或正文底座
- [ ] 刷新页面后，如果缓存命中，页面能稳定恢复
- [ ] 生成未完成时页面不应白屏
- [ ] 接口失败时页面应显示可理解的提示，而不是纯 `Network Error`

### 5. 中文展示

- [ ] 用户可见标题、说明、按钮和提示以中文为主
- [ ] 不要求把内部 agent prompt 或 schema 名称中文化

## 本轮验收记录模板

自动化：

- [ ] `test_generative_reader_agent_runtime.py`
- [ ] `test_literature_reader_api.py -k "experience_plan or generative_plan"`
- [ ] `npm --prefix frontend run lint`
- [ ] `npm --prefix frontend run build`

手动：

- [ ] 产品边界
- [ ] 页面结构
- [ ] 内容归属
- [ ] 状态与回退
- [ ] 中文展示

备注：

- 记录具体样本页面，例如 `paper=78 page=7`
- 如不通过，记录现象、URL、截图或控制台报错

## 2026-03-10 Iteration 1

范围：

- `/literature/78/experience?page=7`
- 目标：验证 `/experience` 是否更接近 plan 驱动页面，并确认 fresh refresh 与中文展示改动已生效

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`20 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan" -q`
  结果：`6 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 hook warning
- [ ] `npm --prefix frontend run build`
  结果：当前环境里 `vite build` 偶发长时间挂起，但已能稳定生成 `frontend/dist/index.html`

运行态观察：

- [x] 后端日志确认存在 `POST /api/v1/literature/papers/78/experience/plan`
- [x] 后端日志确认 fresh refresh 已触发 `compose_force_refresh=True parser_force_refresh=True regenerate=True`
- [x] 后端日志确认工具调用已实际执行：
  `paper_read`、`web_search`、`web_scrape`

本轮已完成：

- [x] `/experience` renderer 更严格按 section-level plan 绑定模块
- [x] `刷新体验` 改为走真正的 fresh path，而不只是 cached path
- [x] prompt 增加“用户可见文案默认简体中文”约束
- [x] 后端 deterministic fallback / experience-plan 文案开始中文化

本轮待你手工确认：

- [ ] 点一次“刷新体验”，确认按钮进入 loading 且页面内容有变化
- [ ] 页面用户可见文案是否已明显以中文为主
- [ ] 页面是否仍然有过强的传统双栏感
- [ ] 若仍有英文，记录英文出现在哪一类：
  hero / section 标题 / 资源卡 / 术语解释 / 问题卡 / widget

## 2026-03-10 Iteration 2

范围：

- `/literature/78/experience?page=7`
- 目标：压缩用户可见英文残留，并避免旧 English-heavy 计划缓存继续命中

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`21 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan" -q`
  结果：`6 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 hook warning
- [ ] `npm --prefix frontend run build`
  结果：当前环境里 `vite build` 仍会长时间挂起；本轮没有拿到稳定退出结果

本轮代码结果：

- [x] fallback plan 中会直接暴露给用户的英文标题、摘要、面板说明已转成中文
- [x] `story_substrate` / `page_brief` / modules / widgets 增加一层 generic English reader-copy rewrite
- [x] generative plan / experience plan cache key 已升级版本，避免旧英文缓存继续复用
- [x] 补了覆盖英文收口逻辑的 runtime 回归测试

本轮待你手工确认：

- [ ] 再点一次“刷新体验”，确认新计划不再被旧英文缓存覆盖
- [ ] hero、section 标题、右侧钩子文案是否明显更偏中文
- [ ] 如果仍有英文，优先记录它属于：
  `hero` / `page_brief hooks` / `resource summary` / `widget panel` / `question card`
- [ ] 页面虽然可以有双栏，但是否已经不像固定死模板，而更像被当前 plan 驱动

## 2026-03-11 Iteration 3

范围：

- `/literature/78/experience?page=7`
- 目标：让 `/experience` fresh 生成时带上前后页 PDF 渲染后的参考 OCR 文本，只作为承接上下文，不覆盖当前页主证据

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`22 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan" -q`
  结果：`6 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 hook warning
- [ ] `npm --prefix frontend run build`
  结果：本轮未执行

本轮代码结果：

- [x] `/experience` fresh 生成前会渲染上一页和下一页 PDF 页面图
- [x] 前后页页面图会交给 `reader_mm_parser_model`（当前默认 `qwen3-vl-flash`）提取参考文本
- [x] 注入给 agent 的上下文显式带有 `page / relation / reference_only / source / text`
- [x] prompt 明确约束：前后页上下文仅用于承接判断，不能覆盖当前页主证据
- [x] 生成后的 `plan.meta` 会记录实际使用过的前后页参考上下文元数据
- [x] generative / experience cache key 版本已升级，避免旧计划掩盖这条新链路

本轮待你手工确认：

- [ ] 打开 `/literature/78/experience?page=7`，点一次“刷新体验”
- [ ] 看 page 7 那类承接上一页的句子，是否比之前更少出现半截句/误接续
- [ ] 如果这一页明显承接 page 6 或 page 8，生成内容是否更自然地解释上下文，而不是把前后页内容直接当当前页证据
- [ ] 如果仍然出现承接错误，记录是更像：
  上一页漏带 / 下一页漏带 / OCR 提取脏 / 模型误把参考上下文当主证据

## 2026-03-11 Iteration 4

范围：

- `/literature/78/experience?page=7`
- `/literature/78/read/workbench?page=7`
- 目标：收尾 Phase 1 的显式空态与最小主链路回归

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：`7 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 hook warning
- [ ] `npm --prefix frontend run build`
  结果：本轮再次卡在 `vite build`，没有拿到稳定退出结果

本轮代码结果：

- [x] `/experience` 增加显式空态：
  无 KB、无 PDF、无 cached compose payload、无 experience plan
- [x] `/workbench` 增加显式空态：
  无 KB、无 PDF、无 cached compose payload、无 generative plan
- [x] 新增最小主链路回归：
  cached seed -> fresh full plan -> cached hit after reload

本轮待你手工确认：

- [ ] 打开 `/literature/78/experience?page=7`，如果当前没有 KB，页面会明确提示“未绑定知识库”，而不是像报错
- [ ] 如果遇到 compose/plan 缺失，页面会显示明确空态，不再只剩 `Network Error`
- [ ] 打开 `/literature/78/read/workbench?page=7`，在没有底座或没有 plan 时也能看到明确状态说明
- [ ] 正常页面情况下，这些空态提示不会遮住已有可用内容

## 2026-03-11 Iteration 5

范围：

- `/literature/78/experience?page=7`
- 目标：开始落 Phase 2 的最小 contract，把用户可见展示文案从 raw evidence 里分层出来

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`26 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "test_reader_experience_flow_should_progress_from_seed_to_full_plan_then_cache_hit" -q`
  结果：`1 passed`
- [ ] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：当前环境中 180s 超时，未拿到失败栈；已确认最小 `reader_experience_flow` 回归通过
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 hook warning
- [ ] `npm --prefix frontend run build`
  结果：本轮未执行；Docker/WSL bind mount 下 `vite build` 仍是已知慢构建项

本轮代码结果：

- [x] 在现有 schema 上增加第一版 `display copy` contract：
  `claim.display_text`、`hero.display_*`、`section.display_*`、`module.display_*`、`widget.display_*`
- [x] raw evidence 相关字段保持原样，不直接篡改论文原文或 provenance
- [x] runtime 在 finalize 阶段会为用户可见模块补 display-copy，优先给出中文展示文案
- [x] `/experience` 与 `/workbench` 前端开始优先消费 `display_*`，原始字段只做回退
- [x] generative / experience cache key 再次升级，避免旧 plan 遮住新的 display-copy contract

本轮待你手工确认：

- [ ] `/experience` 首屏标题、摘要、section 标题、资源卡、交互卡是否更像“中文展示层”，而不是直接复读英文原文
- [ ] 论文原文证据本身是否仍保持在正文/证据链里，没有被我“翻译覆盖”
- [ ] 如果还有英文，记录它更像落在：
  claim 卡片 / resource summary / widget panel / question card / glossary definition

## 2026-03-11 Iteration 6

范围：

- `/literature/78/experience?page=7`
- 目标：从“模块先生成、页面后拼装”继续往前推一步，加入页面级 storyboard / content budget，并消除同一模块跨 section 重复挂载

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`29 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：`7 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 warning

本轮代码结果：

- [x] `page_brief` 新增 `storyboard` 与 `content_budget`，让 plan 先决定页面叙事节拍和内容上限
- [x] `/experience` 现在会按 storyboard 的 beat 顺序组装 section，而不是默认把所有模块都抬出来
- [x] claim card / hooks / resource modules / explainer modules / widget blocks 都开始受内容预算约束
- [x] 资源、解释模块、控件现在采用 section-exclusive 归属：
  同一个模块不会同时出现在 `focus_stage`、`supporting_resources`、`question_lab` 多个 section 中
- [x] 默认不再把 `story_map` 放进产品页主链路

本轮待你手工确认：

- [ ] `/experience` 页面内容是否比之前更少重复，不再出现“同一资源/控件在多个区域重复出现”
- [ ] 当页面没有足够高价值的问题或交互时，`question_lab` 可以不出现，而不是强行补一个弱模块
- [ ] 页面是否更像先有阅读节拍，再补资源和解释，而不是堆一页卡片
- [ ] 如果仍有明显重复，记录它属于：
  hero/claim、explainer、resource、widget、question 哪一类重复

## 2026-03-11 Iteration 7

范围：

- `/literature/78/experience?page=7`
- 目标：把 Phase 2 的 contract validator 接进 generative / experience 两条运行时出口，避免 malformed plan 直接漏到 renderer

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`30 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：`7 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个 warning

本轮代码结果：

- [x] `_finalize_plan` 现在会先规范化 `story_substrate` / `page_brief`，再通过 generative plan schema 校验
- [x] `build_experience_plan` 现在会在返回前通过 experience plan schema 校验
- [x] 对坏的 `storyboard` / `content_budget` / claims / turns 会自动做 contract-normalization，而不是把坏字段原样漏给前端
- [x] 如果 generative plan contract 校验失败，运行时会降级到安全 fallback plan，而不是直接把坏 plan 继续传下去
- [x] `meta.contract_validation` 已写入 generative / experience plan，便于后续观测

本轮待你手工确认：

- [ ] `/experience` 现在是否更稳定，不会因为某次生成字段缺失就直接变成一堆坏卡片或空白区块
- [ ] 如果某页 plan 比较弱，页面是否更像“保守降级”而不是“坏计划硬渲染”
- [ ] 如果你看到明显异常，优先记录它更像：
  contract-normalization 不够 / fallback 触发过多 / 仍有重复内容 / 资源质量问题

## 2026-03-11 Iteration 8

范围：

- `/literature/78/experience?page=7`
- `/literature/78/read/workbench?page=7`
- 目标：收掉 Phase 1 最后一项，把 `/experience` 和 `/workbench` 的 cached/fresh/seed/loading 统一到同一套 surface loader / state machine

自动化结果：

- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning，无新增 error
- [ ] `cd frontend && npx tsc -b --pretty false`
  结果：当前环境里执行明显偏慢，没有在本轮拿到稳定退出结果；未把它算通过

本轮代码结果：

- [x] 新增共享 `readerSurfaceLoader`
- [x] `/experience` 改为复用统一 loader，不再自己维护 cached -> seed -> fresh -> polling 的私有分支
- [x] `/workbench` 改为复用同一 loader，不再保留单独的 compose/generative 加载链路
- [x] 统一了 surface state 语义：
  `loading_cached / showing_seed / refreshing_fresh / ready / partial_error / hard_error`
- [x] 统一了 cache state 语义：
  compose / plan / experience cache layer 与 hit 状态集中返回

本轮待你手工确认：

- [ ] `/experience` 点“刷新体验”后，行为是否更一致：先保留已有内容，再进入后台刷新，而不是让用户猜按钮有没有生效
- [ ] `/workbench` 刷新后，状态提示和 `/experience` 是否更接近，不再像另一套完全不同的加载体系
- [ ] 如果页面失败，是否更像“部分可恢复”或“硬失败”两类明确状态，而不是一堆零散 loading/error 组合

## 2026-03-11 Iteration 9

范围：

- `/literature/78/experience?page=7`
- 目标：把 Phase 2 的 unified block contract 以最小侵入方式接进 schema/runtime/renderer，继续减少前端对旧 module buckets 的依赖

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：`7 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning

本轮代码结果：

- [x] `ReaderExperienceSection` 新增统一 `blocks` 字段
- [x] runtime 在 experience plan validate 前会从 `resource_module_ids / interaction_module_ids / widget_ids` 补齐 section-level `blocks`
- [x] `blocks` 目前已带上：
  `block_id / block_type / ref_id / variant / target_ids / priority / state / data_requirements / fallback_policy / user_actions / agent_actions`
- [x] `/experience` renderer 现在优先按 `section.blocks` 解析资源、解释模块、控件，再回退旧字段
- [x] 旧字段继续保留，避免一次性打断现有缓存和前端兼容

本轮待你手工确认：

- [ ] `/experience` 页面结构是否继续稳定，没有因为换成 `blocks` 优先执行而出现丢模块或错位
- [ ] 页面内容是否更像由统一 block contract 驱动，而不是前端分别拼三套 id 列表
- [ ] 如果出现问题，记录它更像：
  block 引用缺失 / block 顺序不对 / renderer 回退逻辑有误

## 2026-03-11 Iteration 10

范围：

- `/literature/78/experience?page=7`
- 目标：补全 section-level `blocks` contract 的核心字段，并让 `/experience` 更稳定地按 block priority 执行

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning

本轮代码结果：

- [x] `ReaderExperienceBlockRef` 增加 `version`
- [x] runtime 会为已有 block 自动补 `version=block_ref_v1`
- [x] runtime 会按 `priority` 稳定排序 section `blocks`
- [x] `/experience` 新增统一 block 解析 helper，优先按 block 顺序拿 resource / interaction / widget 引用，再回退旧字段

本轮待你手工确认：

- [ ] `/experience` 各 section 内模块顺序是否继续稳定，没有因为 block priority 排序引入错位
- [ ] 页面是否继续正常显示，没有因为 `blocks.version` 或排序补全导致 section 内容丢失

## 2026-03-11 Iteration 11

范围：

- `/literature/78/experience?page=7`
- 目标：继续收 Phase 2，把 block contract 从静态引用推进到最小行为语义，避免“只是模板里塞内容”

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning

本轮代码结果：

- [x] runtime 会根据 block 类型和 payload 自动补 `user_actions`
- [x] runtime 会根据 block 类型和 payload 自动补 `agent_actions`
- [x] `RelatedResourceCard / GlossaryPanel / QuestionStarterPanel / figure-focus-accordion` 已带上不同的行为语义
- [x] block 行为语义仍然来源于 plan/runtime contract，不是前端模板硬编码

本轮待你手工确认：

- [ ] `/experience` 的 block 仍然稳定显示，没有因为行为语义补全影响现有内容渲染
- [ ] 如果你观察页面结构，是否更能理解这些区块是“可交互的页面对象”，而不只是栏目卡片

## 2026-03-11 Iteration 12

范围：

- `/literature/78/experience?page=7`
- 目标：把 block contract 从动作字符串推进到结构化协议对象，继续避免“模板里塞 AI 文案”的路线

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning

本轮代码结果：

- [x] `ReaderExperienceBlockRef` 新增结构化 `ui_actions`
- [x] `ReaderExperienceBlockRef` 新增结构化 `event_bindings`
- [x] runtime 会从 block 类型、目标和现有动作语义自动生成 `ui_actions / event_bindings`
- [x] `QuestionStarterPanel / GlossaryPanel / RelatedResourceCard / figure-focus-accordion` 已带上最小交互协议对象

本轮待你手工确认：

- [ ] `/experience` 页面显示是否保持稳定，没有因为 block 增加协议字段出现渲染异常
- [ ] 从系统角度看，这些 block 是否更像“可被触发和回传的页面对象”，而不再只是静态卡片

## 2026-03-11 Iteration 13

范围：

- `/literature/78/experience?page=7`
- 目标：把最小 block 协议真正接到 `/experience` renderer，验证这页不再只是模板卡片展示

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning

本轮代码结果：

- [x] `/experience` 新增最小 action dispatcher
- [x] 资源卡、术语卡、问题卡、图解面板会消费 block `ui_actions`
- [x] 页面会记录最近一次触发的 block 协议事件
- [x] `focus_target / return_to_reader` 已接到页面焦点状态，不再只是静态协议对象

本轮待你手工确认：

- [ ] 点击 `/experience` 里的图解面板、问题项、资源来源按钮后，页面顶部是否会出现“已触发”的协议反馈
- [ ] 点击图解面板后，焦点区域是否会跟着切换或保持在被指向的目标上，而不只是无反馈
- [ ] 整体上这页是否更像“plan 驱动的页面对象在响应动作”，而不是模板卡片静态展示

## 2026-03-11 Iteration 14

范围：

- `/literature/78/experience?page=7`
- 目标：修复本轮用户反馈的运行时错误，并把 action dispatch 收成可复用的页面内 action bus

自动化结果：

- [x] `npm --prefix frontend run lint`
  结果：0 error，9 个既有 warning
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：`7 passed`

本轮代码结果：

- [x] 修复 `/experience` 运行时错误：`hasNonDraftExperiencePlan is not defined`
- [x] 新增 `useExperienceActionBus`
- [x] `focus_target / return_to_reader / 最近一次协议事件反馈` 已从页面内零散状态收敛到 action bus
- [x] 本轮设计记录已落 `docs/skills`
- [x] 本轮测试记录已落 `docs/tests`

本轮待你手工确认：

- [ ] `http://localhost:3000/literature/78/experience?page=7` 是否继续稳定可打开，不再出现 `hasNonDraftExperiencePlan is not defined`
- [ ] 点击资源、问题、图解交互后，页面反馈是否仍然正常

## 2026-03-11 Iteration 15

范围：

- `/literature/78/experience?page=7`
- 目标：把 `/experience` 从“页面文件里混合渲染逻辑”推进到独立 renderer 执行层，为 Phase 3 的 block registry / shared runtime 继续收口

自动化结果：

- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
  结果：`31 passed`
- [x] `backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q`
  结果：`7 passed`
- [x] `backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`
  结果：`Contract alignment guard passed.`
- [x] `backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py`
  结果：`Broad exception guard passed.`
- [x] `npm --prefix frontend run lint`
  结果：`0 error / 3 warnings`
- [x] `cd frontend && ./node_modules/.bin/eslint src/pages/literature/PaperReaderExperiencePage.tsx src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/useExperienceActionBus.ts`
  结果：退出码 `0`
- [x] `cd frontend && ./node_modules/.bin/tsc -p tsconfig.json --noEmit`
  结果：退出码 `0`

浏览器结果：

- [x] `cmd.exe /c curl -I "http://localhost:3000/literature/78/experience?page=7"`
  结果：`HTTP/1.1 200 OK`
- [x] Playwright 同会话登录并打开 `/literature/78/experience?page=7`
- [x] console error = `0`
- [x] 页面截图：
  `output/playwright/experience-renderer-extraction-2026-03-11.png`

本轮代码结果：

- [x] 新增 `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`
- [x] `/experience` 的 section/layout/block 渲染主逻辑已迁入独立 renderer
- [x] `PaperReaderExperiencePage.tsx` 现在主要保留 route params、shared loader、alerts、details、顶层 header
- [x] renderer 开始成为真正的 block/layout 执行层，而不是继续把执行逻辑散落在页面文件里
- [x] `useExperienceActionBus` 继续作为 renderer-side action dispatch 的唯一入口
- [x] 本轮设计记录已落 `docs/skills`
- [x] 本轮测试记录已落 `docs/tests`

本轮待你手工确认：

- [ ] `http://localhost:3000/literature/78/experience?page=7` 是否继续稳定打开，没有因为 renderer 抽离出现空白或异常区块
- [ ] 页面是否仍然像“plan 驱动的展开阅读页”，而不是抽离后退回普通模板
- [ ] 如果你点击图解/资源/问题卡，交互反馈是否仍然正常

## 2026-03-11 Iteration 16

范围：

- `/literature/78/experience?page=7`
- 目标：把 renderer 里的 block family 分支继续收敛成 registry，而不是让 renderer 再次膨胀成模板分支中心

自动化结果：

- [x] `cd frontend && ./node_modules/.bin/eslint src/pages/literature/PaperReaderExperiencePage.tsx src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/useExperienceActionBus.ts src/pages/literature/experienceBlockRegistry.tsx`
  结果：退出码 `0`
- [x] `cd frontend && ./node_modules/.bin/tsc -p tsconfig.json --noEmit`
  结果：退出码 `0`
- [x] `cmd.exe /c curl -I "http://localhost:3000/literature/78/experience?page=7"`
  结果：`HTTP/1.1 200 OK`

浏览器结果：

- [x] Playwright 复用已有登录会话检查 `/literature/78/experience?page=7`
- [x] 页面截图仍然正常渲染
- [ ] 当前开发态会话里捕获到一次 Vite HMR 404：
  `GenerativeExperienceRenderer.tsx?t=...`

本轮代码结果：

- [x] 新增 `frontend/src/pages/literature/experienceBlockRegistry.tsx`
- [x] `GenerativeExperienceRenderer` 已改为通过 registry 获取 block definition
- [x] renderer 不再直接硬编码资源/交互/widget family 的全部细节渲染逻辑
- [x] `GlossaryPanel / QuestionStarterPanel / FigureExplainPanel / RelatedResourceCard / figure-focus-accordion` 已有注册定义
- [x] 本轮设计记录已落 `docs/skills`
- [x] 本轮测试记录已落 `docs/tests`

问题记录：

- [x] 浏览器层发现的是开发态 HMR 抖动，不是页面业务逻辑报错
- [x] 该问题已记录在 `docs/tests`
- [x] 已执行 `docker compose restart frontend`，宿主机路由仍返回 `HTTP/1.1 200 OK`

本轮待你手工确认：

- [ ] 你刷新页面后，`/experience` 是否继续稳定显示，不再看到明显的模块缺失
- [ ] 页面看起来是否更像“由注册 block 驱动的执行层”，而不是 renderer 文件里继续堆越来越多模板分支
