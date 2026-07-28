FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir --editable .

CMD ["sh", "-c", "codex-auth start --host 0.0.0.0 --port ${PORT:-10000}"]
