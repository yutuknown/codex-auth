import json
import os
import time

import pytest

from codex_auth.beta import m365_bearer
from codex_auth.beta.m365_bearer import (
    BetaConfigurationError,
    BetaCredential,
    BetaRoute,
    BetaUpstreamError,
    M365BearerBeta,
)
from codex_auth.providers.microsoft365 import RECORD_SEPARATOR


def credential_raw(**overrides):
    return {
        "scope": "https://substrate.office.com/sydney/v2/.default",
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_in": 3600,
        **overrides,
    }


def route_raw(**overrides):
    return {
        "identity": "opaque-identity",
        "oauth": {
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "query": {"client-request-id": "request"},
            "form": {"client_id": "client", "scope": "https://substrate.office.com/sydney/v2/.default"},
        },
        **overrides,
    }


class FakeWebSocket:
    def __init__(self):
        update = {"type": 1, "target": "update", "arguments": [{"messages": [{"author": "bot", "text": "M365_BETA_OK"}]}]}
        self.received = [
            (b"{}\x1e", 1),
            ((json.dumps(update) + RECORD_SEPARATOR).encode(), 1),
            (b'{"type":3}\x1e', 1),
        ]
        self.sent = []
        self.closed = False

    def send(self, payload, flags):
        self.sent.append((payload, flags))

    def recv(self):
        return self.received.pop(0)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.cookies = []
        self.websocket = FakeWebSocket()
        self.endpoint = None
        self.headers = None
        self.closed = False

    def ws_connect(self, endpoint, headers, timeout):
        self.endpoint, self.headers = endpoint, headers
        return self.websocket

    def close(self):
        self.closed = True


class FakeRefreshResponse:
    status_code = 200

    def json(self):
        return {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 14400,
        }

    def close(self):
        pass


class FakeRefreshSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.request = None

    def post(self, endpoint, params, data, headers, timeout):
        self.request = {"endpoint": endpoint, "params": params, "data": data, "headers": headers, "timeout": timeout}
        return FakeRefreshResponse()


def test_raw_credential_derives_expiry_and_hides_secrets():
    credential = BetaCredential.from_raw(credential_raw(), captured_at=1_000)

    assert credential.expires_at == 4_600
    assert "access-secret" not in repr(credential)
    assert "refresh-secret" not in repr(credential)


@pytest.mark.parametrize("raw", [{}, {"identity": "identity"}, {"identity": "identity", "oauth": {}}])
def test_route_rejects_missing_capture_metadata(raw):
    with pytest.raises(BetaConfigurationError):
        BetaRoute.from_raw(raw)


def test_cookie_free_generation_uses_signalr_without_cookie_header(tmp_path):
    session = FakeSession()
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    route = BetaRoute.from_raw(route_raw())
    beta = M365BearerBeta(credential, route, tmp_path / "ms365-auth.json", session_factory=lambda: session)

    answer = beta.generate("Reply exactly with: M365_BETA_OK")

    assert answer == "M365_BETA_OK"
    assert session.cookies == []
    assert "Cookie" not in session.headers
    assert "access-secret" in session.endpoint
    assert "access-secret" not in str(beta.status())
    assert session.websocket.closed is True


def test_status_reports_expiring_soon_without_secret(tmp_path):
    credential = BetaCredential.from_raw(credential_raw(expires_in=1), captured_at=time.time())
    beta = M365BearerBeta(credential, BetaRoute.from_raw(route_raw()), tmp_path / "ms365-auth.json", session_factory=FakeSession)

    status = beta.status()

    assert status["state"] == "expiring_soon"
    assert "access-secret" not in str(status)
    assert "refresh-secret" not in str(status)


def test_refresh_rotates_the_local_pair_atomically(tmp_path):
    session = FakeRefreshSession()
    credential_path = tmp_path / "ms365-auth.json"
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(credential, BetaRoute.from_raw(route_raw()), credential_path, session_factory=lambda: session)

    status = beta.refresh()

    saved = json.loads(credential_path.read_text(encoding="utf-8"))
    assert session.cookies == []
    assert session.request["data"]["refresh_token"] == "refresh-secret"
    assert saved["access_token"] == "rotated-access"
    assert saved["refresh_token"] == "rotated-refresh"
    assert status["last_refresh_outcome"] == "succeeded"
    assert "rotated-access" not in str(status)
    assert "rotated-refresh" not in str(status)


def test_refresh_write_failure_preserves_the_active_pair(tmp_path, monkeypatch):
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=FakeRefreshSession,
    )
    monkeypatch.setattr(m365_bearer, "_atomic_write_json", lambda *_: (_ for _ in ()).throw(OSError("disk unavailable")))

    with pytest.raises(BetaUpstreamError, match="oauth_refresh_failed"):
        beta.refresh()

    assert beta.credential.access_token == "access-secret"
    assert beta.credential.refresh_token == "refresh-secret"
    assert beta.status()["last_refresh_outcome"] == "failed"


@pytest.mark.skipif(
    os.environ.get("CODEX_AUTH_M365_BETA_CONFIRM") != "1",
    reason="requires an explicitly confirmed fresh local M365 beta credential",
)
def test_live_cookie_free_beta_probe():
    beta = M365BearerBeta.from_directory()

    response = beta.generate("Reply exactly with: M365_BETA_OK")

    assert response
    assert beta.status()["cookie_count"] == 0
