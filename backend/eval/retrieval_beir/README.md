# Retrieval BEIR Eval

这一目录负责 **公开 BEIR 数据集上的离线检索评测**。

当前边界：

- 当前支持：
  - `dense-only`
  - `dense + BM25 + RRF`
  - `dense + backend reranker`（runner 已补，结果记录独立维护）
  - `dense + backend query rewrite`（系统级评估入口）
  - `contextual compression probe`（系统级行为观测）
- 不走生产 `/api/v1/knowledge/search`
- 还不接生产 query rewrite / contextual compression
- 直接复用项目当前 `EmbeddingService`

## 目录

- `run_dense.py`
  - 第一版 dense-only BEIR runner
- `run_hybrid.py`
  - dense + BM25 + RRF 的离线 runner
- `run_rerank.py`
  - dense 候选 + backend `RerankerService` 的离线 runner
- `run_rerank_smoke.py`
  - 不依赖 `beir` 包的最小 `rerank` smoke runner
  - 直接复用已有 dense `run.trec`，手动读取 `corpus / queries / qrels`
  - 适合在现有 backend 容器里快速收单因素结论
- `run_rewrite.py`
  - dense 候选 + backend `QueryRewriteService` 的离线 runner
- `run_compression_probe.py`
  - backend `ContextualCompressionService` 的系统级 probe
- `data/`
  - 本地下载的数据集目录（忽略提交）
- `output/`
  - 评测产物目录（忽略提交）
- `.cache/`
  - 本地缓存目录（忽略提交）

## 用法

先准备一个**独立评测环境**安装 `beir`，不要改主 `backend/requirements.txt`。

优先使用**独立 venv**，不要改主 backend 依赖链：

```bash
python -m venv .venv-retrieval-eval
. .venv-retrieval-eval/bin/activate
pip install -r backend/eval/retrieval_beir/requirements.txt
```

运行示例：

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_dense.py \
  --dataset scifact \
  --download
```

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_hybrid.py \
  --dataset scifact \
  --download
```

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_rerank.py \
  --dataset scifact \
  --download
```

如果数据集已经存在：

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_dense.py \
  --dataset scifact
```

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_hybrid.py \
  --dataset scifact
```

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_rerank.py \
  --dataset scifact
```

如果只想在现有 backend 环境里快速验证 `rerank` 单因素，不依赖 `beir` 包：

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_rerank_smoke.py \
  --dataset scifact \
  --dense-runfile backend/eval/retrieval_beir/output/<dense-run>/run.trec
```

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_rewrite.py \
  --dataset scifact
```

```bash
PYTHONPATH=backend python backend/eval/retrieval_beir/run_compression_probe.py \
  --dataset scifact \
  --dense-runfile backend/eval/retrieval_beir/output/<run>/run.trec
```

## 输出

每次运行会在 `output/<dataset>/<timestamp>/` 下写出：

- `metrics.json`
- `run.trec`
- `config.json`

## 当前默认

- 使用项目当前 embedding 模型
- 使用项目当前 embedding 维度
- `score_function = cos_sim`
- `k_values = 1,3,5,10,20,50,100`
