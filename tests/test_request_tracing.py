import logging

from fastapi.testclient import TestClient

from codex_auth.api import (
    StreamHandler,
    app,
    log_stream,
    log_stream_lock,
    pending_request_traces,
)
from codex_auth.providers.openai.provider import ChatGPTSessionError, provider


def _isolated_logger():
    logger = logging.Logger("request-trace-test")
    logger.propagate = False
    logger.addHandler(StreamHandler())
    return logger


def _reset_trace_state():
    with log_stream_lock:
        log_stream.clear()
        pending_request_traces.clear()


def test_trace_emitted_before_http_log_is_merged_into_request_row():
    _reset_trace_state()
    logger = _isolated_logger()

    logger.info(
        "completion captured",
        extra={"request_id": "request-a", "trace_data": {"response": "complete answer"}},
    )
    logger.info(
        "POST /v1/chat/completions 200",
        extra={
            "request_id": "request-a",
            "is_http": True,
            "method": "POST",
            "path": "/v1/chat/completions",
            "status": 200,
        },
    )

    assert len(log_stream) == 1
    assert log_stream[0]["is_http"] is True
    assert log_stream[0]["trace_data"]["response"] == "complete answer"
    assert log_stream[0]["trace_data"]["request_id"] == "request-a"


def test_stream_trace_emitted_after_http_log_updates_existing_request_row():
    _reset_trace_state()
    logger = _isolated_logger()

    logger.info(
        "POST /v1/chat/completions 200",
        extra={
            "request_id": "request-b",
            "is_http": True,
            "method": "POST",
            "path": "/v1/chat/completions",
            "status": 200,
        },
    )
    logger.info(
        "stream captured",
        extra={"request_id": "request-b", "trace_data": {"response": "stream answer"}},
    )

    assert len(log_stream) == 1
    assert log_stream[0]["trace_data"]["response"] == "stream answer"
    assert log_stream[0]["trace_message"] == "stream captured"


def test_completion_response_is_available_on_exact_http_log_row(monkeypatch):
    _reset_trace_state()

    async def fake_generate_stream(*args, **kwargs):
        yield "inspector response proof"

    monkeypatch.setenv("CODEX_AUTH_API_KEY", "test-key")
    monkeypatch.setattr(provider, "generate_stream", fake_generate_stream)
    monkeypatch.setattr(provider, "is_configured", lambda: True)
    handler = StreamHandler()
    api_logger = logging.getLogger("codex_auth")
    previous_level = api_logger.level
    api_logger.setLevel(logging.INFO)
    api_logger.addHandler(handler)
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Show inspector proof"}],
            },
        )
        logs = client.get(
            "/api/logs",
            headers={"Authorization": "Bearer test-key"},
        ).json()["logs"]
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    request_id = response.headers["x-request-id"]
    request_log = next(
        entry for entry in reversed(logs) if entry.get("request_id") == request_id and entry.get("is_http")
    )
    assert response.status_code == 200
    assert request_log["path"] == "/v1/chat/completions"
    assert request_log["trace_data"]["response"] == "inspector response proof"
    assert request_log["trace_data"]["total_tokens"] > 0
    assert request_log["trace_data"]["request_headers"]["authorization"] == "[REDACTED]"
    assert request_log["trace_data"]["response_headers"]["x-request-id"] == request_id


def test_upstream_error_payload_is_captured_on_failed_http_row(monkeypatch):
    _reset_trace_state()

    async def failing_generate_stream(*args, **kwargs):
        if False:
            yield ""
        raise ChatGPTSessionError("upstream rejected request")

    monkeypatch.setenv("CODEX_AUTH_API_KEY", "test-key")
    monkeypatch.setattr(provider, "generate_stream", failing_generate_stream)
    monkeypatch.setattr(provider, "is_configured", lambda: True)
    handler = StreamHandler()
    api_logger = logging.getLogger("codex_auth")
    previous_level = api_logger.level
    api_logger.setLevel(logging.INFO)
    api_logger.addHandler(handler)
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"messages": [{"role": "user", "content": "Fail safely"}]},
        )
        logs = client.get(
            "/api/logs",
            headers={"Authorization": "Bearer test-key"},
        ).json()["logs"]
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(previous_level)

    request_log = next(
        entry
        for entry in reversed(logs)
        if entry.get("request_id") == response.headers["x-request-id"] and entry.get("is_http")
    )
    assert response.status_code == 502
    assert request_log["trace_data"]["status"] == 502
    assert "upstream rejected request" in request_log["trace_data"]["response"]
    assert request_log["trace_data"]["response_data"]["finish_reason"] == "error"
