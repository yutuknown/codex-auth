FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir --editable . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

CMD ["sh", "-c", "codex-auth start --host 0.0.0.0 --port ${PORT:-10000}"]
