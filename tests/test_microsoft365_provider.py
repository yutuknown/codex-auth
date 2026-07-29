import asyncio
import json

import pytest

from codex_auth.providers.errors import ProviderNotConfiguredError
from codex_auth.providers.microsoft365 import (
    MODEL_TONES,
    RECORD_SEPARATOR,
    Microsoft365CopilotProvider,
    parse_model_catalog,
)


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False
        update = {
            "type": 1,
            "target": "update",
            "arguments": [{"messages": [{"author": "bot", "text": "M365_OK"}]}],
        }
        self.received = [
            (b"{}\x1e", 1),
            ((json.dumps(update) + RECORD_SEPARATOR).encode(), 1),
            (b'{"type":3,"invocationId":"0"}\x1e', 1),
        ]

    def send(self, payload, flags):
        self.sent.append((payload, flags))

    def recv(self):
        return self.received.pop(0)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.endpoint = None
        self.headers = None

    def ws_connect(self, endpoint, headers, timeout):
        self.endpoint = endpoint
        self.headers = headers
        return self.websocket


class FakeTokenResponse:
    status_code = 200

    def __init__(self):
        self.closed = False

    def json(self):
        return {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 28800,
            "refresh_token_expires_in": 7776000,
        }

    def close(self):
        self.closed = True


class FakeRefreshSession:
    def __init__(self):
        self.request = None
        self.response = FakeTokenResponse()
        self.closed = False

    def post(self, endpoint, params, data, headers, timeout):
        self.request = {
            "endpoint": endpoint,
            "params": params,
            "data": data,
            "headers": headers,
            "timeout": timeout,
        }
        return self.response

    def close(self):
        self.closed = True


class FakeGraphResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeGraphSession:
    def __init__(self, response):
        self.response = response
        self.headers = None
        self.closed = False

    def get(self, endpoint, headers, timeout):
        self.headers = headers
        return self.response

    def close(self):
        self.closed = True


def test_microsoft_generation_uses_signalr_and_extracts_final_bot_text():
    websocket = FakeWebSocket()
    session = FakeSession(websocket)
    provider = Microsoft365CopilotProvider()
    provider.session = session
    provider.access_token = "secret-token"
    provider.identity = "user@tenant"

    response = provider._generate_sync("Reply exactly: M365_OK")

    assert response == "M365_OK"
    assert websocket.closed is True
    assert "access_token=secret-token" in session.endpoint
    handshake = json.loads(websocket.sent[0][0].rstrip(RECORD_SEPARATOR))
    invocation = json.loads(websocket.sent[2][0].rstrip(RECORD_SEPARATOR))
    assert handshake == {"protocol": "json", "version": 1}
    assert invocation["target"] == "chat"
    assert invocation["arguments"][0]["message"]["text"] == "Reply exactly: M365_OK"
    assert invocation["arguments"][0]["tone"] == "Magic"


@pytest.mark.parametrize(
    ("model", "tone"),
    [
        ("auto", "Magic"),
        ("quick-response", "Chat"),
        ("think-deeper", "Reasoning"),
        ("gpt-5.5-quick-response", "Gpt_5_5_Chat"),
        ("gpt-5.5-think-deeper", "Gpt_5_5_Reasoning"),
    ],
)
def test_microsoft_model_mapping_uses_captured_signalr_tones(model, tone):
    payload = Microsoft365CopilotProvider._request_payload(
        "hello",
        "session",
        "request",
        "trace",
        model,
    )

    assert MODEL_TONES[model] == tone
    assert payload["tone"] == tone


def test_microsoft_model_list_exposes_every_proven_ui_mode():
    provider = Microsoft365CopilotProvider()
    provider._models_cache = provider._fallback_models()
    provider._models_cache_time = 1_000_000_000_000

    models = asyncio.run(provider.fetch_models())

    assert {model["slug"] for model in models} == set(MODEL_TONES)


def test_microsoft_model_catalog_is_discovered_from_authenticated_shell():
    metadata = {
        "defaultModelSelectionId": "Magic",
        "availableModelSelectionOptions": [
            {
                "id": "Magic",
                "type": "item",
                "menuItemTitle": "Auto",
                "menuItemDescription": "Decides how long to think",
                "sectionNumber": 1,
            },
            {
                "itemGroup": [
                    {
                        "id": "Gpt_5_6_Chat",
                        "type": "item",
                        "menuItemTitle": "GPT 5.6 Quick response",
                        "sectionNumber": 2,
                    },
                    {
                        "id": "Gpt_5_6_Reasoning",
                        "type": "item",
                        "menuItemTitle": "GPT 5.6 Think deeper",
                        "sectionNumber": 2,
                    },
                ],
                "id": "OpenAI",
                "type": "itemGroup",
                "menuItemTitle": "GPT",
            },
        ],
    }
    escaped = json.dumps(metadata, separators=(",", ":")).replace('"', '\\"')
    shell = f'<script>controller.enqueue("modelSelectorMetadata\\":{escaped}")</script>'

    models, default_tone = parse_model_catalog(shell)

    assert default_tone == "Magic"
    assert [(model["slug"], model["tone"]) for model in models] == [
        ("auto", "Magic"),
        ("gpt-5.6-quick-response", "Gpt_5_6_Chat"),
        ("gpt-5.6-think-deeper", "Gpt_5_6_Reasoning"),
    ]


