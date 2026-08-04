# Codex Auth Setup and Usage Guide

This guide explains how to configure and use the Microsoft 365 beta without exposing credentials or running into the most common client errors.

## 1. Know which service you are using

Microsoft 365 beta dashboard:

```text
https://codex-auth-beta.onrender.com/dashboard
```

Compatibility API base URL:

```text
https://codex-auth-beta.onrender.com/v1
```

Do not use the production `codex-auth` hostname when testing beta features.

## 2. Required secrets

Keep secrets in ignored `beta/.env` for local use and in Render environment variables for deployment.

Minimum API configuration:

```dotenv
CODEX_AUTH_M365_BETA_API_KEY=replace-with-a-long-random-api-key
CODEX_AUTH_M365_BETA_ADMIN_KEY=replace-with-a-different-admin-key
CODEX_AUTH_M365_BETA_DASHBOARD_SESSION_KEY=replace-with-a-random-session-key
CODEX_AUTH_M365_BETA_CONVERSATION_HMAC_KEY=replace-with-a-random-conversation-key
```

Never commit the real `.env` file. Never place an API key in a URL.

The API key and admin key have different purposes:

| Variable | Used for |
|---|---|
| `CODEX_AUTH_M365_BETA_API_KEY` | `/v1/*` OpenAI and Anthropic compatibility routes |
| `CODEX_AUTH_M365_BETA_ADMIN_KEY` | Dashboard login and credential administration |

## 3. Optional durable hosted storage

Render's local filesystem is ephemeral. To preserve refreshed credentials across restarts, configure:

```dotenv
CODEX_AUTH_M365_BETA_DATABASE_URL=postgresql://...
CODEX_AUTH_M365_BETA_CREDENTIAL_KEY=replace-with-a-high-entropy-encryption-key
```

Do not call the deployment restart-safe until `/health` reports:

```json
{
  "credential_persistence": {
    "source": "encrypted_external_postgres",
    "restart_durable": true
  }
}
```

## 4. Optional hosted Microsoft sign-in

Automatic browser return requires an operator-owned Microsoft Entra web application. Register this exact redirect URI:

```text
https://codex-auth-beta.onrender.com/dashboard/oauth/callback
```

Then configure:

```dotenv
CODEX_AUTH_M365_BETA_OAUTH_CLIENT_ID=your-entra-application-client-id
CODEX_AUTH_M365_BETA_OAUTH_CLIENT_SECRET=your-entra-client-secret
CODEX_AUTH_M365_BETA_OAUTH_TENANT=common
CODEX_AUTH_M365_BETA_OAUTH_REDIRECT_URI=https://codex-auth-beta.onrender.com/dashboard/oauth/callback
CODEX_AUTH_M365_BETA_OAUTH_SYDNEY_SCOPE=https://substrate.office.com/sydney/v2/.default openid profile offline_access
```

In the dashboard:

1. Open **Account**.
2. Select **Continue with Microsoft**.
3. Complete Microsoft sign-in.
4. Microsoft returns the browser to the beta callback.
5. Check generation and refresh readiness separately.

Microsoft may refuse the private Sydney scope for third-party Entra applications. If that happens, the dashboard reports `blocked_by_upstream`; it must not claim automatic sign-in succeeded.

### Advanced recovery import

If hosted OAuth is unavailable, open **Advanced recovery** and import a fresh OAuth JSON response. The dashboard clears pasted text after submission and never returns token values.

Do not paste credentials into chat, Git, screenshots, issue reports, or public logs.

## 5. API endpoints

### Model discovery

```http
GET /v1/models
Authorization: Bearer <API_KEY>
```

### OpenAI-compatible Chat Completions

```http
POST /v1/chat/completions
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

### OpenAI Responses

```http
POST /v1/responses
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

### Anthropic-compatible Messages

```http
POST /v1/messages
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

`POST /v1/messages/count_tokens` intentionally returns HTTP `501` until authoritative upstream token counting is available.

## 6. Test the API before configuring an app

### PowerShell model check

Load the API key without printing it:

```powershell
$betaEnvPath = ".\beta\.env"
$apiKeyLine = Get-Content -LiteralPath $betaEnvPath |
  Where-Object { $_ -match '^CODEX_AUTH_M365_BETA_API_KEY=' } |
  Select-Object -First 1
$betaApiKey = $apiKeyLine.Substring($apiKeyLine.IndexOf('=') + 1).Trim()
$headers = @{ Authorization = "Bearer $betaApiKey" }
Invoke-RestMethod `
  -Uri "https://codex-auth-beta.onrender.com/v1/models" `
  -Headers $headers
