# RPF-Agent 服务镜像（构建上下文 = 仓库根：docker build -f deploy/Dockerfile -t rpf-agent:latest .）
# 数据不进镜像：运行时 -v <宿主机>/data:/app/data 挂载；.env 由 --env-file 注入。
FROM python:3.11-slim

WORKDIR /app/backend

# 依赖单独一层：代码变更时命中缓存，不必重装
COPY deploy/requirements-frozen.txt .
RUN pip install --no-cache-dir -r requirements-frozen.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# OpenCV / RapidOCR need these shared libraries in the slim runtime image.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y --no-install-recommends libgl1 libglib2.0-0 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend /app/backend
COPY frontend/dist /app/frontend/dist

ENV PYTHONUNBUFFERED=1 \
    BID_EVAL_STATIC_DIR=/app/frontend/dist

EXPOSE 18200
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18200", "--workers", "1"]
