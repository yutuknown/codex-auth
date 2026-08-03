FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir --editable .

CMD ["sh", "-c", "if [ \"${CODEX_AUTH_BETA_MODE:-0}\" = \"1\" ]; then python -m uvicorn beta.m365_compat:app --host 0.0.0.0 --port ${PORT:-10000}; else codex-auth start --host 0.0.0.0 --port ${PORT:-10000}; fi"]
