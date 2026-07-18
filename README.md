# Codex-Auth

Codex-Auth is a professional-grade Python package that provides an OpenAI-compatible API proxy backed by ChatGPT. By utilizing Stealth Playwright, this tool captures your native ChatGPT session and exposes an extremely stable API layer you can connect to AI tools like Cursor, OpenRouter, ClassSift, and more.

## Features

- 🎭 **Stealth Browsing**: Leverages `playwright-stealth` to bypass automated bot detection.
- 🔄 **OpenAI Compatible**: Fully supports `/v1/chat/completions` and `/v1/models`.
- 🖼️ **Vision & Multimodal**: Dynamically scrapes ChatGPT's internal endpoints to accurately report model vision capabilities.
- ⚡ **Asynchronous**: Built on FastAPI for blazing-fast, non-blocking proxying.
- 📦 **Modern CLI**: Includes a robust `codex-auth` CLI tool for setup and proxy execution.

## Installation

You can install this package in editable mode if you plan to develop:

```bash
pip install -e .
playwright install chromium
```

## Quick Start

### 1. Authenticate

Before running the proxy, you need to capture a valid ChatGPT session token.

```bash
codex-auth auth
```
This command will launch a headless browser window where you can log in to your account. The tokens will be saved automatically to a local `.codex` directory.

### 2. Start the Proxy Server

Once authenticated, start the API server:

```bash
codex-auth start --port 8000
```
This will launch the proxy on `http://127.0.0.1:8000`.

## Connecting Tools

In your preferred AI IDE or routing tool, add a new OpenAI-compatible provider:
- **Base URL**: `http://127.0.0.1:8000/v1`
- **API Key**: `sk-codex-dummy` (Any string works)

The proxy will automatically intercept and route requests directly through your active ChatGPT session.

## Architecture & Contributions

This project is built using:
- **FastAPI** for API routing.
- **Playwright** for browser automation.
- **Typer & Rich** for a beautiful CLI experience.

Pull requests and issues are welcome! See `.github/workflows` for CI requirements.

## License
MIT License
