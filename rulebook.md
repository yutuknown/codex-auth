# Codex Auth Project Rulebook

This file is the operating contract for future work on this repository. Read it before changing, testing, or deploying the project.

## 1. Project boundaries

- `src/` and the production `codex-auth` Render service are production scope.
- `beta/` and `codex-auth-beta.onrender.com` are the Microsoft 365 experimental scope.
- Never promote beta behavior into production without a separate review and matching live evidence.
- Gemini is out of scope unless the user explicitly adds it.
- Preserve existing logos, README content, and unrelated user changes.
- Auto-deploy stays disabled. Deployments are deliberate and beta-only unless the user explicitly names production.

## 2. Secrets and credentials

- Never print, quote, commit, log, or return access tokens, refresh tokens, cookies, API keys, client secrets, protected upload URLs, or authorization headers.
- Local beta secrets belong in ignored `beta/.env` or ignored beta credential files.
- Render secrets belong in Render environment variables, never source files.
- The beta API key variable is `CODEX_AUTH_M365_BETA_API_KEY`.
- Dashboard administration uses `CODEX_AUTH_M365_BETA_ADMIN_KEY`; it is not the public compatibility API key.
- Durable hosted credentials require both:
  - `CODEX_AUTH_M365_BETA_DATABASE_URL`
  - `CODEX_AUTH_M365_BETA_CREDENTIAL_KEY`
- A successful HTTP response is not permission to expose its credential payload.

## 3. Hosted Microsoft authorization

- Automatic browser authorization must use an operator-owned Entra web application and the exact callback:
  - `https://codex-auth-beta.onrender.com/dashboard/oauth/callback`
- Required beta configuration:
  - `CODEX_AUTH_M365_BETA_OAUTH_CLIENT_ID`
  - `CODEX_AUTH_M365_BETA_OAUTH_CLIENT_SECRET`
  - `CODEX_AUTH_M365_BETA_OAUTH_REDIRECT_URI`
- Use authorization code + PKCE, single-use state, browser-session binding, and a ten-minute transaction expiry.
- Do not attempt to intercept Microsoft first-party browser callbacks or storage.
- Do not use device-code flow with the captured first-party M365 client; Microsoft rejected it because that client is not enabled as a mobile client.
- Sydney generation and Microsoft Graph are separate authorization resources. Report their readiness separately.
- Hosted OAuth is `blocked_by_upstream` or `unconfigured` until the complete Sydney consent, callback, token validation, zero-cookie generation, and refresh path succeeds.
- Keep OAuth JSON import available only as Advanced recovery.

## 4. Public compatibility API

Base URL:

```text
https://codex-auth-beta.onrender.com/v1
```

OpenAI-compatible routes:

- `GET /v1/models`
- `GET /v1/models/{model_id}`
- `POST /v1/chat/completions`
- `POST /v1/responses`

Anthropic-compatible routes:

- `POST /v1/messages`
- `POST /v1/messages/count_tokens` currently returns intentional HTTP `501`.

Authentication:

```http
Authorization: Bearer <CODEX_AUTH_M365_BETA_API_KEY>
Content-Type: application/json
```

- Never place the API key in a URL or committed browser JavaScript.
- A dashboard that calls `/v1/*` must use a protected server-side proxy or a transient in-memory key field.
- HTTP `401` means the route is reachable but the API key is absent or wrong.

## 5. Supported request behavior

- Text generation and streaming are supported only where verified by live M365 evidence.
- Reasoning output is an unsigned provider summary, not raw chain-of-thought and not signed thinking.
- System instructions and multi-turn history are compiled text, not a native structured M365 contract.
- Do not silently accept unsupported parameters.
- Until a proven mapping exists, reject:
  - `max_tokens`
  - `temperature`
  - `top_p`
  - `top_k`
  - stop sequences
  - caller tools and tool choice
  - Anthropic thinking controls
- Image or file support is advertised only after a complete live upload, annotation binding, extraction, and marker-readback test.
- Generated images are returned as bytes only after retrieval and MIME validation. Otherwise return safe `unretrievable` metadata.

## 6. Conversation continuity

- A new chat should use a new `X-Codex-Conversation-ID` or omit an explicit ID when the first message is unique.
- A continuation reuses the same conversation ID and appends new messages to the history.
- Do not submit an identical completed payload again under the same conversation identity.
- Concurrent identical requests return a conflict instead of replaying upstream generation.
- The proxy never replays a request after prompt submission.
- Edited or branched history must fork to a new upstream conversation.
- `conversation_request_already_completed` is a continuity/idempotency conflict, not an authentication failure.
- A model change keeps the caller-facing proxy conversation ID but must create a
  fresh upstream M365 conversation ID, matching M365 Web behavior. Replay only
  bounded recent context into the new upstream chat and report
  `upstream_continuity: model_switched`.

## 7. Truthful capability states

Use only these evidence states:

- `verified_live`
- `verified_mock_only`
- `implemented_unverified`
- `blocked_by_upstream`
- `unsupported`
- `out_of_scope`

- Mock tests cannot promote a capability to `verified_live`.
- A local pass does not prove the deployed Render commit.
- Every hosted claim must identify the tested commit and evidence ID.
- Do not claim OAuth, refresh durability, Graph files, generated-image retrieval, conversation continuity, or personalization as live without a matching-commit campaign.

## 8. Dashboard rules

- Preserve the six views: Overview, Account, Models, Capabilities, Verification, and Live Logs.
- Active navigation must remain readable in light and dark themes.
- SVG icons use `currentColor` and keyboard focus must be visible.
- Mobile controls use at least 44-pixel touch targets and must not overflow at 320–430 pixel widths.
- The primary Account action is `Continue with Microsoft` only when hosted OAuth is configured.
- Manual JSON import belongs under `Advanced recovery`.
- Clear pasted credential text and file inputs immediately after submission.
- Dashboard status must distinguish generation, refresh, Graph/file access, and persistence.
- Live Logs never show prompts, responses, credentials, identities, authorization headers, or protected URLs.

## 9. Testing standard

Before reporting completion:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
pytest -q
ruff check beta src tests
git diff --check
```

- Explicitly set this checkout's `src` on `PYTHONPATH`; otherwise Windows may import another installed or OneDrive checkout.
- Run focused tests first, then the complete suite.
- Validate Python compilation and dashboard JavaScript syntax after dashboard changes.
- Scan changed files and generated proof artifacts for secrets.
- Intentional `400`, `401`, `404`, and `501` outcomes are contract checks, not test failures.
- Never describe a skipped live test as passed.

## 10. Deployment and live proof

- Verify the deployed `/health` commit before and after a campaign.
- The production service remains untouched during beta work.
- Do not deploy when required OAuth, encryption, database, API, or admin secrets are missing.
- After credential rotation, restart the beta service and prove that encrypted persistence reloads the replacement credential.
- A live generation proof must use a harmless fixed marker and report only status, latency, event types, byte counts, marker presence, cookie count, and safe failure phase.
- Never store raw prompts or response text in proof bundles.
- Stop after uncertain credential mutation or partial remote success; inspect state before any retry.

## 11. Definition of done

Work is complete only when:

- the requested code is implemented in the correct beta or production boundary;
- tests and lint pass from this checkout;
- no secret appears in source, output, logs, or documentation;
- capability claims match evidence;
- deployed behavior is tested when deployment was requested;
- known blockers and untested live paths are stated plainly.
