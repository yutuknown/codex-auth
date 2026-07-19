from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict


class BaseProvider(ABC):
    @abstractmethod
    async def initialize(self, engine) -> None:
        """
        Initialize the provider by creating a browser context from the engine,
        injecting authentication cookies/headers, and navigating to the necessary URL.
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
    async def get_context(self):
        """
        Return the Playwright BrowserContext associated with this provider,
        so that routes can make direct HTTP proxy requests if needed.
        """
        pass

    @abstractmethod
    async def reset_session(self, model: str):
        """
        Reset the chat session or navigate to a new context.
        """
        pass
