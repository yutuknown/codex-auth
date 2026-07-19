---
name: f12-token-extraction
description: Workflow for guiding users to manually extract web session tokens via F12 DevTools and mapping the raw data into structured auth files.
---

# F12 Manual Token Extraction Workflow

When a user requests to extract web tokens manually via browser DevTools (F12) rather than using an automated script, follow this strict interactive workflow:

## Step 1: Guide the User
Provide the user with a step-by-step artifact guide detailing exactly what network requests or cookies to look for in the F12 Network/Application tabs. 
- Ask them to find the `authorization: Bearer <token>` in the request headers.
- Ask them to find the required session cookies (e.g., `__Secure-next-auth.session-token`).
- Explicitly ask the user to paste the raw text/JSON results back into the chat.

## Step 2: Receive and Map Data
Once the user provides the raw pasted text:
1. Extract the raw JWT or tokens from the messy header/cookie strings.
2. If cookies are split (e.g., `.0` and `.1`), dynamically concatenate them.
3. Map the extracted values into the exact structured format required by the target application (e.g., `auth.json`).

## Step 3: Document and Arrange
1. Write the final formatted JSON to the required file location (e.g., `.codex/auth.json` or globally if specified).
2. Confirm the successful mapping and placement of the token so the user knows they can immediately use their desktop apps.
