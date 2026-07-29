<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/logo.svg">
    <img alt="Codex-Auth Logo" src="assets/logo.svg" width="300">
  </picture>

  # Codex-Auth

  **A low-memory, provider-routed compatibility API for authenticated AI sessions.**

  [![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
  [![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com/)
  [![HTTP](https://img.shields.io/badge/HTTP-curl--cffi-24B47E?style=for-the-badge)](https://github.com/lexiforest/curl_cffi)
  [![License](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](#license)
</div>

<br />

Codex-Auth is a Python package that provides an OpenAI-compatible API proxy.
Its shipped providers support authenticated ChatGPT Web and Microsoft 365
Copilot Chat sessions behind one namespaced model catalog. It uses direct HTTP,
SSE, and WebSocket transports, so Chromium and Playwright are not required at
runtime.

> ChatGPT's web endpoints are undocumented and can change without notice. A
> ChatGPT subscription is not an OpenAI API subscription.

## 📑 Table of Contents

- [✨ Features](#-features)
- [🚀 Getting Started](#-getting-started)
- [💻 Usage](#-usage)
- [☁️ Deploy on Render](#️-deploy-on-render)
- [📸 Screenshots](#-screenshots)
- [🔌 Connecting Tools](#-connecting-tools)
- [🏗️ Architecture](#️-architecture)
- [📜 License](#-license)

## ✨ Features

- 🍪 **Cookie Authentication**: Loads a Netscape-format ChatGPT `cookies.txt`.
- 🔄 **OpenAI Compatible**: Supports `/v1/chat/completions` and `/v1/models`.
- 🪶 **Low Memory**: No browser process, renderer, DOM, or JavaScript heap.
- ⚡ **Streaming Core**: Reconstructs assistant text from ChatGPT SSE events.
- 📊 **Dashboard**: Browser login protects runtime logs, usage, and model status.
- 🔐 **Session Refresh**: Validates and activates pasted or uploaded `cookies.txt`
  exports without restarting the service.
- 📦 **CLI Tool**: Includes the `codex-auth` CLI built with Typer and Rich.

HTTP-only mode supports text and canonical streaming without launching
Chromium. Attachment and web-search availability depends on the authenticated
ChatGPT account and upstream permissions. Generic function tools, Canvas, and
image-generation responses are not yet exposed by the proxy.

## 🚀 Getting Started

### Option 1: Python Developers (Recommended)

```bash
pipx install codex-auth-proxy
codex-auth install
```

The install command confirms that no browser download is needed.

### Option 2: Editable Development Install

```bash
git clone https://github.com/yutuknown/codex-auth.git
cd codex-auth
python -m pip install -e .
```

## 💻 Usage

### 1. Authenticate

Export your signed-in `chatgpt.com` cookies in Netscape HTTP Cookie File format
and save them at:

```text
.codex/cookies.txt
```

The `.codex` directory is ignored by Git. Never commit or share this file.
Alternatively, set `CODEX_AUTH_COOKIE_FILE` to a different path.

For Microsoft 365 Copilot, save the Netscape cookie export at
`.codex/m365-cookies.txt` and the current Copilot connection metadata at
`.codex/m365-auth.json`. A captured OAuth refresh exchange can be stored at
`.codex/m365-oauth.json`, then select a namespaced model such as
`m365-copilot:auto` or `m365-copilot:gpt-5.5-think-deeper`. Render deployments
can provide the same values through
`CODEX_AUTH_M365_COOKIES`, `CODEX_AUTH_M365_AUTH_JSON`, and
`CODEX_AUTH_M365_OAUTH_JSON`.

The Account dashboard is provider-first: it exposes separate connection,
model, credential-health, and account views for each registered provider.
Microsoft 365 profile data is optional and read-only. Configure
`CODEX_AUTH_M365_GRAPH_JSON` for a Graph `User.Read` access token and, when a
durable refresh flow is available, `CODEX_AUTH_M365_GRAPH_OAUTH_JSON`. A
missing or expired Graph profile token never prevents Microsoft 365 chat
generation.

The provider discovers the current account-visible Microsoft model selector
from authenticated `GET https://m365.cloud.microsoft/chat` hydration data. The
result is cached for 15 minutes and safely falls back to the last known catalog
if a refresh fails. Use `GET /v1/models?refresh=true` or
`GET /api/providers/m365-copilot/models?refresh=true` to force a refresh.

The Microsoft 365 web cookies validate the signed-in web shell, but they do not
contain the OAuth refresh token used by the site. The bearer in
`m365-auth.json` is short-lived. When `m365-oauth.json` is present, the adapter
uses the captured `grant_type=refresh_token` exchange before expiry, rotates
both returned tokens atomically, and keeps the browser-free runtime renewable.
Without it, the adapter reports `generation_ready: false` after the bearer
expires instead of pretending a cookie-only session can generate.

### 2. Start the Proxy Server

```bash
codex-auth start --port 8000
```

The OpenAI-compatible base URL is `http://127.0.0.1:8000/v1`.

### 3. Test a Completion

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-3","messages":[{"role":"user","content":"Reply with OK"}]}'
```

When `CODEX_AUTH_API_KEY` is configured, also send:

```text
Authorization: Bearer <CODEX_AUTH_API_KEY>
```

### 4. Open the Dashboard

Visit `/login`, enter `CODEX_AUTH_API_KEY`, and the server creates a secure
HttpOnly dashboard session. The raw key is not stored in the browser cookie.

The Account page can replace an expired session by pasting a Netscape cookie
export or selecting `cookies.txt`. The server validates the replacement against
the account endpoints before atomically activating it. Cookie values are not
returned by the API or included in request traces.

## ☁️ Deploy on Render

The included `Dockerfile` has no browser dependencies, and `render.yaml` uses
Render's Free plan.

1. Create a Render Blueprint or Docker web service from this repository.
2. Set `CODEX_AUTH_COOKIES` to the complete Netscape cookie-file contents.
3. If Render cannot perform the cookie-to-token exchange, set
   `CODEX_AUTH_ACCESS_TOKEN` from the same authenticated browser session.
4. Generate a private `CODEX_AUTH_API_KEY`.
5. Replace the cookies and access token when the ChatGPT session expires.

The health check is `GET /healthz`. Secrets must remain in Render environment
variables and must never be committed.

Dashboard cookie replacement takes effect immediately and writes the configured
cookie file. Render's default filesystem is ephemeral, so also update the
`CODEX_AUTH_COOKIES` environment secret—or attach a persistent disk—if the new
cookies must survive a restart or redeploy.

See [`docs/low-memory-architecture.md`](docs/low-memory-architecture.md) for the
request algorithm and memory characteristics.

## 📸 Screenshots

| Authentication Setup | API Server Logs | CLI Chat Interface |
| :---: | :---: | :---: |
| <img src="assets/screenshot-1.png" width="250"> | <img src="assets/screenshot-2.png" width="250"> | <img src="assets/screenshot-3.png" width="250"> |

## 🔌 Connecting Tools

Configure any OpenAI-compatible client with:

- **Base URL**: `http://127.0.0.1:8000/v1` or your Render URL followed by `/v1`
- **API Key**: `CODEX_AUTH_API_KEY`
- **Model**: A slug returned by `GET /v1/models`

## 🏗️ Architecture

```mermaid
graph LR
    A[AI Tool / IDE] -->|OpenAI API Request| B(FastAPI Server)
    B --> R{Provider registry}
    R -->|default or openai-web:model| C[ChatGPT web adapter]
    R -->|m365-copilot:auto| M[Microsoft 365 web adapter]
    C -->|Provider-local admission lock| D[ChatGPT conversation]
    M -->|Provider-local admission lock| H[Copilot SignalR chat hub]
    D --> E[SSE and canonical reconciler]
    H --> A
    E --> A
```

- **FastAPI** handles API routing, API-key checks, and the dashboard.
- **The provider registry** selects an isolated adapter without initializing
  unrelated providers.
- **curl-cffi** maintains the authenticated HTTP cookie session.
- **The response reconciler** combines SSE delivery with the canonical stored
  assistant message for reliable long multimodal and web-search responses.
- **Typer and Rich** power the CLI.

Provider selection is backward compatible: an unqualified model such as `auto`
uses the default provider. Multi-provider clients can use a namespaced model
such as `openai-web:auto` or send the optional `provider` field. Microsoft 365
Copilot text and web-search generation are implemented through its web
SignalR transport; file input is rejected explicitly until its upload protocol
is implemented. Gemini is intentionally not registered in the shipped runtime
yet. See
[the provider architecture and implementation roadmap](docs/provider-architecture.md).

Microsoft 365 model IDs are discovered dynamically and map to the `tone` field
in the SignalR chat invocation. At the time of the latest verified session:

| API model | Captured Microsoft tone |
| --- | --- |
| `m365-copilot:auto` | `Magic` |
| `m365-copilot:quick-response` | `Chat` |
| `m365-copilot:think-deeper` | `Reasoning` |
| `m365-copilot:gpt-5.5-quick-response` | `Gpt_5_5_Chat` |
| `m365-copilot:gpt-5.5-think-deeper` | `Gpt_5_5_Reasoning` |

## 🤝 Contributors

- [@yutuknown](https://github.com/yutuknown) - Creator & Lead Developer
- **Antigravity AI** - AI Pair Programmer

## 📜 License

Distributed under the MIT License. See `LICENSE` for more information.
