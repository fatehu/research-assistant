# Local Structured PDF 外部评测策略

## 目标

这份文档定义 `local_structured_pdf` 的外部验证策略，避免后续优化只围着 `opendataloader-bench` 打转。

原则：

1. 外部数据集用于约束启发式泛化能力。
2. `opendataloader-bench` 继续作为最终回归门和对标分数来源。
3. 不允许为了外部集再写新的数据集特判；外部集只是帮助我们识别更一般的失败类型。

## 推荐顺序

### 第一优先级：READoc

原因：

- 任务形态最接近我们当前链路：整份 `PDF -> Markdown`
- 更像真实知识库入库文档，而不是单页视觉识别
- 适合检验：
  - heading role
  - front matter
  - section band
  - references / contents / appendix
  - 跨页正文与脚注

建议把 `READoc` 作为第一个“外部 holdout”。

### 第二优先级：OmniDocBench

原因：

- 页级、组件级覆盖更广
- 对阅读顺序、表格、公式、复杂版面更有诊断价值

但它和当前 `local_structured_pdf` 主链的输入输出契约不完全一致，更适合做第二阶段。

## 当前接入方式

已经提供了一个外部 suite manifest 模板：

- [local_structured_pdf_external_suites_v1.json](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/manifests/local_structured_pdf_external_suites_v1.json)

以及一个将外部 `PDF + Markdown` 配对语料整理成 holdout 目录的脚本：

- [prepare_external_pdf_markdown_holdout.py](/mnt/d/codefield/agent-platform/research-assistant/backend/scripts/prepare_external_pdf_markdown_holdout.py)

默认模板里预留了：

- `readoc_holdout`

目录约定：

- PDF: `backend/tmp/external/readoc/pdfs`
- GT Markdown: `backend/tmp/external/readoc/markdown`

## 准备 READoc holdout

如果你已经把 READoc 下载到某个本地目录，并且其中存在可配对的 `.pdf` 与 `.md` 文件，可以先运行：

```bash
python backend/scripts/prepare_external_pdf_markdown_holdout.py \
  --source-root /path/to/READoc \
  --output-root backend/tmp/external/readoc \
  --limit 200 \
  --manifest backend/eval/manifests/local_structured_pdf_external_suites_v1.json \
  --suite-name readoc_holdout
```

说明：

- 脚本会递归扫描 `source-root`，按相对路径配对 `.pdf` 和 `.md`
- 输出目录会生成：
  - `pdfs/`
  - `markdown/`
  - `holdout_manifest.json`
- 如果传入 `--manifest`，脚本会自动把 suite 注册到 manifest
- 如果路径里带有 `arxiv` / `github` 片段，脚本会自动推断 subset，并在 `--limit` 生效时优先做均衡采样

如果你拿到的是 Hugging Face 上 READoc 的原始发布形态，也就是：

- `arxiv.zip`
- `github.zip`
- `zenodo.zip`（如果后续版本包含）
- `arxiv_ground_truth/*.md`
- `github_ground_truth/*.md`
- `zenodo_ground_truth/*.md`

那么更适合直接用：

```bash
python backend/scripts/prepare_readoc_holdout.py \
  --source-root /path/to/READoc \
  --output-root backend/tmp/external/readoc \
  --subset arxiv \
  --subset github \
  --limit 200 \
  --manifest backend/eval/manifests/local_structured_pdf_external_suites_v1.json \
  --suite-name readoc_holdout
```

这个脚本会只解出被采样命中的 PDF，不需要先把整包 zip 全部展开。

## 运行方式

只跑外部 `READoc` suite：

```bash
docker compose exec -T backend sh -lc \
  'PYTHONPATH=/app python scripts/run_local_structured_pdf_eval_suites.py \
     --manifest eval/manifests/local_structured_pdf_external_suites_v1.json \
     --suite readoc_holdout \
     --heuristic-profile balanced \
     --skip-eval'
```

说明：

- `--suite` 现在支持只跑单个命名 suite，避免每次改 manifest。
- 如果要跑 evaluator，需要给 `run_local_structured_pdf_eval_suites.py` 提供可用的 evaluator Python 环境。

## 推荐工作流

1. 先在 `READoc` 这类外部集上观察失败类型。
2. 抽象成通用失败类别：
   - front matter confusion
   - section-band heading miss
   - references / contents page type confusion
   - mixed-layout reading order
3. 只实现通用处理器，不按外部集文档写特判。
4. 最后回到 `opendataloader-bench` 跑分，确认：
   - 没有破坏主 benchmark
   - `NID / MHS` 继续提升或至少不回退

## 现阶段判断

当前默认主链已经基本追平并略超 Java 本地线，剩余主要差距在 `NID`。

因此外部集最该帮助我们补的是：

- `page type / region role`
- `reference-like / contents-like / appendix-like` 页分类
- 更稳的复杂版面阅读顺序

而不是继续在普通 dense table 上堆规则。