```

Do not run commands that print `$betaApiKey`.

### OpenAI-compatible request

```powershell
$body = @{
  model = "gpt-5.5-quick-response"
  messages = @(
    @{ role = "user"; content = "Reply exactly: BETA_OK" }
  )
  stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "https://codex-auth-beta.onrender.com/v1/chat/completions" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

### Anthropic-compatible request

```powershell
$body = @{
  model = "gpt-5.5-quick-response"
  messages = @(
    @{ role = "user"; content = "Reply exactly: BETA_OK" }
  )
  stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "https://codex-auth-beta.onrender.com/v1/messages" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

## 7. Client configuration

Use these values in an OpenAI-compatible client:

```text
Base URL: https://codex-auth-beta.onrender.com/v1
API key: value of CODEX_AUTH_M365_BETA_API_KEY
Model: an ID returned by GET /v1/models
```

For an Anthropic-compatible client:

```text
Base URL: https://codex-auth-beta.onrender.com/v1
Messages endpoint: /v1/messages
API key: value of CODEX_AUTH_M365_BETA_API_KEY
```

If the client automatically adds unsupported controls, disable them before sending the request.

Currently rejected until a proven Microsoft 365 mapping exists:

- `max_tokens`
- `temperature`
- `top_p`
- `top_k`
- stop sequences
- caller tools or `tool_choice`
- Anthropic thinking controls

Do not silently remove these parameters inside a shared client without understanding the behavior change.

## 8. Conversation usage

For a new chat, generate a fresh opaque ID:

```http
X-Codex-Conversation-ID: <new-random-uuid>
```

For the next turn:

- reuse the same `X-Codex-Conversation-ID`;
- include the prior message history;
- append the new user message;
- do not resubmit an identical completed payload.

Example first turn:

```json
{
  "model": "gpt-5.5-quick-response",
  "messages": [
    {"role": "user", "content": "My project name is Atlas."}
  ]
}
```

Example continuation using the same conversation ID:

```json
{
  "model": "gpt-5.5-quick-response",
  "messages": [
    {"role": "user", "content": "My project name is Atlas."},
    {"role": "assistant", "content": "Understood."},
    {"role": "user", "content": "What is my project name?"}
  ]
}
```

Disable automatic retries that replay an already submitted generation.

When the user changes models inside the same client chat, keep the same
`X-Codex-Conversation-ID` and send the updated history. The beta keeps that
client chat intact but creates a fresh upstream M365 conversation, matching the
M365 Web behavior. The response metadata reports
`upstream_continuity: model_switched` and `model_switch: true`.

## 9. Common errors

| Error | Meaning | Fix |
|---|---|---|
| `401 Invalid or missing API key` | The endpoint is reachable, but the compatibility API key was not sent or is wrong. | Send `Authorization: Bearer <CODEX_AUTH_M365_BETA_API_KEY>`. Do not use the dashboard admin key. |
| `max_tokens is not mapped to a proven M365 upstream contract` | The client automatically sent an unsupported parameter. | Configure the client to omit `max_tokens`. |
| `conversation_request_already_completed` | An identical completed payload was reused under the same conversation identity. | Start a new conversation ID or append a genuinely new turn. |
| `conversation_request_in_progress` | The same request is already running. | Wait for it to finish; do not submit it again. |
| `blocked_by_upstream` during OAuth | Microsoft refused or did not expose the required Sydney contract to the configured app. | Use Advanced recovery; do not claim hosted OAuth works. |
| `re_import_required` | Access expired and refresh cannot recover it. | Complete a new authorization or import a fresh OAuth response. |
| `refresh_failed` | Microsoft rejected refresh or the captured refresh contract is incomplete. | Inspect the safe failure phase, then reauthorize once. Avoid blind retries. |
| `501` from token counting | Authoritative token counting is not implemented. | Let the client continue without preflight counting if supported. |
| Render works, then fails after restart | Runtime credentials were stored only in memory or ephemeral storage. | Configure encrypted external Postgres and verify `restart_durable: true`. |

## 10. Streaming and reasoning

- Streaming is available through the supported compatibility endpoints.
- M365 reasoning output is an unsigned provider activity summary.
- It is not raw chain-of-thought and does not contain a Microsoft or Google reasoning signature.
- Clients must not label it as signed thinking.

## 11. Images and files

- Do not assume every model accepts every image or document format.
- Only use file types reported as verified by `/v1/capabilities` for the running commit.
- Graph-based documents require a separate valid Graph credential.
- Generated images are trustworthy only when the API returns validated image bytes. `unretrievable` metadata is not an image.

## 12. Before reporting a problem

Record only safe information:

- endpoint path;
- HTTP status;
- model ID;
- whether streaming was enabled;
- safe error message or failure phase;
- deployed commit from `/health`;
- latency and structural event names.

Never include:

- API keys;
- cookies;
- access or refresh tokens;
- authorization headers;
- full protected URLs;
- personal profile, memory, or custom-instruction content.

## 13. Local verification for contributors

From the repository root:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
pytest -q
ruff check beta src tests
git diff --check
```

Explicitly setting `PYTHONPATH` prevents Windows from importing a different installed or OneDrive checkout.

Read [rulebook.md](rulebook.md) before modifying or deploying the project.
