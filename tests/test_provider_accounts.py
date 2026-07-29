import asyncio

import pytest
from fastapi import HTTPException

from codex_auth.api import routes_ui
from codex_auth.providers.base import BaseProvider, ProviderCapabilities
from codex_auth.providers.errors import ProviderUpstreamError
from codex_auth.providers.registry import ProviderRegistry


class AccountProvider(BaseProvider):
    provider_id = "account"
    display_name = "Account Provider"
    auth_kind = "test session"
    capabilities = ProviderCapabilities(text=True, streaming=True)

    def __init__(self, fail=False):
        self.fail = fail

    async def initialize(self):
        return None

    async def generate_stream(self, prompt, files=None, web_search=False, model=None, realtime=False):
        yield prompt

    async def fetch_models(self, *, refresh=False):
        return []

    async def reset_session(self, model):
        return None

    def runtime_status(self):
        return {
            "provider": self.provider_id,
            "configured": True,
            "initialized": True,
            "generation_ready": True,
            "auth_mode": "test",
            "model_count": 2,
            "access_token": "must-not-leak",
        }

    async def fetch_account_snapshot(self, *, refresh=False):
        if self.fail:
            raise ProviderUpstreamError("profile source failed")
        snapshot = await super().fetch_account_snapshot(refresh=refresh)
        snapshot["profile"] = {"name": "Example User"}
        return snapshot


def install_registry(monkeypatch, providers):
    from codex_auth.providers import runtime

    registry = ProviderRegistry(default_provider_id=providers[0].provider_id)
    for provider in providers:
        registry.register(provider)
    monkeypatch.setattr(runtime, "registry", registry)
    return registry


def test_aggregate_provider_accounts_keeps_partial_failures_visible(monkeypatch):
    good = AccountProvider()
    failing = AccountProvider(fail=True)
    failing.provider_id = "failing"
    install_registry(monkeypatch, [good, failing])

    result = asyncio.run(routes_ui.get_provider_accounts())

    assert result["default_provider"] == "account"
    assert [item["provider"]["id"] for item in result["providers"]] == ["account", "failing"]
    assert result["providers"][0]["profile"]["name"] == "Example User"
    assert result["providers"][1]["connection"]["state"] == "error"
    assert "must-not-leak" not in str(result)


def test_provider_account_detail_and_unknown_provider(monkeypatch):
    install_registry(monkeypatch, [AccountProvider()])

    result = asyncio.run(routes_ui.get_provider_account("account"))

    assert result["provider"]["id"] == "account"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_ui.get_provider_account("missing"))
    assert exc.value.status_code == 404

