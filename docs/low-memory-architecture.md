# Low-memory architecture

The runtime does not launch Chromium. Each configured provider owns at most one
lazy curl-cffi session that holds its cookie or token state and reuses
connections. ChatGPT uses HTTP/SSE; Microsoft 365 opens one WebSocket only
during a generation and closes it afterward. Scaffolded or disabled providers
allocate no network session.

```text
HTTP request
    |
    v
API-key middleware
    |
    v
Provider registry (explicit provider, namespaced model, or default)
    |
    v
Provider-local asyncio admission lock
    |
    +--> session validation / bounded cookie-token refresh
    +--> Sentinel requirements and local proof-of-work
    +--> optional file registration and direct blob upload
    +--> isolated upstream conversation creation
    |
    v
SSE parser + canonical conversation reconciliation
    |
    v
OpenAI-compatible response
```

## Algorithm

1. Reject unauthenticated proxy requests before upstream work.
2. Reject oversized bodies, excessive attachments, private-network URLs, and
   unsupported function-tool requests before expensive upstream work.
3. Convert the caller's message history into a stateless transcript so one API
   client cannot inherit another client's ChatGPT conversation.
4. Select one provider without initializing unrelated providers.
5. Admit one generation at a time per mutable authenticated upstream session.
6. Reuse one TLS session and one in-memory credential jar per active provider.
7. For ChatGPT, on an authenticated upstream 401, exchange the existing cookie session for
   a genuinely new access token and retry once. If the exchange is blocked or
   returns the same rejected token, retry without the bearer and let the
   authenticated cookie session authorize the backend request.
8. For Microsoft 365, validate cookies against the chat shell, then require a
   separately captured short-lived bearer before opening its SignalR chat hub.
9. Compute Sentinel proof locally with a bounded loop.
10. Upload image/document bytes directly to the authenticated ChatGPT file
   service when attachments are present.
11. Use the regular conversation endpoint to create an isolated conversation,
   attaching multimodal pointers or enabling the web tool when requested.
12. Parse assistant text and conversation identifiers from SSE or SignalR.
13. Fetch the canonical completed ChatGPT assistant message from the registered
   conversation before replying. This reconciles tool/citation event shapes that
   cannot be reconstructed reliably from incremental patches alone.
14. Put chunks onto an async queue so the synchronous TLS client does not block
   the FastAPI event loop.
15. Store only bounded, sanitized trace summaries; never retain base64
   attachment bodies or public file URLs in dashboard logs.
16. Return `Cache-Control: no-store` and retain no prompt history in the proxy.

Memory usage is bounded by the Python process, cookie/model metadata, the SSE
response buffer maintained by the HTTP library, and short response strings.
There is no browser renderer, JavaScript heap, page DOM, or browser cache.

## Trade-offs

- Throughput is intentionally one generation per instance.
- Every API request starts an isolated upstream conversation. Message history
  supplied by the caller is serialized into the new request.
- Uploads are capped at 20 MB each, requests at 30 MB total, and attachment
  count at four. Files are processed serially to keep peak memory predictable.
- OpenAI streaming frames are supported, but output is buffered until canonical
  reconciliation, favoring complete responses over early TTFT.
- Generic OpenAI/Ollama function tools, Canvas, and image-generation responses
  are not implemented and fail explicitly instead of being silently ignored.
- ChatGPT's internal protocol and Sentinel format are undocumented and may
  require updates when the web client changes.
