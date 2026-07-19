---
name: f12-token-extraction
description: Workflow for guiding users to manually extract web session tokens via F12 DevTools and mapping them into the codex-auth proxy auth.json.
---

# F12 Manual Token Extraction Workflow (codex-auth)

When a user requests to extract web tokens manually via browser DevTools (F12) for the `codex-auth` proxy server, follow this strict interactive workflow:

## Context
This project is a Stealth Playwright proxy providing an OpenAI-compatible API to ChatGPT. Sometimes the automated Playwright login fails or the user prefers manual extraction.

## Step 1: Guide the User
Provide the user with a step-by-step artifact guide detailing exactly what network requests or cookies to look for in the F12 Network/Application tabs. 
- Ask them to find the `authorization: Bearer <token>` in the request headers (e.g., from `/backend-api/conversation/...`).
- Ask them to find the required session cookies (e.g., `__Secure-next-auth.session-token`).
- Explicitly ask the user to paste the raw text/JSON results back into the chat.

## Step 2: Receive and Map Data
Once the user provides the raw pasted text:
1. Extract the raw JWT or tokens from the messy header/cookie strings.
2. If cookies are split (e.g., `.0` and `.1`), dynamically concatenate them.
3. Map the extracted values into the exact structured format required by the `codex-auth` proxy.

```json
{
  "tokens": {
    "access_token": "<extracted_jwt>",
    "refresh_token": "<extracted_cookie>"
  }
}
```

## Step 3: Document and Arrange
1. Write the final formatted JSON to the local proxy configuration path: `C:\Users\abhis\OneDrive\Documents\codex-auth\.codex\auth.json`.
2. Do **not** overwrite the global `~/.codex/auth.json` from within this project to protect the user's primary credentials (unless explicitly requested).
3. Confirm the successful mapping and placement of the token so the user knows they can instantly start the `codex-auth` proxy server.
