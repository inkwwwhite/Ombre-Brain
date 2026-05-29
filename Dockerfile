# ============================================================
# Ombre Brain Docker Build
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (leverage Docker cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY *.py .
COPY config.example.yaml ./config.yaml

# Copy dashboard HTML (原作者的网页端 dashboard)
COPY dashboard.html ./dashboard.html

# Copy PWA static files (我加的手机端)
COPY static/ ./static/

# Persistent mount point: bucket data
VOLUME ["/app/buckets"]

# Default to streamable-http for container
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets

EXPOSE 8000

CMD ["python", "server.py"]
