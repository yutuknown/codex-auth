from fastapi.testclient import TestClient
from starlette.requests import Request

from codex_auth.api import (
    api_key_is_valid,
    app,
    dashboard_session_value,
    routes_ui,
    sanitized_headers,
)
from codex_auth.providers.openai.provider import provider


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


def test_cookie_update_activates_validated_text_without_returning_secrets(
    monkeypatch,
    tmp_path,
):
    cookie_text = (
        "# Netscape HTTP Cookie File\n"
        ".chatgpt.com\tTRUE\t/\tTRUE\t0\toai-did\tdevice-secret\n"
        ".chatgpt.com\tTRUE\t/\tTRUE\t0\t__Secure-next-auth.session-token\tsession-secret"
    )
    seen = {}

    async def replace_cookies(text):
        seen["text"] = text
        return {
            "profile": {"identity_level": "identified", "id": "user-1", "email": "u@example.com"},
            "account": {"plan_type": "plus"},
            "entitlement": {"subscription_plan": "chatgptplusplan"},
            "can_access_with_session": True,
        }

    saved_path = tmp_path / "cookies.txt"
    monkeypatch.delenv("CODEX_AUTH_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_AUTH_COOKIES", "old-cookie-data")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(provider, "replace_cookies", replace_cookies)
    monkeypatch.setattr(
        provider,
        "cookie_metadata",
        [{"name": "oai-did"}, {"name": "__Secure-next-auth.session-token"}],
    )
    monkeypatch.setattr(
        provider,
        "runtime_status",
        lambda: {
            "auth_mode": "cookie_refresh",
            "session_cookie": {"expires_at": None, "seconds_remaining": None, "expired": None},
        },
    )
    monkeypatch.setattr(routes_ui, "save_cookie_text", lambda text: saved_path)

    response = TestClient(app).post(
        "/api/auth/cookies",
        json={"cookies": cookie_text, "source_name": "cookies.txt"},
    )

    assert response.status_code == 200
    assert seen["text"] == cookie_text
    assert response.json()["status"] == "activated"
    assert response.json()["cookie_count"] == 2
    assert response.json()["persistence"]["restart_safe"] is True
    assert "device-secret" not in response.text
    assert "session-secret" not in response.text


def test_cookie_update_rejects_invalid_netscape_text(monkeypatch):
    async def reject_cookies(text):
        raise ValueError("Invalid Netscape cookie record on line 1")

    monkeypatch.delenv("CODEX_AUTH_API_KEY", raising=False)
    monkeypatch.setattr(provider, "replace_cookies", reject_cookies)

    response = TestClient(app).post(
        "/api/auth/cookies",
        json={"cookies": "not-a-cookie-file"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["type"] == "invalid_cookie_format"


def test_cookie_update_requires_dashboard_auth_when_api_key_is_configured(monkeypatch):
    monkeypatch.setenv("CODEX_AUTH_API_KEY", "dashboard-secret")

    response = TestClient(app).post(
        "/api/auth/cookies",
        json={"cookies": "not-a-cookie-file"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
