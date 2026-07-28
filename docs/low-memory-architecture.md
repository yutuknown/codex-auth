# Low-memory architecture

The Render Free runtime provides approximately 512 MB of RAM. Codex-Auth uses
the following constrained pipeline when `CODEX_AUTH_LOW_MEMORY=true`:

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
One Chromium process / one renderer / one page
    |
    +--> Abort images, fonts, media, stylesheets, service workers, and telemetry
    |
    v
DOM text-delta extractor
    |
    v
OpenAI-compatible response
```

## Algorithm

1. Authenticate the request before it enters the browser queue.
2. Admit only one generation at a time with the engine lock.
3. Reuse one browser context and page to avoid duplicate Chromium processes.
4. Limit Chromium to one renderer and a 128 MB JavaScript heap.
5. Abort resources that do not contribute to prompt submission or text output.
6. Extract only the newest assistant message and yield text deltas.
7. Return API responses with `Cache-Control: no-store`.
8. Let Render restart the single instance if Chromium still exceeds the hard
   container memory limit.

This mode trades throughput and some UI resilience for lower peak memory. A
2 GB Render Standard instance remains the recommended production target.
