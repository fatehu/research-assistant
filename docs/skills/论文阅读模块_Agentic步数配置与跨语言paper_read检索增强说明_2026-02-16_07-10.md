# 论文阅读模块 Agentic 步数配置与跨语言 paper_read 检索增强说明

- 时间：2026-02-16 07:10
- 阶段目标：
1. 让论文阅读页 Agentic 询问最大步数支持独立 env 配置，去掉代码内硬编码上限。
2. 修复 `paper_read` 在英文论文场景下被中文模板词拖垮召回的问题。

## 本阶段改动
1. 增加配置项：`LITERATURE_AGENT_MAX_ITERATIONS`
- 文件：`backend/app/config.py`
- 默认值：`8`
- 作用：论文阅读模块 Agentic 流程可单独控制最大迭代步数，不再被 `<=4` 硬限制。

2. 更新环境变量样例
- 文件：`.env.example`
- 新增：`LITERATURE_AGENT_MAX_ITERATIONS=8`
- 说明：建议范围 `4-20`。

3. 运行日志可观测性补充
- 文件：`backend/app/main.py`
- 启动日志新增输出：`LITERATURE_AGENT_MAX_ITERATIONS`，便于确认 env 是否生效。

4. `paper_read` 跨语言检索增强
- 文件：`backend/app/api/literature.py`
- 增加中英术语桥接映射（如：研究方法 -> method/methodology/approach，结论 -> conclusion 等）。
- 当 query 含中文章节模板词时，自动补充英文学术章节回退词（abstract/introduction/method/results/discussion/conclusion）。
- 工具参数说明改为：优先使用用户原问题或同语言关键词。
- Agent 系统提示新增约束：调用 `paper_read` 时不要固定套用中文模板词。

5. 迭代步数解析逻辑统一
- 文件：`backend/app/api/literature.py`
- 新增 `_resolve_literature_agent_max_iterations()`：
  - 优先读取 `literature_agent_max_iterations`
  - 未配置则回退 `react_max_iterations`
  - 最终夹紧到 `2..20`
- Agent 创建处改为调用该函数。

## 生效与回滚
- 生效：修改 `.env` 后重启后端容器/进程即可。
- 回滚：
1. 删除 `LITERATURE_AGENT_MAX_ITERATIONS`，系统将回退到 `REACT_MAX_ITERATIONS`。
2. 若需彻底回退行为，可还原本次相关文件变更。
