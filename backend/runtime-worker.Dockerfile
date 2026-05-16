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

ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG APT_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG CLAUDE_CODE_VERSION=latest
ARG CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS=240000
ENV CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS=${CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS}
ENV CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-plugin-seed
ENV CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/claude-plugin-seed
ENV NODE_PATH=/usr/local/lib/node_modules

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i \
        -e "s|http://deb.debian.org/debian|${APT_MIRROR}|g" \
        -e "s|https://deb.debian.org/debian|${APT_MIRROR}|g" \
        -e "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i \
        -e "s|http://deb.debian.org/debian|${APT_MIRROR}|g" \
        -e "s|https://deb.debian.org/debian|${APT_MIRROR}|g" \
        -e "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|https://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        /etc/apt/sources.list; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    pandoc \
    poppler-utils \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN npm config set registry ${NPM_REGISTRY} && \
    npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} docx

RUN claude --version

COPY scripts/runtime_worker_entrypoint.sh /usr/local/bin/runtime_worker_entrypoint.sh
RUN chmod +x /usr/local/bin/runtime_worker_entrypoint.sh && \
    mkdir -p /opt/claude-plugin-seed && \
    chown -R app:app /opt/claude-plugin-seed

WORKDIR /app

EXPOSE 8109

ENTRYPOINT []
CMD ["/usr/local/bin/runtime_worker_entrypoint.sh"]
