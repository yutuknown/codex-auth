import asyncio

import pytest

from codex_auth.providers.base import BaseProvider, ProviderCapabilities
from codex_auth.providers.errors import (
    ProviderNotConfiguredError,
    ProviderNotFoundError,
)
from codex_auth.providers.registry import ProviderRegistry


class FakeProvider(BaseProvider):
    provider_id = "fake"
    display_name = "Fake"
    auth_kind = "test"
    capabilities = ProviderCapabilities(text=True)

    def __init__(self, configured=True):
        self.configured = configured
        self.initialized = False
        self.closed = False
        self.last_model = None
        self.last_refresh = None

    def is_configured(self):
        return self.configured

    async def initialize(self):
        self.initialized = True

    async def generate_stream(
        self,
        prompt,
        files=None,
        web_search=False,
        model=None,
        realtime=False,
    ):
        self.last_model = model
        yield prompt

    async def fetch_models(self, *, refresh=False):
        self.last_refresh = refresh
        return [{"slug": "model"}]

    async def reset_session(self, model):
        return None

    async def close(self):
        self.closed = True


def test_registry_keeps_factories_lazy_until_provider_is_selected():
    calls = []
    registry = ProviderRegistry(default_provider_id="fake")
    registry.register_factory("fake", lambda: calls.append("created") or FakeProvider())

    assert registry.ids() == ("fake",)
    assert calls == []

    selection = registry.select("fake:model")

    assert calls == ["created"]
    assert selection.provider_id == "fake"
    assert selection.model == "model"


def test_registry_supports_default_alias_and_explicit_provider():
    registry = ProviderRegistry(default_provider_id="fake")
    provider = FakeProvider()
    registry.register(provider)

    assert registry.select("model").provider is provider
    assert registry.select("model", "fake").model == "model"


def test_registry_rejects_conflicts_unknown_and_unconfigured_providers():
    registry = ProviderRegistry(default_provider_id="fake")
    registry.register(FakeProvider(configured=False))

    with pytest.raises(ProviderNotConfiguredError):
        registry.select("model")
    with pytest.raises(ProviderNotFoundError):
        registry.select("missing:model")
    with pytest.raises(ProviderNotFoundError):
        registry.select("fake:model", "missing")


def test_registry_lifecycle_only_closes_created_instances():
    registry = ProviderRegistry(default_provider_id="fake")
    default = FakeProvider()
    untouched = FakeProvider()
    untouched.provider_id = "untouched"
    registry.register(default)
    registry.register_factory("untouched", lambda: untouched)

    asyncio.run(registry.initialize_default())
    asyncio.run(registry.close_instances())

    assert default.initialized is True
    assert default.closed is True
    assert untouched.closed is False


def test_chat_route_reports_namespace_but_passes_upstream_model(monkeypatch):
    from codex_auth.api import routes_openai
    from codex_auth.api.routes_openai import (
        ChatCompletionRequest,
        ChatMessage,
        openai_chat_completions,
    )

    isolated_registry = ProviderRegistry(default_provider_id="fake")
    provider = FakeProvider()
    isolated_registry.register(provider)
    monkeypatch.setattr(routes_openai, "registry", isolated_registry)
    monkeypatch.setattr(routes_openai, "record_usage", lambda *args, **kwargs: None)

    response = asyncio.run(
        openai_chat_completions(
            ChatCompletionRequest(
                model="fake:model",
                messages=[ChatMessage(role="user", content="hello")],
            )
        )
    )

    assert provider.last_model == "model"
    assert response["model"] == "fake:model"


def test_dashboard_model_catalog_combines_namespaced_providers(monkeypatch):
    from codex_auth.api.routes_ui import get_models_list
    from codex_auth.providers import runtime

    isolated_registry = ProviderRegistry(default_provider_id="fake")
    default = FakeProvider()
    secondary = FakeProvider()
    secondary.provider_id = "secondary"
    secondary.display_name = "Secondary"
    isolated_registry.register(default)
    isolated_registry.register(secondary)
    monkeypatch.setattr(runtime, "registry", isolated_registry)

    result = asyncio.run(get_models_list(refresh=True))

    assert {model["id"] for model in result["models"]} == {
        "fake:model",
        "secondary:model",
    }
    assert result["default_model"] == "fake:auto"
    assert default.last_refresh is True
    assert secondary.last_refresh is True
