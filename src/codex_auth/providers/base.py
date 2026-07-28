from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict


class BaseProvider(ABC):
    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the provider and validate its authenticated HTTP session.
        """
        pass

    @abstractmethod
    async def generate_stream(self, prompt: str, files: list = None, web_search: bool = False) -> AsyncGenerator[str, None]:
        """
        Generate a text stream from the AI provider.
        Yields chunks of text as they appear.
        """
        pass

    @abstractmethod
    async def fetch_models(self) -> list[Dict[str, Any]]:
        """
        Fetch the list of real models supported by this provider.
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
