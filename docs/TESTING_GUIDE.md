# 测试指南（2026-02）

## 1. 目标
- 提供“可复制”的本地后端测试环境准备流程。
- 统一门禁、单测、前端构建、容器与 E2E 的执行入口。

## 2. 快速准备（宿主环境）

### 2.1 一键准备后端测试环境
```powershell
chcp 65001
.\scripts\bootstrap_backend_test_env.ps1 -UpgradePip
```

网络不稳定时可指定镜像源：
```powershell
.\scripts\bootstrap_backend_test_env.ps1 -UpgradePip -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.2 依赖自检（脚本内已做，可单独复验）
```powershell
.\.venv-ragtest\Scripts\python.exe -c "import fastapi,sqlalchemy,pydantic_settings,loguru,pytest;print('OK')"
```

## 3. 后端检查与测试

### 3.1 门禁
```powershell
.\.venv-ragtest\Scripts\python.exe backend/checks/check_no_new_broad_excepts.py
.\.venv-ragtest\Scripts\python.exe backend/checks/check_contract_alignment.py
```

### 3.2 关键最小回归
```powershell
.\.venv-ragtest\Scripts\python.exe -m pytest backend/tests/test_contract_alignment_check.py -q
.\.venv-ragtest\Scripts\python.exe -m pytest backend/tests/test_codelab_runner_unavailable.py -q
.\.venv-ragtest\Scripts\python.exe -m pytest backend/tests/test_codelab_timeout_enforced.py -q
.\.venv-ragtest\Scripts\python.exe -m pytest backend/tests/test_literature_reader_api.py -q
```

### 3.3 全集（时间较长）
```powershell
.\.venv-ragtest\Scripts\python.exe -m pytest backend/tests -q
```

## 4. 前端质量检查
```powershell
cd frontend
npm ci
npm run lint
npm run build
```

## 5. 容器闭环验证

```powershell
docker compose up -d --build backend frontend
docker compose ps
docker compose logs --tail 100 backend
docker compose logs --tail 100 frontend
```

- 默认对外后端地址：`http://localhost:8888`
- 默认前端地址：`http://localhost:3000`

## 6. E2E / 验收脚本
```powershell
./acceptance_tests.ps1
./e2e/role_business_api_smoke_e2e.ps1
./e2e/remaining_modules_api_smoke_e2e.ps1
./e2e/student_role_smoke_e2e.ps1
./e2e/role_route_smoke_e2e.ps1 -Role mentor
./e2e/role_route_smoke_e2e.ps1 -Role admin
./e2e/mcp_settings_login_e2e.ps1
./e2e/knowledge_upload_pipeline_e2e.ps1
```

## 7. 常见问题

### 7.1 `ModuleNotFoundError: loguru` / `fastapi`
- 原因：未使用项目测试虚拟环境或依赖未安装完整。
- 处理：重新执行 `.\scripts\bootstrap_backend_test_env.ps1 -UpgradePip`。

### 7.2 文档和命令端口不一致
- 统一使用 `8888` 访问后端（Compose 默认映射 `8888:8000`）。

### 7.3 前端构建有 chunk warning
- 属于性能告警，不阻塞构建通过；需在性能治理阶段继续拆包。

## 8. `/read` 证据链回归约束

以下规则适用于所有会影响 `/literature/:id/read` 证据预览、高光、锚点菜单、表格证据、公式证据的改动。

### 8.1 不可破坏的实现约束
- DocMind 几何是真值来源；`uniqueId -> blocks[].pos` 是 `/read` 高光与预览的默认主链。
- 不要为了局部问题切换 `/read` 的全局预览坐标系；表格、公式等局部修复不能改写整页 evidence preview 主路径。
- `geometry.page_width/page_height` 与 `bbox_hint.page_width/page_height` 必须对应真实 DocMind 页图尺寸，不能退回“最大内容边界”推断值。
- `layout_uid_v1` 相关改动必须保留 `证据` 菜单、hover evidence preview、pinned evidence 三条行为链。
- 表格重建、公式重建、fallback 节点注入，都不能绕开通用节点级证据链。

### 8.2 必做回归
- 任何改动触及以下文件之一，都必须执行 `/read` 证据链回归：
  - `frontend/src/pages/literature/PaperReaderPage.tsx`
  - `frontend/src/pages/literature/readerComponents/index.tsx`
  - `backend/app/services/literature_reader_compose_service.py`
- 至少验证一页 prose-heavy 和一页 table-heavy 页面：
  - `http://localhost:3000/literature/85/read?page=3&kb=84`
  - `http://localhost:3000/literature/85/read?page=7&kb=84`
- 最低验收项：
  - `...` 菜单里仍有 `证据`
  - hover 能拉起 evidence preview
  - pinned evidence 与 hover evidence 对齐到同一 PDF 区域
  - 新生成 payload 的 `page_width/page_height` 与 DocMind 页图尺寸一致