def test_microsoft_dynamic_catalog_controls_generation_tone():
    websocket = FakeWebSocket()
    provider = Microsoft365CopilotProvider()
    provider.session = FakeSession(websocket)
    provider.access_token = "secret-token"
    provider.identity = "user@tenant"
    provider._model_tones = {"gpt-5.6-quick-response": "Gpt_5_6_Chat"}

    provider._generate_sync("hello", "gpt-5.6-quick-response")

    invocation = json.loads(websocket.sent[2][0].rstrip(RECORD_SEPARATOR))
    assert invocation["arguments"][0]["tone"] == "Gpt_5_6_Chat"


def test_microsoft_generation_fails_explicitly_when_cookie_session_has_no_bearer():
    provider = Microsoft365CopilotProvider()
    provider.session = object()
    provider.web_session_valid = True
    provider.initialized = True

    with pytest.raises(ProviderNotConfiguredError, match="short-lived bearer token"):
        provider._generate_sync("hello")


def test_microsoft_runtime_capabilities_reflect_current_credentials(monkeypatch):
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.provider_cookies_are_configured",
        lambda provider_id: True,
    )
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.load_m365_oauth_data",
        lambda: {},
    )
    provider = Microsoft365CopilotProvider()

    cookie_only = provider.runtime_status()
    provider.access_token = "token"
    provider.identity = "identity"
    ready = provider.runtime_status()

    assert cookie_only["proxy_capabilities"]["text"] is False
    assert ready["proxy_capabilities"]["text"] is True
    assert ready["generation_ready"] is True


def test_microsoft_oauth_refresh_rotates_and_persists_tokens(monkeypatch):
    oauth = {
        "token_endpoint": ("https://login.microsoftonline.com/tenant/oauth2/v2.0/token"),
        "query": {"client_id": "public-client"},
        "form": {
            "client_id": "public-client",
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
            "scope": "https://substrate.office.com/sydney/v2/.default",
        },
    }
    saved = {}
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.load_m365_oauth_data",
        lambda: oauth,
    )
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.load_m365_auth_data",
        lambda: {"identity": "route"},
    )
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.save_m365_oauth_data",
        lambda value: saved.setdefault("oauth", value),
    )
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.save_m365_auth_data",
        lambda value: saved.setdefault("auth", value),
    )
    session = FakeRefreshSession()
    provider = Microsoft365CopilotProvider()
    provider.session = session

    provider._refresh_access_token_sync()

    assert provider.access_token == "rotated-access"
    assert provider.access_token_expires_at > 0
    assert session.request["data"]["refresh_token"] == "old-refresh"
    assert session.request["params"]["client-request-id"]
    assert saved["oauth"]["form"]["refresh_token"] == "rotated-refresh"
    assert saved["auth"]["access_token"] == "rotated-access"
    assert session.response.closed is True


def test_microsoft_graph_profile_is_optional_and_normalized(monkeypatch):
    response = FakeGraphResponse(
        200,
        {
            "id": "graph-user",
            "displayName": "Graph User",
            "userPrincipalName": "graph@example.com",
            "officeLocation": "Singapore",
        },
    )
    session = FakeGraphSession(response)
    monkeypatch.setattr("codex_auth.providers.microsoft365.Session", lambda **_: session)
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.load_m365_graph_data",
        lambda: {"access_token": "graph-token", "expires_at": 9_999_999_999},
    )
    monkeypatch.setattr("codex_auth.providers.microsoft365.load_m365_graph_oauth_data", lambda: {})
    provider = Microsoft365CopilotProvider()

    graph, connection, diagnostics = provider._fetch_graph_profile_sync()

    assert graph["profile"]["name"] == "Graph User"
    assert graph["account"]["office_location"] == "Singapore"
    assert connection["state"] == "available"
    assert diagnostics[0]["status"] == 200
    assert session.headers["Authorization"] == "Bearer graph-token"


def test_microsoft_graph_expiry_does_not_break_copilot_generation(monkeypatch):
    response = FakeGraphResponse(401)
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.Session",
        lambda **_: FakeGraphSession(response),
    )
    monkeypatch.setattr(
        "codex_auth.providers.microsoft365.load_m365_graph_data",
        lambda: {"access_token": "expired-graph-token", "expires_at": 9_999_999_999},
    )
    monkeypatch.setattr("codex_auth.providers.microsoft365.load_m365_graph_oauth_data", lambda: {})
    provider = Microsoft365CopilotProvider()

    graph, connection, diagnostics = provider._fetch_graph_profile_sync()

    assert graph == {}
    assert connection["state"] == "expired"
    assert diagnostics[0]["required"] is False


def test_microsoft_graph_refresh_rotates_optional_profile_token(monkeypatch):
    oauth = {
        "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        "form": {"grant_type": "refresh_token", "refresh_token": "old-graph-refresh"},
    }
    saved = {}
    session = FakeRefreshSession()
    monkeypatch.setattr("codex_auth.providers.microsoft365.Session", lambda **_: session)
    monkeypatch.setattr("codex_auth.providers.microsoft365.load_m365_graph_oauth_data", lambda: oauth)
    monkeypatch.setattr("codex_auth.providers.microsoft365.load_m365_graph_data", lambda: {})
    monkeypatch.setattr("codex_auth.providers.microsoft365.save_m365_graph_data", lambda value: saved.setdefault("graph", value))
    monkeypatch.setattr("codex_auth.providers.microsoft365.save_m365_graph_oauth_data", lambda value: saved.setdefault("oauth", value))
    provider = Microsoft365CopilotProvider()

    graph_data = provider._refresh_graph_access_token_sync()

    assert graph_data["access_token"] == "rotated-access"
    assert graph_data["expires_at"] > 0
    assert saved["oauth"]["form"]["refresh_token"] == "rotated-refresh"
    assert session.closed is True
