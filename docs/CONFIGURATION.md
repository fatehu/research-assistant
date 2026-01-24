# 配置管理说明

## 概述

所有可配置参数都通过环境变量管理，支持 `.env` 文件。配置类位于 `backend/app/config.py`。

## 配置分类

### 1. LLM 推理参数

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `LLM_TEMPERATURE` | float | 0.7 | LLM 默认温度 (0-1, 越高越随机) |
| `LLM_MAX_TOKENS` | int | 4096 | LLM 最大输出 tokens |

**影响范围**: 所有 LLM 调用（对话、Agent 推理等）

**调整建议**:
- 需要更确定性的输出：降低 temperature (0.3-0.5)
- 需要更创造性的输出：提高 temperature (0.8-1.0)
- 长文本输出：增加 max_tokens

---

### 2. ReAct Agent 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `REACT_MAX_ITERATIONS` | int | 10 | Agent 最大推理迭代次数 |
| `REACT_TEMPERATURE` | float | 0.7 | Agent 推理温度 |
| `REACT_OUTPUT_MAX_LENGTH` | int | 500 | 工具输出显示的最大字符数 |

**影响范围**: CodeLab Agent 和通用 Agent

**调整建议**:
- 复杂任务需要更多迭代：增加 `REACT_MAX_ITERATIONS` (15-20)
- 简单任务减少延迟：降低 `REACT_MAX_ITERATIONS` (3-5)
- 看到完整工具输出：增加 `REACT_OUTPUT_MAX_LENGTH`

---

### 3. 代码执行配置

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `CODE_EXECUTION_TIMEOUT` | int | 30 | 单次代码执行超时（秒） |
| `KERNEL_IDLE_TIMEOUT` | int | 7200 | 内核空闲超时（秒），默认 2 小时 |

**影响范围**: CodeLab 代码执行

**调整建议**:
- 长时间运行的代码（训练模型）：增加 `CODE_EXECUTION_TIMEOUT` (60-300)
- 资源紧张：降低 `KERNEL_IDLE_TIMEOUT` (1800 = 30分钟)

---

### 4. Notebook 上下文配置

这些参数控制 Agent 获取的 Notebook 上下文范围，影响 **token 消耗** 和 **上下文质量**。

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `NOTEBOOK_CONTEXT_CELLS` | int | 5 | Agent 上下文包含的最近 Cell 数量 |
| `NOTEBOOK_CONTEXT_CELL_MAX_LENGTH` | int | 200 | 单个 Cell 代码预览的最大字符数 |
| `NOTEBOOK_CONTEXT_VARIABLES` | int | 15 | Agent 上下文包含的最大变量数量 |
| `NOTEBOOK_CONTEXT_OUTPUT_CELLS` | int | 5 | recent_outputs 包含的 Cell 数量 |

**影响范围**: Agent 对话时的 Notebook 上下文

**调整建议**:

| 场景 | 调整方式 |
|------|----------|
| Token 预算有限 | 减少 `NOTEBOOK_CONTEXT_CELLS` 和 `NOTEBOOK_CONTEXT_VARIABLES` |
| 复杂数据分析任务 | 增加 `NOTEBOOK_CONTEXT_CELLS` (8-10) 和 `NOTEBOOK_CONTEXT_VARIABLES` (20-30) |
| 长代码 Cell | 增加 `NOTEBOOK_CONTEXT_CELL_MAX_LENGTH` (500-1000) |

---

## 配置生效机制详解

### Pydantic Settings 自动绑定

```python
# backend/app/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",           # 1. 读取 .env 文件
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # 2. 字段名自动映射到环境变量（转换为大写 + 下划线）
    react_max_iterations: int = 10   # ← 对应 REACT_MAX_ITERATIONS
    notebook_context_cells: int = 5  # ← 对应 NOTEBOOK_CONTEXT_CELLS
```

**Pydantic 自动做的事情**：
1. 启动时读取 `.env` 文件
2. 将字段名 `react_max_iterations` 转换为环境变量名 `REACT_MAX_ITERATIONS`
3. 如果环境变量存在，使用环境变量的值
4. 如果不存在，使用代码中的默认值

### 代码中如何使用

```python
# 在任何需要的地方导入
from app.config import settings

# 直接使用 settings.xxx
agent = ReActAgent(
    max_iterations=settings.react_max_iterations  # 使用配置值
)

# Notebook 上下文构建
for cell in code_cells[-settings.notebook_context_cells:]:
    source = cell.get('source', '')[:settings.notebook_context_cell_max_length]
    ...
```

---

## 配置验证方法

### 方法 1: 启动日志（自动）

启动后端服务后，会自动打印所有配置：

```bash
docker-compose up backend
```

