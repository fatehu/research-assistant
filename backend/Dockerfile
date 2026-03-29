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

ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --default-timeout=120 --retries 10 --index-url ${PIP_INDEX_URL} -r requirements.txt && \
    python - <<'PY'
import docling
print(docling.__file__)
PY

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
