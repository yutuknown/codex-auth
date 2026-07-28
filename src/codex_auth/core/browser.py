import asyncio
import logging
import os
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
        browser_args = [
            "--disable-background-networking",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-renderer-backgrounding",
            "--renderer-process-limit=1",
        ]
        if os.environ.get("CODEX_AUTH_LOW_MEMORY", "false").lower() in {"1", "true", "yes"}:
            browser_args.extend(
                [
                    "--js-flags=--max-old-space-size=128",
                    "--no-zygote",
                    "--single-process",
                ]
            )
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=browser_args,
        )
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
