FROM research-assistant-backend:latest

USER root

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    docker.io \
    git \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @devcontainers/cli && \
    pip install --no-cache-dir --default-timeout=120 --retries 10 --index-url ${TORCH_INDEX_URL} \
      torch && \
    pip install --no-cache-dir --default-timeout=120 --retries 10 --index-url ${PIP_INDEX_URL} \
      papermill \
      jupyter-repo2docker \
      scikit-learn \
      h5py \
      schedulefree && \
    python - <<'PY'
import importlib

for name in [
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "h5py",
    "matplotlib",
    "seaborn",
    "torch",
    "schedulefree",
    "papermill",
]:
    importlib.import_module(name)
print("runtime-worker ML environment ready")
PY

WORKDIR /app

EXPOSE 8109

ENTRYPOINT []
CMD ["uvicorn", "app.runtime_worker.main:app", "--host", "0.0.0.0", "--port", "8109"]
