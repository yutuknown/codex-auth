import asyncio
import os

import pytest
from fastapi import HTTPException

from codex_auth.api import routes_ui
from codex_auth.providers.errors import ProviderUpstreamError
from codex_auth.providers.microsoft365 import Microsoft365CopilotProvider
from codex_auth.providers.registry import ProviderRegistry


def generation_auth():
    return {
        "access_token": "access-secret",
        "identity": "user@tenant",
        "expires_in": 3600,
    }


def generation_oauth():
    return {
        "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        "form": {"grant_type": "refresh_token", "refresh_token": "refresh-secret"},
    }


def test_generation_import_is_safe_and_marks_active(monkeypatch):
    saved = {}
    monkeypatch.setenv("CODEX_AUTH_M365_AUTH_JSON", "")
    monkeypatch.setenv("CODEX_AUTH_M365_OAUTH_JSON", "")
    monkeypatch.setattr("codex_auth.providers.microsoft365.save_m365_auth_data", lambda value: saved.setdefault("auth", value))
    monkeypatch.setattr("codex_auth.providers.microsoft365.save_m365_oauth_data", lambda value: saved.setdefault("oauth", value))
    provider = Microsoft365CopilotProvider()

    lifecycle = asyncio.run(provider.replace_generation_credentials(generation_auth(), generation_oauth()))

    assert lifecycle["state"] == "active"
    assert provider.generation_ready is True
    assert saved["auth"]["access_token"] == "access-secret"
    assert "access_token" not in provider.runtime_status()
    assert "access-secret" not in str(provider.generation_credential_status())
    assert os.environ["CODEX_AUTH_M365_AUTH_JSON"].find("access-secret") >= 0


def test_generation_import_rejects_incomplete_refresh_bundle():
    provider = Microsoft365CopilotProvider()

    with pytest.raises(ValueError, match="form.refresh_token"):
        asyncio.run(provider.replace_generation_credentials(generation_auth(), {"token_endpoint": generation_oauth()["token_endpoint"], "form": {}}))


def test_failed_refresh_preserves_valid_runtime_bearer(monkeypatch):
    monkeypatch.setenv("CODEX_AUTH_M365_AUTH_JSON", "")
    monkeypatch.setenv("CODEX_AUTH_M365_OAUTH_JSON", "")
    provider = Microsoft365CopilotProvider()
    provider.access_token = "current-access"
    provider.identity = "user@tenant"
    provider.access_token_expires_at = 9_999_999_999
    provider.initialized = True
    monkeypatch.setattr(provider, "_refresh_access_token_sync", lambda: (_ for _ in ()).throw(ProviderUpstreamError("refresh rejected")))

    lifecycle = asyncio.run(provider.refresh_generation_credentials())

    assert provider.access_token == "current-access"
    assert lifecycle["state"] == "refresh_failed"
    assert lifecycle["recovery_action"] == "refresh"


def test_refreshing_lifecycle_is_safe_and_transient():
    provider = Microsoft365CopilotProvider()
    provider.access_token = "current-access"
    provider.identity = "user@tenant"
    provider.access_token_expires_at = 9_999_999_999
    provider.generation_refresh_in_progress = True

    lifecycle = provider.generation_credential_status()

    assert lifecycle["state"] == "refreshing"
    assert "current-access" not in str(lifecycle)


class ManagedProvider:
    provider_id = "m365-copilot"

    def __init__(self):
        self.received = None

    async def replace_generation_credentials(self, auth, oauth):
        self.received = (auth, oauth)
        return {"state": "active", "access_expires_in_seconds": 3600, "refresh_available": True}

    async def fetch_account_snapshot(self, *, refresh=False):
        return {"connection": {"generation_ready": True}}

    async def refresh_generation_credentials(self):
        return {"state": "active", "access_expires_in_seconds": 3500, "refresh_available": True}

    async def clear_generation_credentials(self):
        return {"state": "re_import_required", "access_expires_in_seconds": None, "refresh_available": False}


def install_managed_provider(monkeypatch):
    from codex_auth.providers import runtime

    provider = ManagedProvider()
    registry = ProviderRegistry(default_provider_id="m365-copilot")
    registry.register(provider)
    monkeypatch.setattr(runtime, "registry", registry)
    return provider


def test_generation_credential_endpoint_returns_only_safe_lifecycle(monkeypatch):
    provider = install_managed_provider(monkeypatch)
    payload = routes_ui.GenerationCredentialUpdateRequest(
        auth_json='{"access_token":"access-secret","identity":"user@tenant"}',
        oauth_json='{"token_endpoint":"https://login.microsoftonline.com/tenant/oauth2/v2.0/token","form":{"refresh_token":"refresh-secret"}}',
    )

    result = asyncio.run(routes_ui.update_m365_generation_credentials(payload))

    assert result["generation_credential"]["state"] == "active"
    assert "access-secret" not in str(result)
    assert "refresh-secret" not in str(result)
    assert provider.received[0]["identity"] == "user@tenant"


def test_clear_generation_credentials_requires_confirmation():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_ui.clear_m365_generation_credentials(routes_ui.CredentialClearRequest(confirm=False)))
    assert exc.value.status_code == 422
