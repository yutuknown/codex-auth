import json

from codex_auth.config import auth_is_configured, get_auth_file, load_auth_data


def test_get_auth_file_uses_environment(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))

    assert get_auth_file() == auth_file


def test_load_auth_data_uses_inline_render_secret(monkeypatch):
    expected = {"tokens": {"refresh_token": "secret"}}
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(expected))

    assert auth_is_configured()
    assert load_auth_data() == expected
