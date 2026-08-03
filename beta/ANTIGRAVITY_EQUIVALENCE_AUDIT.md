# Antigravity equivalence map

Audit target: `badrisnarayanan/antigravity-claude-proxy` commit
`055699fcebcac83cea64bf599546a3ce820ebcdb` (version 2.7.7).

This is a protocol-equivalence audit, not a claim that Microsoft 365 and
Google Cloud Code expose the same upstream API. Antigravity uses an HTTP SSE
generation endpoint. The M365 beta uses a SignalR WebSocket and converts
replacement snapshots into append-only deltas.

## Architecture

| Layer | Antigravity | M365 bearer beta | State |
|---|---|---|---|
| Credential source | Google OAuth/local Antigravity DB | Local JSON or environment seed plus an optional persistent rotation state file | Equivalent for one account when persistent state is mounted |
| Refresh lifecycle | OAuth refresh and token cache | Live-proven broker refresh, serialized rotation, safe durability status, and generation with the rotated bearer | Equivalent for one account; process-memory mode is not restart-durable |
| Cookie dependence | None | None; empty cookie jar enforced | Equivalent |
| Model discovery | `v1internal:fetchAvailableModels` | Captured shell catalog or labelled fallback | Partial |
| Model aliases | Mapping before validation | Mapping before validation | Equivalent |
| Generation transport | HTTP POST plus SSE | SignalR WebSocket | Different, functionally equivalent for text |
| Anthropic API | `/v1/messages` | `/v1/messages` | Equivalent for text |
| OpenAI API | Not provided | `/v1/chat/completions` | M365 beta extension |
| Streaming preflight | First event before HTTP 200 | First event before HTTP 200 | Equivalent |
| Reasoning | Signed thinking blocks | Provider reasoning summaries without signatures | Partial |
| Web search | Model dependent | M365 Bing plugin plus observed search/citation frames | Supported |
| Client function tools | Native function-call translation | No proven M365 external-tool protocol | Unavailable |
| Image input | Base64 `inlineData` and URL `fileData` | Live-proven base64 and bounded public-HTTPS retrieval followed by Substrate upload and `ImageFile` binding | Equivalent for base64 and public HTTPS images |
| File input | Base64 `inlineData` and URL `fileData` | Zero-cookie Graph upload, extraction, File annotation, and marker readback; Graph bearer is acquired from the same broker refresh session | Supported with a separate Graph resource permission |
| Images returned from tool results | Converted to deferred `inlineData` | No client tool-result loop | Missing |
| Generated-image response bytes | Base64 only when an image response exists | Bounded zero-cookie retrieval from observed generated-image references | Supported; live retrieval produced verified bytes, never an upstream URL |
| Usage accounting | Upstream usage mapping | Explicit local lexical estimates; no stable M365 upstream fields | Partial |
| Model quota | Available from model API | No confirmed bearer quota endpoint | Unavailable |
| Multi-account routing | Pool, health, cooldown, strategies | Intentionally one personal account | Out of scope |
| API key | Optional proxy key | Optional `CODEX_AUTH_M365_BETA_API_KEY` | Equivalent |
| Structured request IR | Native role/block conversion | Bounded roles/turns/system/developer/tool-result context compiled as response preferences and a conversation transcript | Partial; structured IR is preserved but transport is textual |
| Retry policy | Capacity, endpoint, account, and empty-response retries | Bounded pre-submit 401 refresh and transient backoff; never replays a submitted prompt | Partial |
| Persistent telemetry | Dashboard statistics, live log SSE, and account strategy health | Bounded redacted JSONL, metrics, latency/failure health, and live log SSE | Equivalent for the single-account beta |

## Local beta endpoints

- `GET /v1/logs/stream` - live secret-free operational events over SSE.

- `GET /` — endpoint discovery.
- `GET /health` — credential and catalog lifecycle.
- `GET /account-limits` — one-account health and truthful quota-unavailable state.
- `POST /refresh-token` — serialized OAuth refresh.
- `GET /v1/capabilities` — machine-readable version of this matrix.
- `GET /v1/metrics` — redacted bounded operational summary.
- `GET /v1/logs` — recent secret-free operational events.
- `GET /v1/models` — source-labelled model catalog.
- `GET /v1/models/{model_id}` — canonical/alias resolution proof.
- `POST /v1/messages/count_tokens` — HTTP 501, matching Antigravity 2.7.7.
- `POST /v1/messages` — Anthropic buffered or SSE text/reasoning-summary responses.
- `POST /v1/chat/completions` — OpenAI buffered or SSE text/reasoning responses.

