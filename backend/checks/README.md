# Backend Checks

本目录保存后端质量门禁脚本，默认在仓库根目录执行。

## 脚本列表

1. `python backend/checks/check_no_new_broad_excepts.py`
- 目的：禁止关键文件新增 `except Exception`，防止异常语义退化。

2. `python backend/checks/check_contract_alignment.py`
- 目的：校验前后端任务状态与前端默认 API/WS 端口契约一致，避免静默偏差。

## 建议执行顺序

1. `python backend/checks/check_no_new_broad_excepts.py`
2. `python backend/checks/check_contract_alignment.py`
3. 再执行对应阶段的 `pytest` 与前端 `lint/build`。

## 失败处理

- 先看脚本输出中的 `[tag]` 标识定位问题类别。
- 修复后重复执行同一脚本，直至通过。
- 失败原因与修复结论应同步记录到 `docs/test` 阶段测试文档。
