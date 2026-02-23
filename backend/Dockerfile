FROM python:3.11-slim

WORKDIR /app

# Runtime locale / encoding defaults for predictable UTF-8 behavior.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONUNBUFFERED=1

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    fontconfig \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# Torch wheel source is configurable at build time.
# CPU default:
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# CUDA example (NVIDIA):
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --default-timeout=1000 --retries 10 --no-deps torch --index-url ${TORCH_INDEX_URL}

# 安装 Python 依赖
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --default-timeout=120 --retries 10 --index-url ${PIP_INDEX_URL} -r requirements.txt

# 复制应用代码
COPY . .

# 创建必要目录
RUN mkdir -p /app/uploads /app/model_cache

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
