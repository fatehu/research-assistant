FROM python:3.11-slim

WORKDIR /app

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    gcc \
    gosu \
    libpq-dev \
    fontconfig \
    fonts-wqy-zenhei \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Torch wheel source is configurable at build time.
# CPU default:
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# CUDA example (NVIDIA):
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_PACKAGE=torch
RUN set -eu; \
    attempt=1; \
    max_attempts=3; \
    while [ "$attempt" -le "$max_attempts" ]; do \
      echo "Installing ${TORCH_PACKAGE} from ${TORCH_INDEX_URL} (attempt ${attempt}/${max_attempts})"; \
      if pip install --no-cache-dir --default-timeout=1000 --retries 10 --progress-bar off --no-deps "${TORCH_PACKAGE}" --index-url "${TORCH_INDEX_URL}"; then \
        exit 0; \
      fi; \
      if [ "$attempt" -eq "$max_attempts" ]; then \
        echo "Torch installation failed after ${max_attempts} attempts"; \
        exit 1; \
      fi; \
      attempt=$((attempt + 1)); \
      echo "Retrying torch installation after transient failure..."; \
      sleep 5; \
    done

ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --default-timeout=120 --retries 10 --index-url ${PIP_INDEX_URL} -r requirements.txt

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app && \
    mkdir -p /app/uploads /app/model_cache && \
    chown -R app:app /app

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
