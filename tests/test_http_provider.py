import asyncio

import pytest

from codex_auth.providers.openai.provider import (
    ChatGPTSessionError,
    OpenAIProvider,
    _message_delta,
    parse_netscape_cookies,
)


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


def test_initialize_can_use_cookie_only_when_token_exchange_is_blocked(monkeypatch):
    class FakeCookies:
        def set(self, *args, **kwargs):
            pass

        def get(self, name):
            return "device" if name == "oai-did" else None

    class FakeResponse:
        status_code = 403

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.cookies = FakeCookies()

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setenv("CODEX_AUTH_COOKIES", ".chatgpt.com\tTRUE\t/\tTRUE\t0\toai-did\tdevice")
    monkeypatch.delenv("CODEX_AUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("codex_auth.providers.openai.provider.Session", FakeSession)

    provider = OpenAIProvider()
    provider._initialize_sync()

    assert provider.access_token == ""
    assert provider.device_id == "device"
    assert provider.auth_mode == "cookie_only"
    assert provider.runtime_status()["initialized"] is True


def test_cookie_replacement_validation_does_not_trust_hosted_bearer(monkeypatch):
    class FakeCookies:
        def set(self, *args, **kwargs):
            pass

        def get(self, name):
            return "replacement-device" if name == "oai-did" else None

    class FakeResponse:
        status_code = 403

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.cookies = FakeCookies()
            self.exchange_attempted = False

        def get(self, *args, **kwargs):
            self.exchange_attempted = True
            return FakeResponse()

    monkeypatch.setenv("CODEX_AUTH_ACCESS_TOKEN", "unrelated-hosted-token")
    monkeypatch.setattr("codex_auth.providers.openai.provider.Session", FakeSession)
    provider = OpenAIProvider()

    provider._initialize_from_cookie_text_sync(
        ".chatgpt.com\tTRUE\t/\tTRUE\t0\toai-did\treplacement-device",
        include_hosted_access_token=False,
    )

    assert provider.session.exchange_attempted is True
    assert provider.access_token == ""
    assert provider.device_id == "replacement-device"
    assert provider.auth_mode == "cookie_only"


def test_replace_cookies_swaps_validated_session_and_clears_cached_context(monkeypatch):
    class FakeSession:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Candidate:
        def __init__(self):
            self.session = FakeSession()
            self.access_token = "fresh-token"
            self.device_id = "fresh-device"
            self.auth_mode = "cookie_refresh"
            self.cookie_metadata = [{"name": "oai-did"}]
            self.initialized_at = 123.0
            self.include_hosted_access_token = None

        def _initialize_from_cookie_text_sync(
            self,
            text,
            *,
            include_hosted_access_token=True,
        ):
            assert text == "fresh-cookie-text"
            self.include_hosted_access_token = include_hosted_access_token

        def _fetch_account_details_sync(self):
            return {"profile": {"id": "fresh-user"}}

        async def close(self):
            if self.session:
                self.session.close()

    current = OpenAIProvider()
    old_session = FakeSession()
    current.session = old_session
    current.conversation_id = "stale-conversation"
    current.parent_message_id = "stale-parent"
    current._account_cache = {"stale": True}
    current._models_cache = [{"stale": True}]
    candidate = Candidate()
    new_session = candidate.session
    monkeypatch.setattr(
        "codex_auth.providers.openai.provider.OpenAIProvider",
        lambda: candidate,
    )

    details = asyncio.run(current.replace_cookies("fresh-cookie-text"))

    assert details["profile"]["id"] == "fresh-user"
    assert candidate.include_hosted_access_token is False
    assert current.session is new_session
    assert candidate.session is None
    assert old_session.closed is True
    assert current.access_token == "fresh-token"
    assert current.device_id == "fresh-device"
    assert current.conversation_id is None
    assert current.parent_message_id is None
    assert current._account_cache is None
    assert current._models_cache is None


def test_expiry_details_reports_remaining_lifetime(monkeypatch):
    monkeypatch.setattr("codex_auth.providers.openai.provider.time.time", lambda: 1_000)

    details = OpenAIProvider._expiry_details(1_120)

    assert details["seconds_remaining"] == 120
    assert details["expired"] is False
    assert details["expires_at"] == "1970-01-01T00:18:40+00:00"


def test_runtime_status_only_advertises_uploads_with_bearer_authentication():
    provider = OpenAIProvider()
    provider.access_token = "bearer-token"
    provider.device_id = ""

    status = provider.runtime_status()

    assert status["transport"] == "curl-cffi"
    assert status["browser_process"] is False
    assert status["max_concurrent_generations"] == 1
    assert status["proxy_capabilities"]["streaming"] is True
    assert status["proxy_capabilities"]["image_input"] is True
    assert status["proxy_capabilities"]["file_uploads"] is True
    assert status["proxy_capabilities"]["web_search"] is True

    provider.auth_mode = "cookie_only"
    cookie_status = provider.runtime_status()
    assert cookie_status["proxy_capabilities"]["image_input"] is False
    assert cookie_status["proxy_capabilities"]["file_uploads"] is False
    assert cookie_status["proxy_capabilities"]["web_search"] is True


def test_decode_data_url_and_build_multimodal_message():
    provider = OpenAIProvider()
    png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZFlAAAAAASUVORK5CYII="
    )

    data, mime_type, name = provider._decode_input_file({"url": png, "name": "pixel.png"}, 1)
    upload = {
        "id": "file_test",
        "mime_type": mime_type,
        "name": name,
        "size": len(data),
        "width": 1,
        "height": 1,
        "is_image": True,
    }
    message = provider._user_message("Describe it", "message-id", [upload])

    assert mime_type == "image/png"
    assert name == "pixel.png"
    assert message["content"]["content_type"] == "multimodal_text"
    assert message["content"]["parts"][0]["asset_pointer"] == "file-service://file_test"
    assert message["metadata"]["attachments"][0]["mimeType"] == "image/png"


