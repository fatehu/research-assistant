# CI PowerShell curl 跨平台修复测试记录（2026-02-16 09:31）

## 1. 测试环境
- 分支：`feature/ci-gpu-reset-fix-20260216`
- 服务：本地 Docker 栈（backend/frontend/postgres/redis/codelab-runner）
- 目标：验证 `curl.exe` 硬编码改造后脚本可正常执行

## 2. 执行命令
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./e2e/remaining_modules_api_smoke_e2e.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./e2e/knowledge_upload_pipeline_e2e.ps1
```

## 3. 结果
- `remaining_modules_api_smoke_e2e.ps1`：全量通过（含 `D-16`、上传/处理/检索链路）。
- `knowledge_upload_pipeline_e2e.ps1`：通过，文档状态到 `completed`，检索命中正常。

## 4. 结论
- 跨平台 curl 修复生效。
- 脚本已兼容 Windows 与 Linux runner 的 curl 命令差异。
