# Low-memory architecture

The runtime does not launch Chromium. One `curl-cffi` session holds the cookie
jar and reuses network connections.

```text
HTTP request
    |
    v
API-key middleware
    |
    v
Single asyncio admission lock
    |
    v
Worker thread (one active upstream request)
    |
    +--> session validation / access-token exchange
    +--> Sentinel requirements and local proof-of-work
    +--> conversation creation or continuation preparation
    |
    v
Incremental SSE parser
    |
    v
OpenAI-compatible response
```

## Algorithm

1. Reject unauthenticated proxy requests before upstream work.
2. Admit one generation at a time so concurrent callers cannot duplicate the
   mutable ChatGPT conversation state.
3. Reuse one TLS session and one in-memory cookie jar.
4. Compute Sentinel proof locally with a bounded loop.
5. Use the regular conversation endpoint to create a registered conversation.
6. Store only the returned conversation ID and latest message ID.
7. For a continuation, call `f/conversation/prepare`, then stream from
   `f/conversation` with its conduit token.
8. Reconstruct only assistant text from initial SSE messages and patch events.
9. Put chunks onto an async queue so the synchronous TLS client does not block
   the FastAPI event loop.
10. Return `Cache-Control: no-store` and retain no prompt history in the proxy.

Memory usage is bounded by the Python process, cookie/model metadata, the SSE
response buffer maintained by the HTTP library, and short response strings.
There is no browser renderer, JavaScript heap, page DOM, or browser cache.

## Trade-offs

- Throughput is intentionally one generation per instance.
- The conversation state is in memory and resets after a restart.
- File upload and web search are not implemented in HTTP-only mode.
- ChatGPT's internal protocol and Sentinel format are undocumented and may
  require updates when the web client changes.
