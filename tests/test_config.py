import json

from codex_auth.config import (
    auth_is_configured,
    get_auth_file,
    get_cookie_file,
    load_auth_data,
    load_cookie_text,
)


def test_get_auth_file_uses_environment(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))

    assert get_auth_file() == auth_file


def test_load_auth_data_uses_inline_render_secret(monkeypatch):
    expected = {"tokens": {"refresh_token": "secret"}}
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(expected))

    assert auth_is_configured()
    assert load_auth_data() == expected


def test_load_cookie_text_uses_inline_render_secret(monkeypatch):
    monkeypatch.setenv("CODEX_AUTH_COOKIES", "cookie-data")

    assert auth_is_configured()
    assert load_cookie_text() == "cookie-data"


def test_get_cookie_file_uses_environment(monkeypatch, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    monkeypatch.setenv("CODEX_AUTH_COOKIE_FILE", str(cookie_file))

    assert get_cookie_file() == cookie_file
