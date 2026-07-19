import asyncio
import logging
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

logger = logging.getLogger("codex_auth")

class StealthTimeoutError(Exception):
    pass

class CaptchaDetectedError(Exception):
    pass

class AccountBlockedError(Exception):
    pass

class PlaywrightEngine:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(self):
        """
        Generic Playwright engine startup. 
        It does NOT load cookies or hit provider URLs. 
        That is the responsibility of the Provider.
        """
        logger.info("[API] Starting Stealth Playwright Engine...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        yield self
        
        logger.info("[API] Shutting down Playwright Engine...")
        try:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            # Swallow connection closed errors during abrupt Ctrl+C teardowns
            pass

# Global singleton engine
engine = PlaywrightEngine()