日志输出示例：
```
🚀 启动 AI科研助手 v1.0.0
📦 默认 LLM 提供商: deepseek
==================================================
📋 当前配置:
  LLM_TEMPERATURE: 0.7
  LLM_MAX_TOKENS: 4096
  REACT_MAX_ITERATIONS: 10
  REACT_OUTPUT_MAX_LENGTH: 500
  CODE_EXECUTION_TIMEOUT: 30s
  KERNEL_IDLE_TIMEOUT: 7200s
  NOTEBOOK_CONTEXT_CELLS: 5
  NOTEBOOK_CONTEXT_CELL_MAX_LENGTH: 200
  NOTEBOOK_CONTEXT_VARIABLES: 15
==================================================
```

### 方法 2: API 端点（推荐）

访问配置检查端点：

```bash
curl http://localhost:8000/health/config | jq
```

响应示例：
```json
{
  "llm": {
    "provider": "deepseek",
    "temperature": 0.7,
    "max_tokens": 4096
  },
  "react_agent": {
    "max_iterations": 10,
    "temperature": 0.7,
    "output_max_length": 500
  },
  "code_execution": {
    "timeout_seconds": 30,
    "kernel_idle_timeout_seconds": 7200
  },
  "notebook_context": {
    "cells": 5,
    "cell_max_length": 200,
    "variables": 15,
    "output_cells": 5
  }
}
```

### 方法 3: Python REPL

```bash
docker-compose exec backend python
```

```python
from app.config import settings

print(f"REACT_MAX_ITERATIONS: {settings.react_max_iterations}")
print(f"NOTEBOOK_CONTEXT_CELLS: {settings.notebook_context_cells}")
print(f"CODE_EXECUTION_TIMEOUT: {settings.code_execution_timeout}")
```

### 方法 4: 修改配置并验证

1. 修改 `.env` 文件：
```bash
REACT_MAX_ITERATIONS=20
NOTEBOOK_CONTEXT_CELLS=8
```

2. 重启服务：
```bash
docker-compose restart backend
```

3. 检查配置是否更新：
```bash
curl http://localhost:8000/health/config | jq '.react_agent.max_iterations'
# 应该返回: 20
```

---

## 配置优先级

```
环境变量 > .env 文件 > config.py 默认值
```

---

## 使用示例

### 方式 1: 修改 .env 文件

```bash
# .env
REACT_MAX_ITERATIONS=15
LLM_TEMPERATURE=0.5
CODE_EXECUTION_TIMEOUT=60
NOTEBOOK_CONTEXT_CELLS=8
```

### 方式 2: Docker Compose 环境变量

```yaml
# docker-compose.yml
services:
  backend:
    environment:
      - REACT_MAX_ITERATIONS=15
      - LLM_TEMPERATURE=0.5
```

### 方式 3: 命令行

```bash
REACT_MAX_ITERATIONS=15 python -m uvicorn app.main:app
```

---

## 配置验证

启动时会加载配置并缓存。要验证配置是否生效：

```python
# 在 Python 中检查
from app.config import settings

print(f"max_iterations: {settings.react_max_iterations}")
print(f"temperature: {settings.llm_temperature}")
print(f"context_cells: {settings.notebook_context_cells}")
```

或通过日志查看：

```bash
docker-compose logs backend | grep -i "config\|settings"
```

---

## 配置速查表

### 性能优化配置

```bash
# 减少 token 消耗
NOTEBOOK_CONTEXT_CELLS=3
NOTEBOOK_CONTEXT_CELL_MAX_LENGTH=100
NOTEBOOK_CONTEXT_VARIABLES=10
REACT_OUTPUT_MAX_LENGTH=300

# 加快响应
REACT_MAX_ITERATIONS=5
CODE_EXECUTION_TIMEOUT=15
```

### 复杂任务配置

```bash
# 支持复杂推理
REACT_MAX_ITERATIONS=20
LLM_MAX_TOKENS=8192

# 更多上下文
NOTEBOOK_CONTEXT_CELLS=10
NOTEBOOK_CONTEXT_CELL_MAX_LENGTH=500
NOTEBOOK_CONTEXT_VARIABLES=30
```

### 长时间运行任务配置

```bash
# 训练模型、大数据处理
CODE_EXECUTION_TIMEOUT=300
KERNEL_IDLE_TIMEOUT=14400  # 4 小时
```

---

## 配置文件位置

| 文件 | 说明 |
|------|------|
| `backend/app/config.py` | 配置类定义，包含默认值 |
| `.env.example` | 环境变量模板 |
| `.env` | 实际使用的环境变量（不提交到 Git） |
| `docker-compose.yml` | Docker 环境变量覆盖 |
