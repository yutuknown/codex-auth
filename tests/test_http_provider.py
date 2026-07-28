import pytest

from codex_auth.providers.openai.provider import OpenAIProvider, _message_delta, parse_netscape_cookies


def test_parse_netscape_cookie_and_http_only_prefix():
    text = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "#HttpOnly_.chatgpt.com\tTRUE\t/\tTRUE\t0\tsession\tsecret",
        ]
    )

    assert parse_netscape_cookies(text) == [
        {
            "name": "session",
            "value": "secret",
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "expires_at": None,
        }
    ]


def test_parse_netscape_cookie_rejects_invalid_record():
    with pytest.raises(ValueError, match="line 1"):
        parse_netscape_cookies("not-a-cookie")


def test_message_delta_reconstructs_initial_and_patch_events():
    state = {}
    initial = {
        "v": {
            "conversation_id": "conversation",
            "message": {
                "id": "assistant",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["Hello"]},
            },
        }
    }
    patch = {
        "v": [
            {"o": "append", "p": "/message/content/parts/0", "v": " world"},
        ]
    }

    assert _message_delta(initial, state) == "Hello"
    assert _message_delta(patch, state) == " world"
    assert state["text"] == "Hello world"
    assert state["conversation_id"] == "conversation"


def test_message_delta_handles_top_level_v1_patch():
    state = {"role": "assistant", "content_type": "text", "text": ""}

    assert (
        _message_delta(
            {"p": "/message/content/parts/0", "o": "append", "v": "prefix"},
            state,
        )
        == "prefix"
    )


def test_initialize_uses_hosted_access_token_without_session_exchange(monkeypatch):
    class FakeCookies:
        def set(self, *args, **kwargs):
            pass

        def get(self, name):
            return "device" if name == "oai-did" else None

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.cookies = FakeCookies()

        def get(self, *args, **kwargs):
            raise AssertionError("session exchange should not run")

    monkeypatch.setenv("CODEX_AUTH_COOKIES", ".chatgpt.com\tTRUE\t/\tTRUE\t0\toai-did\tdevice")
    monkeypatch.setenv("CODEX_AUTH_ACCESS_TOKEN", "hosted-token")
    monkeypatch.setattr("codex_auth.providers.openai.provider.Session", FakeSession)

    provider = OpenAIProvider()
    provider._initialize_sync()

    assert provider.access_token == "hosted-token"
    assert provider.device_id == "device"
    assert provider.auth_mode == "hosted_bearer"


def test_expiry_details_reports_remaining_lifetime(monkeypatch):
    monkeypatch.setattr("codex_auth.providers.openai.provider.time.time", lambda: 1_000)

    details = OpenAIProvider._expiry_details(1_120)

    assert details["seconds_remaining"] == 120
    assert details["expired"] is False
    assert details["expires_at"] == "1970-01-01T00:18:40+00:00"


def test_runtime_status_distinguishes_proxy_from_upstream_capabilities():
    provider = OpenAIProvider()
    provider.access_token = ""
    provider.device_id = ""

    status = provider.runtime_status()

    assert status["transport"] == "curl-cffi"
    assert status["browser_process"] is False
    assert status["max_concurrent_generations"] == 1
    assert status["proxy_capabilities"]["streaming"] is True
    assert status["proxy_capabilities"]["file_uploads"] is False
