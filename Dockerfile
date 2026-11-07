# AI Support Copilot — single-container image (stage 4).
# Python 3.12 slim, stdlib-only app code plus FastAPI/uvicorn.
# The SQLite database lives in /data (see docker-compose.yml), so the
# container is stateless apart from its named volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Create the runtime user first so later layers are owned correctly.
RUN adduser --disabled-password --gecos "" copilot \
    && mkdir -p /data \
    && chown copilot:copilot /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY demo-data ./demo-data

USER copilot

# Healthcheck without extra packages: stdlib urllib against /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