## Compatibility rules

The beta accepts multi-turn text, string or text-block system prompts,
historical reasoning-summary and tool-result blocks, Anthropic base64 image/document/file
blocks, OpenAI base64 data-URI image/file blocks, and public HTTPS attachment
URLs. Documents are staged through Microsoft Graph. Images use the captured
Substrate multipart upload with the Sydney bearer and preserve its conversation
ID for SignalR. Remote retrieval revalidates HTTPS redirects and public DNS,
uses no cookies, and enforces a 20 MB limit. Historical tool results are
compiled as context, while client tool definitions, tool choice, sampling
controls, and stop sequences are rejected rather than silently ignored.

Reasoning progress is exposed as a summary. No `thoughtSignature`,
`signature_delta`, raw chain-of-thought guarantee, quota value, or upstream
token usage is invented. Compatibility usage numbers are explicitly labelled
local lexical estimates.

Microsoft's public CDN UI chunks expose a richer presentation taxonomy for
thinking, search, generated code, terminal actions, and generated images.
Those layouts are tracked as diagnostics only. Unlike Antigravity's signed
thinking blocks, they provide no provider-issued signature or stable reasoning
token contract.

The Sydney performance helper separately measures first/last chain-of-thought,
task reasoning, task steps, and task creation. This validates separate
reasoning/task lanes and latency metrics, but not Antigravity-style signed
thinking content.

## Renewable session proof

On 2026-07-30, the beta refreshed the local M365 credential through
Microsoft's brokered OAuth request with the broker client and redirect fields
present in both the query and form. The request used the Sydney resource scope,
rotated the access and refresh credentials atomically, and retained no cookies.

The newly rotated access bearer was then used for a real SignalR generation:

- Result: passed
- Phase: completed
- Latency: 4,959 ms
- Expected response marker: observed
- Cookie count: 0
- Connect attempts: 1
- Generation ready after refresh: true
- Refresh ready after rotation: true

The earlier `AADSTS70000` result was caused by an incomplete broker refresh
request and the wrong resource scope. It was not evidence that Microsoft
refresh tokens are single-use.

## Promotion gates

The beta is not fully Antigravity-equivalent until all of the following have
real upstream evidence:

1. An account-scoped M365 bearer model-list endpoint or reproducible shell
   catalog capture.
2. A stable external function-tool request and result protocol.
3. Stable token-usage or quota fields.
4. A provider-issued reasoning signature, if Microsoft ever exposes one.
5. Tool-result image blocks and Graph-independent non-image URL staging, if
   they are needed for client parity.

Until then, `/v1/capabilities` is the authoritative feature contract.

## Live compatibility proof

On 2026-07-29, the buffered Anthropic route was called through
`POST /v1/messages` with the explicitly namespaced
`m365-copilot:gpt-5.5-think-deeper` model, a system text block, a reasoning
prompt, and zero cookies.

- Result: passed
- Latency: 8,892 ms
- Content blocks: `thinking`, then `text`
- Reasoning-summary characters: 510
- Answer characters: 472
- Stop reason: `end_turn`
- Cookie count: 0

Only structural counts are retained here; credentials and response content are
not recorded.

## Attachment binding proof

On 2026-07-29, the M365 web client uploaded a text file to the Copilot uploads
special folder and sent a chat invocation with one `File` message annotation.
The annotation contained the expected opaque SPO identity, filename, trusted
OneDrive URL, and file-type metadata. Copilot read the attachment and returned
the fixed verification marker requested by the prompt.

The beta now emits the current client identity format: it retrieves the Graph
item's SharePoint site/web/list identifiers, base64-encodes that triple, and
appends the item identifier. It deliberately does not substitute ``driveId``.
The new binding awaits a fresh live beta run because the current ignored OAuth
refresh bundle is expired. Secret values, protected URLs, account identity,
and raw response content are not retained.
