from starlette.requests import Request

from codex_auth.api import api_key_is_valid, dashboard_session_value


def make_request(headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "headers": raw_headers})


def test_api_key_accepts_bearer_token():
    request = make_request({"Authorization": "Bearer render-secret"})

    assert api_key_is_valid(request, "render-secret")


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
