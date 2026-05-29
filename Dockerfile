# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (leverage Docker cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files / 复制项目文件
COPY *.py .
COPY config.example.yaml ./config.yaml

# Copy frontend static files / 复制前端静态文件
COPY static/ ./static/

# Persistent mount point: bucket data
VOLUME ["/app/buckets"]

# Default to streamable-http for container
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets

EXPOSE 8000

CMD ["python", "server.py"]
