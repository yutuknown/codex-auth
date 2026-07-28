from fastapi.testclient import TestClient
from starlette.requests import Request

from codex_auth.api import api_key_is_valid, app, dashboard_session_value, sanitized_headers


def make_request(headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "headers": raw_headers})


def test_api_key_accepts_bearer_token():
    request = make_request({"Authorization": "Bearer render-secret"})

    assert api_key_is_valid(request, "render-secret")


def test_trace_headers_redact_credentials_and_bound_values():
    headers = sanitized_headers(
        {
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-API-Key": "secret",
            "Content-Type": "application/json",
            "X-Long": "x" * 600,
        }
    )

    assert headers["authorization"] == "[REDACTED]"
    assert headers["cookie"] == "[REDACTED]"
    assert headers["x-api-key"] == "[REDACTED]"
    assert headers["content-type"] == "application/json"
    assert len(headers["x-long"]) == 512


def test_api_key_accepts_x_api_key():
    request = make_request({"X-API-Key": "render-secret"})

    assert api_key_is_valid(request, "render-secret")


def test_api_key_rejects_missing_or_wrong_token():
    assert not api_key_is_valid(make_request(), "render-secret")
    assert not api_key_is_valid(
        make_request({"Authorization": "Bearer wrong-secret"}),
        "render-secret",
    )


def test_api_key_accepts_dashboard_session_cookie():
    session = dashboard_session_value("render-secret")
    request = make_request({"Cookie": f"codex_auth_dashboard={session}"})

    assert api_key_is_valid(request, "render-secret")


def test_dashboard_session_does_not_contain_raw_api_key():
    assert "render-secret" not in dashboard_session_value("render-secret")


def test_root_head_redirects_instead_of_returning_method_not_allowed():
    response = TestClient(app).head("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert len(response.headers["x-request-id"]) == 16


def test_oversized_request_is_rejected_before_body_parsing(monkeypatch):
    monkeypatch.delenv("CODEX_AUTH_API_KEY", raising=False)

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Content-Length": str(31 * 1024 * 1024)},
        content=b"{}",
    )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "request_too_large"
