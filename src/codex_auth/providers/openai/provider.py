import asyncio
import base64
import json
import logging
import mimetypes
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict

from ...core.browser import AccountBlockedError, CaptchaDetectedError, StealthTimeoutError
from ..base import BaseProvider

logger = logging.getLogger("codex_auth")

class OpenAIProvider(BaseProvider):
    def __init__(self):
        self.context = None
        self.page = None
        self.engine = None

    def load_session_token(self):
        auth_file = Path(__file__).resolve().parent.parent.parent.parent.parent / ".codex" / "auth.json"
        if not auth_file.exists():
            raise Exception(f"Could not find auth.json at {auth_file}")
        with open(auth_file, "r") as f:
            data = json.load(f)
            token = data.get("tokens", {}).get("refresh_token")
            if not token:
                raise Exception("Session token not found in auth.json")
            return token

    async def initialize(self, engine):
        self.engine = engine
        try:
            token = self.load_session_token()
        except Exception as e:
            logger.error(f"[OpenAI] Error loading token: {e}")
            raise e

        self.context = await engine.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        await self.context.add_cookies([
            {
                "name": "__Secure-next-auth.session-token",
                "value": token,
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax"
            }
        ])
        self.page = await self.context.new_page()
        logger.info("[OpenAI] Navigating to chatgpt.com...")
        # Fix the networkidle crash by using domcontentloaded
        await self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        logger.info("[OpenAI] Provider ready and authenticated!")

    async def get_context(self):
        return self.context

    async def reset_session(self, model: str):
        await self.page.goto(f"https://chatgpt.com/?model={model}", wait_until="domcontentloaded")

    async def generate_stream(self, prompt: str, files: list = None, web_search: bool = False) -> AsyncGenerator[str, None]:
        async with self.engine.lock:
            logger.info(f"[OpenAI] Processing prompt stream: {prompt[:50]}...")
            
            temp_files = []
            if files:
                for i, file_b64 in enumerate(files):
                    ext = ".jpg"
                    if file_b64.startswith("data:"):
                        header, file_b64 = file_b64.split(",", 1)
                        mime_type = header.split(";")[0].replace("data:", "")
                        guessed_ext = mimetypes.guess_extension(mime_type)
                        if guessed_ext: ext = guessed_ext
                    try:
                        file_data = base64.b64decode(file_b64)
                        temp_path = os.path.join(tempfile.gettempdir(), f"stealth_upload_{uuid.uuid4().hex}{ext}")
                        with open(temp_path, "wb") as f: f.write(file_data)
                        temp_files.append(temp_path)
                    except Exception as e:
                        logger.error(f"[OpenAI] Error decoding base64 file: {e}")
                if temp_files:
                    file_input = self.page.locator('input[type="file"]').first
                    await file_input.set_input_files(temp_files)
                    await asyncio.sleep(3)
            
            if web_search:
                search_btn = self.page.locator('button[aria-label="Search Web"], button[aria-label="Search"]')
                if await search_btn.count() > 0:
                    await search_btn.first.click()
                    await asyncio.sleep(0.5)

            await self.page.fill("#prompt-textarea", prompt)
            await asyncio.sleep(1)
            
            send_btn = self.page.locator('button[data-testid="send-button"]')
            if await send_btn.count() > 0:
                await send_btn.click()
            else:
                await self.page.keyboard.press("Enter")
            
            try:
                for _ in range(30):
                    if await self.page.locator('iframe[src*="cloudflare"]').count() > 0:
                        raise CaptchaDetectedError("Cloudflare Turnstile CAPTCHA detected.")
                    if await self.page.locator('text="Account deactivated"').count() > 0:
                        raise AccountBlockedError("OpenAI account deactivated or blocked.")
                    if await self.page.locator('div[data-message-author-role="assistant"]').count() > 0:
                        break
                    await asyncio.sleep(1)
                else:
                    raise StealthTimeoutError("Timed out waiting for assistant response to start.")
            except (CaptchaDetectedError, AccountBlockedError, StealthTimeoutError) as e:
                raise e
            
            last_text = ""
            stable_count = 0
            empty_count = 0
            
            while True:
                assistant_messages = await self.page.locator('div[data-message-author-role="assistant"]').all()
                if not assistant_messages:
                    break
                    
                current_text = (await assistant_messages[-1].inner_text()).strip()
                
                if not current_text:
                    empty_count += 1
                    if empty_count > 150: # 15 seconds timeout
                        raise StealthTimeoutError("Timeout waiting for model to start typing")
                    await asyncio.sleep(0.1)
                    continue
                    
                stop_btn = self.page.locator('button[aria-label="Stop generating"]')
                is_generating = await stop_btn.count() > 0
                
                if current_text != last_text:
                    new_chunk = current_text[len(last_text):]
                    if new_chunk:
                        yield new_chunk
                    last_text = current_text
                    stable_count = 0
                else:
                    if not is_generating:
                        stable_count += 1
                    else:
                        stable_count = 0
                    
                if stable_count > 35: # 3.5 seconds of stability AND no stop button
                    break
                    
                await asyncio.sleep(0.1)
                
            logger.info("[OpenAI] Generated response stream completed.")
            
            for tmp_f in temp_files:
                try: os.remove(tmp_f)
                except: pass

    async def fetch_models(self) -> list[Dict[str, Any]]:
        try:
            response = await self.context.request.get("https://chatgpt.com/backend-api/models")
            if response.ok:
                data = await response.json()
                return data.get("models", [])
        except Exception as e:
            logger.error(f"[OpenAI] Failed to fetch real models: {e}")
        return [{"slug": "auto", "max_tokens": 128000}, {"slug": "gpt-5-5", "max_tokens": 34834}, {"slug": "gpt-5-3", "max_tokens": 34834}]

# We instantiate a singleton for the router to use
provider = OpenAIProvider()
