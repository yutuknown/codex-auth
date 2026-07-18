import asyncio
import json
from playwright.async_api import async_playwright
from pathlib import Path

async def main():
    auth_file = Path(__file__).resolve().parent.parent / ".codex" / "auth.json"
    with open(auth_file, "r") as f:
        token = json.load(f)["tokens"]["refresh_token"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        await context.add_cookies([{
            "name": "__Secure-next-auth.session-token",
            "value": token,
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax"
        }])
        page = await context.new_page()
        await page.goto("https://chatgpt.com/", wait_until="networkidle")
        
        print("Fetching /backend-api/models...")
        r_models = await context.request.get("https://chatgpt.com/backend-api/models")
        if r_models.ok:
            models_json = await r_models.json()
            with open("models.json", "w") as f:
                json.dump(models_json, f, indent=2)
            
        print("\nFetching /backend-api/conversation/init...")
        r_init = await context.request.post("https://chatgpt.com/backend-api/conversation/init", data="{}")
        if r_init.ok:
            init_json = await r_init.json()
            with open("init.json", "w") as f:
                json.dump(init_json, f, indent=2)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
