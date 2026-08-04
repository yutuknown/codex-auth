"""Server-owned OAuth authorization-code flow for the hosted M365 beta.

This module deliberately does not intercept the Microsoft first-party client.
It only works with an operator-owned Entra application whose callback is this
service.  Tokens are handed directly to the existing credential manager and
are never returned by these helpers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from beta.m365_bearer import M365BearerBeta

OAUTH_CLIENT_ID_ENV = "CODEX_AUTH_M365_BETA_OAUTH_CLIENT_ID"
OAUTH_CLIENT_SECRET_ENV = "CODEX_AUTH_M365_BETA_OAUTH_CLIENT_SECRET"
OAUTH_TENANT_ENV = "CODEX_AUTH_M365_BETA_OAUTH_TENANT"
OAUTH_REDIRECT_URI_ENV = "CODEX_AUTH_M365_BETA_OAUTH_REDIRECT_URI"
OAUTH_SYDNEY_SCOPE_ENV = "CODEX_AUTH_M365_BETA_OAUTH_SYDNEY_SCOPE"
OAUTH_GRAPH_SCOPE_ENV = "CODEX_AUTH_M365_BETA_OAUTH_GRAPH_SCOPE"
OAUTH_TRANSACTION_TTL = 600
DEFAULT_SYDNEY_SCOPE = "https://substrate.office.com/sydney/v2/.default openid profile offline_access"
DEFAULT_GRAPH_SCOPE = "https://graph.microsoft.com/.default openid profile offline_access"


@dataclass
class OAuthTransaction:
    state: str
    verifier: str
    session_binding: str
    created_at: int
    redirect_uri: str


_transactions: dict[str, OAuthTransaction] = {}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def configured() -> bool:
    return bool(os.environ.get(OAUTH_CLIENT_ID_ENV) and os.environ.get(OAUTH_CLIENT_SECRET_ENV))


def redirect_uri(base_url: str | None = None) -> str:
    return (os.environ.get(OAUTH_REDIRECT_URI_ENV) or f"{(base_url or '').rstrip('/')}/dashboard/oauth/callback").rstrip('/')


def _tenant() -> str:
    return os.environ.get(OAUTH_TENANT_ENV) or "common"


def _session_binding(cookie: str) -> str:
    secret = (os.environ.get("CODEX_AUTH_M365_BETA_DASHBOARD_SESSION_KEY") or "").encode()
    return hmac.new(secret, cookie.encode(), hashlib.sha256).hexdigest()


def start(session_cookie: str, *, base_url: str = "") -> dict[str, Any]:
    if not configured():
        return {"available": False, "state": "unconfigured", "reason": "operator_oauth_app_not_configured"}
    now = int(time.time())
    for key, item in list(_transactions.items()):
        if now - item.created_at > OAUTH_TRANSACTION_TTL:
            _transactions.pop(key, None)
    state = secrets.token_urlsafe(32)
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    uri = redirect_uri(base_url)
    _transactions[state] = OAuthTransaction(state, verifier, _session_binding(session_cookie), now, uri)
    query = {
        "client_id": os.environ[OAUTH_CLIENT_ID_ENV],
        "response_type": "code",
        "redirect_uri": uri,
        "response_mode": "query",
        "scope": os.environ.get(OAUTH_SYDNEY_SCOPE_ENV, DEFAULT_SYDNEY_SCOPE),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {
        "available": True,
        "state": "started",
        "authorization_url": f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/authorize?{urllib.parse.urlencode(query)}",
        "expires_in_seconds": OAUTH_TRANSACTION_TTL,
        "provider": "m365-copilot",
    }


def consume(state: str, session_cookie: str) -> OAuthTransaction:
    item = _transactions.pop(state, None)
    if item is None:
        raise ValueError("oauth_state_invalid_or_expired")
    if int(time.time()) - item.created_at > OAUTH_TRANSACTION_TTL:
        raise ValueError("oauth_state_invalid_or_expired")
    if not hmac.compare_digest(item.session_binding, _session_binding(session_cookie)):
        raise ValueError("oauth_session_mismatch")
    return item


def exchange(transaction: OAuthTransaction, code: str) -> dict[str, Any]:
    endpoint = f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/token"
    form = {
        "client_id": os.environ[OAUTH_CLIENT_ID_ENV],
        "client_secret": os.environ[OAUTH_CLIENT_SECRET_ENV],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": transaction.redirect_uri,
        "code_verifier": transaction.verifier,
        "scope": os.environ.get(OAUTH_SYDNEY_SCOPE_ENV, DEFAULT_SYDNEY_SCOPE),
    }
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode(form).encode(), method="POST", headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(128 * 1024))
    except Exception as exc:
        raise ValueError("oauth_code_exchange_failed") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("oauth_code_exchange_invalid_response")
    if not payload.get("refresh_token"):
        raise ValueError("oauth_refresh_metadata_missing")
    scope = str(payload.get("scope") or "")
    required = os.environ.get(OAUTH_SYDNEY_SCOPE_ENV, "https://substrate.office.com/sydney/v2/.default").split()[0]
    if required not in scope and "substrate.office.com/sydney/v2" not in scope:
        raise ValueError("oauth_required_sydney_scope_missing")
    return payload


def import_server_response(response: dict[str, Any]) -> dict[str, Any]:
    """Attach safe route metadata, then atomically activate the credential."""
    candidate = dict(response)
    token = str(candidate.get("id_token") or "")
    claims: dict[str, Any] = {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)))
    except Exception:
        pass
    tenant = str(claims.get("tid") or _tenant())
    client = str(claims.get("aud") or os.environ[OAUTH_CLIENT_ID_ENV])
    identity = str(claims.get("preferred_username") or claims.get("email") or "")
    if not identity:
        raise ValueError("oauth_identity_missing")
    candidate["route"] = {
        "identity": identity,
        "oauth": {
            "capture_complete": True,
            "token_endpoint": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            "query": {"client_id": client},
            "form": {"client_id": client, "grant_type": "refresh_token", "scope": DEFAULT_SYDNEY_SCOPE},
        },
    }
    return M365BearerBeta.from_directory().replace_credential(candidate)
