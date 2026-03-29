# Retrieval BEIR Eval

这一目录只负责 **公开 BEIR 数据集上的 dense-only 检索评测**。

当前边界：

- 只评估 dense retrieval
- 不走生产 `/api/v1/knowledge/search`
- 不开启 query rewrite / hybrid FTS / rerank / contextual compression
- 直接复用项目当前 `EmbeddingService`

## 目录

- `run_dense.py`
  - 第一版 dense-only BEIR runner
- `data/`
  - 本地下载的数据集目录（忽略提交）
- `output/`
  - 评测产物目录（忽略提交）
- `.cache/`
  - 本地缓存目录（忽略提交）

## 用法

先准备一个**独立评测环境**安装 `beir`，不要改主 `backend/requirements.txt`。

例如在 backend 容器里临时安装：

```bash
docker compose exec -T backend sh -lc \
  'cd /app && pip install -r backend/eval/retrieval_beir/requirements.txt'
```

或在单独 venv 里安装：

```bash
python -m venv .venv-retrieval-eval
. .venv-retrieval-eval/bin/activate
pip install -r backend/eval/retrieval_beir/requirements.txt
```

运行示例：

```bash
docker compose exec -T backend sh -lc \
  'cd /app && PYTHONPATH=/app python backend/eval/retrieval_beir/run_dense.py \
    --dataset scifact \
    --download'
```

如果数据集已经存在：

```bash
docker compose exec -T backend sh -lc \
  'cd /app && PYTHONPATH=/app python backend/eval/retrieval_beir/run_dense.py \
    --dataset scifact'
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