def test_build_document_message_uses_attachment_metadata_without_image_pointer():
    message = OpenAIProvider._user_message(
        "Summarize it",
        "message-id",
        [
            {
                "id": "file_document",
                "mime_type": "application/pdf",
                "name": "report.pdf",
                "size": 42,
                "width": 0,
                "height": 0,
                "is_image": False,
            }
        ],
    )

    assert message["content"] == {"content_type": "text", "parts": ["Summarize it"]}
    assert message["metadata"]["attachments"][0]["id"] == "file_document"


def test_remote_file_url_rejects_private_network_targets():
    with pytest.raises(ChatGPTSessionError, match="private or local"):
        OpenAIProvider._validate_remote_file_url("http://127.0.0.1/private.txt")


def test_authenticated_request_refreshes_cookie_token_once_after_401():
    class FakeResponse:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body or {}
            self.closed = False

        def json(self):
            return self._body

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.authorization_headers = []
            self.first_response = FakeResponse(401)

        def request(self, method, url, headers, **kwargs):
            self.authorization_headers.append(headers["Authorization"])
            if len(self.authorization_headers) == 1:
                return self.first_response
            return FakeResponse(200)

        def get(self, url, timeout):
            return FakeResponse(200, {"accessToken": "refreshed-token"})

    provider = OpenAIProvider()
    provider.session = FakeSession()
    provider.access_token = "expired-token"
    provider.device_id = "device"

    response = provider._authenticated_request("GET", "/backend-api/me")

    assert response.status_code == 200
    assert provider.access_token == "refreshed-token"
    assert provider.auth_mode == "cookie_refresh"
    assert provider.session.first_response.closed
    assert provider.session.authorization_headers == [
        "Bearer expired-token",
        "Bearer refreshed-token",
    ]


@pytest.mark.parametrize("auth_failure_status", [401, 403])
def test_authenticated_request_falls_back_to_cookie_only_when_refresh_is_stale(
    auth_failure_status,
):
    class FakeResponse:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body or {}

        def json(self):
            return self._body

        def close(self):
            pass

    class FakeSession:
        def __init__(self):
            self.authorization_headers = []

        def request(self, method, url, headers, **kwargs):
            self.authorization_headers.append(headers.get("Authorization"))
            return FakeResponse(
                auth_failure_status if len(self.authorization_headers) == 1 else 200
            )

        def get(self, url, timeout):
            return FakeResponse(200, {"accessToken": "stale-token"})

    provider = OpenAIProvider()
    provider.session = FakeSession()
    provider.access_token = "stale-token"
    provider.device_id = "device"

    response = provider._authenticated_request("GET", "/backend-api/models")

    assert response.status_code == 200
    assert provider.auth_mode == "cookie_only"
    assert provider.session.authorization_headers == ["Bearer stale-token", None]


def test_account_discovery_keeps_profile_when_optional_settings_are_unauthorized(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body or {}

        def json(self):
            return self._body

    responses = {
        "/backend-api/accounts/check/v4-2023-04-27": FakeResponse(
            200,
            {
                "accounts": {
                    "default": {
                        "account": {
                            "account_user_role": "account-owner",
                            "plan_type": "free",
                        },
                        "features": ["feature-a"],
                        "can_access_with_session": True,
                    }
                }
            },
        ),
        "/backend-api/me": FakeResponse(
            200,
            {
                "id": "user-123",
                "object": "user",
                "email": "user@example.com",
            },
        ),
        "/backend-api/settings/user": FakeResponse(401),
    }
    provider = OpenAIProvider()
    provider.session = object()
    monkeypatch.setattr(
        provider,
        "_authenticated_request",
        lambda method, target, **kwargs: responses[target],
    )

    details = provider._fetch_account_details_sync()

    assert details["profile"]["email"] == "user@example.com"
    assert details["profile"]["id"] == "user-123"
    assert details["profile"]["mfa_enabled"] is None
    assert details["account"]["plan_type"] == "free"
    assert details["account"]["role"] == "account-owner"
    assert details["feature_count"] == 1
    assert details["endpoint_status"]["settings"] == 401
    assert details["endpoint_health"]["settings"] == {
        "status": 401,
        "state": "restricted",
        "required": False,
    }
    assert details["data_quality"] == {
        "state": "partial",
        "identity": "identified",
        "privacy_settings": "unavailable",
    }
    assert details["warnings"] == [
        "Optional privacy settings are unavailable in cookie-only mode"
    ]
