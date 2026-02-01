# Notebook Cell 管理工具增强补丁

## 问题描述

原始的 `notebook_cell` 工具存在以下问题：
1. `get` 操作显示 "Cell 1, Cell 2..." 序号，但不显示实际的 UUID cell_id
2. 删除时需要 UUID，但 AI 只看到序号，导致无法正确定位单元格
3. 缺少通过索引操作的支持
4. 缺少批量操作和智能清理功能
5. **【关键】** 删除操作没有同步到数据库，刷新后数据恢复

## 优化内容

### 1. 增强 NotebookCellTool

- **支持索引操作**：现在可以使用 `cell_index`（从1开始）来定位单元格
- **改进 get 显示**：显示每个单元格的完整 ID 和索引
- **新增 get_one 操作**：获取单个单元格详情
- **新增 move 操作**：移动单元格位置
- **智能解析**：如果传入数字字符串，会自动尝试解析为索引

### 2. 新增 NotebookCellCleanupTool

专门用于智能清理单元格的工具，支持以下策略：
- `preview`: 预览分析，显示可清理的单元格
- `duplicates`: 删除重复内容的单元格
- `empty`: 删除空白单元格
- `unexecuted`: 删除未执行的代码单元格
- `ai_created`: 删除AI创建的单元格
- `by_indices`: 批量删除指定索引的单元格

### 3. 新增 ErrorDiagnosisTool

错误诊断工具，帮助分析 Python 错误并提供解决方案。

### 4. 改进 ReAct Agent 系统提示词

- 详细说明了 notebook_cell 工具的使用方法
- 添加了典型工作流程示例
- 强调了通过索引操作的方式

### 5. 【重要】修复数据库持久化问题

添加了删除操作的数据库同步，确保刷新/重启后数据不丢失。

## 安装方法

### 步骤 1：备份原文件

```bash
cp backend/app/services/notebook_tools.py backend/app/services/notebook_tools.py.bak
cp backend/app/services/react_agent.py backend/app/services/react_agent.py.bak
cp backend/app/api/codelab.py backend/app/api/codelab.py.bak
```

### 步骤 2：覆盖服务层文件

```bash
# 解压补丁包
unzip notebook_cell_enhancement_patch.zip

# 复制 notebook_tools.py 和 react_agent.py
cp patch/backend/app/services/notebook_tools.py backend/app/services/
cp patch/backend/app/services/react_agent.py backend/app/services/
```

### 步骤 3：手动修改 codelab.py

由于 codelab.py 文件较大，需要手动修改。

打开 `backend/app/api/codelab.py`，找到约第 1289 行（搜索 `elif updated_cell:`）。

**在这段代码后面：**
```python
elif updated_cell:
    # 更新 cell
    await service.update_cell(
        notebook_id, user_id,
        updated_cell.get('id'),
        source=updated_cell.get('source'),
        cell_type=updated_cell.get('cell_type'),
        outputs=updated_cell.get('outputs'),
        execution_count=updated_cell.get('execution_count')
    )
    logger.info(f"[Agent] Cell 更新已同步到数据库: {updated_cell.get('id')}")
```

**添加以下代码（在 `except Exception as e:` 之前）：**
```python
# 【新增】处理删除操作
deleted_ids = tool_data.get('deleted_ids', [])
if deleted_ids:
    for del_id in deleted_ids:
        try:
            await service.delete_cell(notebook_id, user_id, del_id)
            logger.info(f"[Agent] Cell 删除已同步到数据库: {del_id}")
        except Exception as del_e:
            logger.warning(f"删除 cell {del_id} 失败: {del_e}")
```

详细说明请参见 `patch/backend/app/api/codelab_patch_instructions.py`

### 步骤 4：重启服务

```bash
docker-compose restart backend
# 或
docker-compose down && docker-compose up -d
```

## 文件清单

```
patch/
├── README.md                                      # 本说明文件
└── backend/
    └── app/
        ├── api/
        │   └── codelab_patch_instructions.py      # codelab.py 修改说明
        └── services/
            ├── notebook_tools.py                  # 增强版 notebook 工具集
            └── react_agent.py                     # 改进版 ReAct Agent
```

## 使用示例

### 获取所有单元格

AI 现在会调用：
```json
{"tool": "notebook_cell", "input": {"action": "get"}}
```

返回结果示例：
```
📓 Notebook 共有 5 个单元格:
============================================================

💻 【索引 1】[7] 📤有输出
   ID: abc123-def456-789...
   内容: import numpy as np...

💻 【索引 2】[未执行]
   ID: xyz789-abc123-456...
   内容: # 生成正弦函数...

============================================================
💡 提示: 删除/更新时可使用 cell_index（如 1, 2, 3）或 cell_id
```

### 通过索引删除单元格

```json
{"tool": "notebook_cell", "input": {"action": "delete", "cell_index": 2}}
```

### 智能清理重复单元格

```json
{"tool": "notebook_cleanup", "input": {"strategy": "preview"}}
```

然后根据分析结果：
```json
{"tool": "notebook_cleanup", "input": {"strategy": "duplicates"}}
```

### 批量删除指定单元格

```json
{"tool": "notebook_cleanup", "input": {"strategy": "by_indices", "indices": [2, 3, 5]}}
```

## 注意事项

1. 所有修改操作（delete, update, add, move, cleanup）仍需用户授权
2. `cell_index` 是从 1 开始的，与显示一致
3. 清理操作支持 `dry_run: true` 参数，可以预览不实际删除
4. 建议在执行清理前先使用 `preview` 策略分析
5. **修改 codelab.py 后必须重启服务才能生效**

## 回滚方法

如果需要回滚到原版本：

```bash
# 恢复备份
cp backend/app/services/notebook_tools.py.bak backend/app/services/notebook_tools.py
cp backend/app/services/react_agent.py.bak backend/app/services/react_agent.py
cp backend/app/api/codelab.py.bak backend/app/api/codelab.py

# 重启服务
docker-compose restart backend
```

## 版本信息

- 补丁版本：1.1.0（修复持久化问题）
- 适用项目：research-assistant
- 创建日期：2026-02-01
