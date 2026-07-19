import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    auth_file = Path(r"c:\Users\abhis\OneDrive\Documents\codex-auth\.codex\auth.json")
    with open(auth_file, "r") as f:
        data = json.load(f)
        token = data.get("tokens", {}).get("refresh_token")

    # Create dummy files for testing
    scratch_dir = Path(r"c:\Users\abhis\OneDrive\Documents\codex-auth\scratch")
    scratch_dir.mkdir(exist_ok=True)
    
    test_txt = scratch_dir / "test_doc.txt"
    with open(test_txt, "w") as f:
        f.write("Hello! This is a super secret text file. The magic word is HACKERMAN.")
        
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
        print("Navigating to ChatGPT...")
        await page.goto("https://chatgpt.com/", wait_until="networkidle")
        
        print("Finding file input and uploading test_doc.txt...")
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files([str(test_txt)])
        
        print("Waiting 5 seconds for upload to process...")
        await asyncio.sleep(5)
        
        print("Filling prompt...")
        await page.fill("#prompt-textarea", "Read the attached file and tell me the magic word.")
        await asyncio.sleep(1) # wait for button to become enabled
        await page.locator('button[data-testid="send-button"]').click()
        
        print("Waiting for response...")
        try:
            await page.wait_for_selector('div[data-message-author-role="assistant"]', state="attached", timeout=30000)
            
            # Wait for generation to start
            try:
                await page.wait_for_selector('button[aria-label="Stop generating"]', state="attached", timeout=5000)
            except Exception:
                pass
                
            # Wait for generation to finish
            await page.wait_for_selector('button[aria-label="Stop generating"]', state="detached", timeout=30000)
            
        except Exception:
            print("Timed out waiting for assistant. Taking screenshot...")
            await page.screenshot(path="scratch/timeout.png")
            return
            
        # Get the final text
        msgs = await page.locator('div[data-message-author-role="assistant"]').all()
        last_text = await msgs[-1].inner_text() if msgs else ""
            
        print("\n--- ChatGPT Response ---")
        print(last_text)
        print("------------------------")

if __name__ == "__main__":
    asyncio.run(main())
