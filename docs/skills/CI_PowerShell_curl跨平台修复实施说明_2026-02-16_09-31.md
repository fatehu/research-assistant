# CI PowerShell curl 跨平台修复实施说明（2026-02-16 09:31）

## 1. 背景
- GPU 报错修复后，PR `api-smoke` 继续执行到脚本阶段。
- 新失败点：Ubuntu runner 下 `curl.exe` 不存在，导致 `remaining_modules_api_smoke_e2e.ps1` 在上传文件用例失败。

## 2. 本次改造
- 文件：`e2e/remaining_modules_api_smoke_e2e.ps1`
  - 新增 `Get-CurlCommand`：优先找 `curl.exe`，否则回退 `curl`。
  - `Invoke-CurlMultipartUpload` 改为调用动态解析出的 curl 命令。
- 文件：`e2e/knowledge_upload_pipeline_e2e.ps1`
  - 同步新增 `Get-CurlCommand`。
  - `Invoke-FileUpload` 改为调用动态 curl 命令。

## 3. 设计取舍
- 保持原有脚本结构和接口不变，仅替换命令解析层。
- 保证 Windows 开发环境兼容（仍可命中 `curl.exe`），并支持 Linux runner（`curl`）。

## 4. 回滚方式
- 回滚上述两处脚本到本次提交前版本。

## 5. 预期效果
- CI 上 PowerShell 脚本不再因 `curl.exe` 缺失中断。
- `D-16 Invalid file upload is rejected` 与上传链路用例可继续执行。
