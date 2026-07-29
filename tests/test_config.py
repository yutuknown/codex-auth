import json

from codex_auth.config import (
    auth_is_configured,
    get_auth_file,
    get_cookie_file,
    get_m365_auth_file,
    get_m365_graph_file,
    get_m365_graph_oauth_file,
    get_provider_cookie_file,
    load_auth_data,
    load_cookie_text,
    load_m365_auth_data,
    load_m365_graph_data,
    load_m365_graph_oauth_data,
    load_provider_cookie_text,
    provider_cookies_are_configured,
    save_cookie_text,
    save_m365_graph_data,
    save_m365_graph_oauth_data,
    save_provider_cookie_text,
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


def test_save_cookie_text_atomically_updates_configured_file(monkeypatch, tmp_path):
    cookie_file = tmp_path / "private" / "cookies.txt"
    monkeypatch.setenv("CODEX_AUTH_COOKIE_FILE", str(cookie_file))

    result = save_cookie_text("# Netscape HTTP Cookie File\n.example\tTRUE\t/\tTRUE\t0\tname\tvalue")

    assert result == cookie_file
    assert cookie_file.read_text(encoding="utf-8").endswith("\n")
    assert not list(cookie_file.parent.glob("*.tmp"))


def test_m365_provider_credentials_use_configured_files(monkeypatch, tmp_path):
    cookie_file = tmp_path / "m365-cookies.txt"
    auth_file = tmp_path / "m365-auth.json"
    monkeypatch.setenv("CODEX_AUTH_M365_COOKIE_FILE", str(cookie_file))
    monkeypatch.setenv("CODEX_AUTH_M365_AUTH_FILE", str(auth_file))

    save_provider_cookie_text("m365-copilot", "cookie-data")
    auth_file.write_text(json.dumps({"identity": "route"}), encoding="utf-8")

    assert get_provider_cookie_file("m365-copilot") == cookie_file
    assert get_m365_auth_file() == auth_file
    assert provider_cookies_are_configured("m365-copilot")
    assert load_provider_cookie_text("m365-copilot") == "cookie-data\n"
    assert load_m365_auth_data() == {"identity": "route"}


def test_m365_graph_credentials_use_configured_file(monkeypatch, tmp_path):
    graph_file = tmp_path / "m365-graph.json"
    monkeypatch.setenv("CODEX_AUTH_M365_GRAPH_FILE", str(graph_file))

    result = save_m365_graph_data(
        {"access_token": "graph-token", "expires_at": 12345}
    )

    assert result == graph_file
    assert get_m365_graph_file() == graph_file
    assert load_m365_graph_data() == {
        "access_token": "graph-token",
        "expires_at": 12345,
    }


def test_m365_graph_refresh_credentials_use_render_secret_or_configured_file(monkeypatch, tmp_path):
    oauth_file = tmp_path / "m365-graph-oauth.json"
    monkeypatch.setenv("CODEX_AUTH_M365_GRAPH_OAUTH_FILE", str(oauth_file))

    saved = save_m365_graph_oauth_data({"form": {"refresh_token": "secret"}})

    assert saved == oauth_file
    assert get_m365_graph_oauth_file() == oauth_file
    assert load_m365_graph_oauth_data() == {"form": {"refresh_token": "secret"}}
    monkeypatch.setenv("CODEX_AUTH_M365_GRAPH_OAUTH_JSON", json.dumps({"access_token": "inline"}))
    assert load_m365_graph_oauth_data() == {"access_token": "inline"}
