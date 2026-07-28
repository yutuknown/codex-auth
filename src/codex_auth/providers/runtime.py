import os

from .microsoft365 import Microsoft365CopilotProvider
from .openai.provider import provider as openai_provider
from .registry import ProviderRegistry

registry = ProviderRegistry(default_provider_id=os.environ.get("CODEX_AUTH_DEFAULT_PROVIDER", "openai-web"))
registry.register(openai_provider)
registry.register_factory("m365-copilot", Microsoft365CopilotProvider)
