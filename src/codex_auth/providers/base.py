from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, AsyncGenerator, Dict


@dataclass(frozen=True)
class ProviderCapabilities:
    text: bool = False
    streaming: bool = False
    image_input: bool = False
    file_input: bool = False
    web_search: bool = False
    tools: bool = False
    image_generation: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


class BaseProvider(ABC):
    provider_id = "unknown"
    display_name = "Unknown provider"
    auth_kind = "unknown"
    capabilities = ProviderCapabilities()

    def descriptor(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "display_name": self.display_name,
            "auth_kind": self.auth_kind,
            "capabilities": self.capabilities.to_dict(),
        }

    def is_configured(self) -> bool:
        return True

    def runtime_status(self) -> dict[str, Any]:
        return {
            "initialized": False,
            "configured": self.is_configured(),
            "provider": self.provider_id,
            "capabilities": self.capabilities.to_dict(),
        }

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the provider and validate its authenticated HTTP session.
        """
        pass

    @abstractmethod
    async def generate_stream(
        self,
        prompt: str,
        files: list = None,
        web_search: bool = False,
        model: str | None = None,
        realtime: bool = False,
    ) -> AsyncGenerator[str, None]:
        """
        Generate a text stream from the AI provider.
        Yields chunks of text as they appear.
        """
        pass

    @abstractmethod
    async def fetch_models(self, *, refresh: bool = False) -> list[Dict[str, Any]]:
        """
        Fetch the list of real models supported by this provider.
        Providers may use a short-lived cache unless refresh is requested.
        """
        pass

    @abstractmethod
    async def reset_session(self, model: str):
        """
        Reset the chat session or navigate to a new context.
        """
        pass

    async def close(self) -> None:
        """Release provider resources."""
