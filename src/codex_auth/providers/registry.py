from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from .base import BaseProvider
from .errors import ProviderNotConfiguredError, ProviderNotFoundError

ProviderFactory = Callable[[], BaseProvider]


@dataclass(frozen=True)
class ProviderSelection:
    provider_id: str
    model: str
    provider: BaseProvider


class ProviderRegistry:
    """Lazy provider registry with deterministic model routing."""

    def __init__(self, default_provider_id: str = "openai-web") -> None:
        self.default_provider_id = default_provider_id
        self._factories: dict[str, ProviderFactory] = {}
        self._instances: dict[str, BaseProvider] = {}
        self._initialized: set[str] = set()
        self._initialization_locks: dict[str, asyncio.Lock] = {}

    def register(self, provider: BaseProvider) -> None:
        self._validate_id(provider.provider_id)
        self._instances[provider.provider_id] = provider
        self._factories[provider.provider_id] = lambda provider=provider: provider

    def register_factory(self, provider_id: str, factory: ProviderFactory) -> None:
        self._validate_id(provider_id)
        if provider_id in self._factories:
            raise ValueError(f"Provider '{provider_id}' is already registered")
        self._factories[provider_id] = factory

    @staticmethod
    def _validate_id(provider_id: str) -> None:
        if not provider_id or ":" in provider_id:
            raise ValueError("Provider IDs must be non-empty and cannot contain ':'")

    def ids(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def get(self, provider_id: str) -> BaseProvider:
        factory = self._factories.get(provider_id)
        if factory is None:
            raise ProviderNotFoundError(f"Unknown provider '{provider_id}'")
        provider = self._instances.get(provider_id)
        if provider is None:
            provider = factory()
            if provider.provider_id != provider_id:
                raise RuntimeError(
                    f"Provider factory for '{provider_id}' returned '{provider.provider_id}'"
                )
            self._instances[provider_id] = provider
        return provider

    def select(self, model: str, explicit_provider: str | None = None) -> ProviderSelection:
        provider_from_model = None
        upstream_model = model
        if ":" in model:
            provider_from_model, upstream_model = model.split(":", 1)
            if not provider_from_model or not upstream_model:
                raise ProviderNotFoundError(f"Invalid namespaced model '{model}'")
        if explicit_provider and provider_from_model and explicit_provider != provider_from_model:
            raise ProviderNotFoundError(
                f"Provider '{explicit_provider}' conflicts with model namespace "
                f"'{provider_from_model}'"
            )
        provider_id = explicit_provider or provider_from_model or self.default_provider_id
        provider = self.get(provider_id)
        if not provider.is_configured():
            raise ProviderNotConfiguredError(
                f"Provider '{provider_id}' is registered but not configured"
            )
        return ProviderSelection(provider_id, upstream_model, provider)

    async def ensure_initialized(self, provider_id: str) -> BaseProvider:
        provider = self.get(provider_id)
        if not provider.is_configured():
            raise ProviderNotConfiguredError(
                f"Provider '{provider_id}' is not configured"
            )
        if provider_id in self._initialized:
            return provider
        lock = self._initialization_locks.setdefault(provider_id, asyncio.Lock())
        async with lock:
            if provider_id not in self._initialized:
                await provider.initialize()
                self._initialized.add(provider_id)
        return provider

    async def initialize_default(self) -> None:
        await self.ensure_initialized(self.default_provider_id)

    async def close_instances(self) -> None:
        for provider in reversed(tuple(self._instances.values())):
            await provider.close()
        self._initialized.clear()
        self._initialization_locks.clear()

    def statuses(self) -> list[dict]:
        statuses = []
        for provider_id in self.ids():
            provider = self.get(provider_id)
            status = provider.runtime_status()
            status.setdefault("provider", provider_id)
            status.setdefault("configured", provider.is_configured())
            descriptor = provider.descriptor()
            effective_capabilities = status.get(
                "proxy_capabilities", descriptor["capabilities"]
            )
            statuses.append({
                **descriptor,
                "effective_capabilities": effective_capabilities,
                "runtime": status,
            })
        return statuses
