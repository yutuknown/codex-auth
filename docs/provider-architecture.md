# Provider architecture

## Audit summary

The original runtime was reliable for one ChatGPT session but its API routes,
dashboard, lifecycle, model IDs, errors, and cookie update flow all imported one
global OpenAI-web provider. Adding another provider by copying that module would
duplicate protocol translation and create ambiguous model and credential state.

The first multi-provider foundation introduces:

- a provider contract with provider identity and implemented capabilities;
- a lazy registry that owns provider selection and lifecycle;
- namespaced model routing (`provider-id:model-id`);
- an optional `provider` request field for clients that cannot use namespaced
  model IDs;
- shared provider error types suitable for stable HTTP mapping;
- an HTTP-only Microsoft 365 Copilot Chat adapter;
- dashboard-visible provider/runtime status without exposing credentials.

Unqualified model IDs remain aliases for the configured default provider, which
keeps existing clients compatible.

## Runtime flow

```text
OpenAI or Ollama-compatible request
    |
    v
request validation and bounded trace capture
    |
    v
provider registry
    +-- explicit `provider`
    +-- `provider-id:model-id`
    +-- configured default provider
    |
    v
lazy provider instance
    |
    +-- provider-local admission control
    +-- provider-local auth/session refresh
    +-- provider-local file and search translation
    +-- provider-local SSE/event normalization
    |
    v
protocol-neutral text stream
    |
    v
OpenAI/Ollama response encoder
```

Provider sessions are not shared. Each enabled adapter owns its own locks,
cookies or tokens, model cache, conversation state, and cleanup. Deferred
providers are not registered and therefore consume no runtime memory.

## Provider matrix

| Provider ID | State | Recommended authentication | Notes |
| --- | --- | --- | --- |
| `openai-web` | Implemented | ChatGPT Netscape cookies | Existing HTTP-only adapter |
| `m365-copilot` | Implemented for text/search | Web cookies plus short-lived bearer | Direct SignalR web transport; no browser process |
| `gemini-web` | Deferred, not registered | Captured web session | No Gemini runtime is shipped yet |

The implemented and target capability sets are intentionally separate. The API
must never advertise a capability merely because the upstream product UI has
it. A capability becomes implemented only after an adapter test proves the
complete request and response path.

## Microsoft 365 implementation and durability

The current `m365-copilot` adapter validates a Netscape cookie export against
the authenticated Microsoft 365 chat shell, extracts
`clientPreferences.modelSelectorMetadata.availableModelSelectionOptions`, and
then uses the same SignalR WebSocket protocol as that shell. Known and future
GPT menu IDs are converted to stable namespaced API slugs and mapped back to
the exact upstream `tone` for generation. The catalog is cached for 15 minutes;
refresh failure uses the last known catalog rather than silently inventing new
models. The adapter provides buffered text streaming and web search. It
deliberately advertises file input, image input, and generic function tools as
unavailable because their complete protocol paths have not yet been
implemented and tested.

Catalog surfaces:

- `GET /v1/models` lists every configured provider with namespaced IDs.
- `GET /v1/models?refresh=true` forces upstream catalog refresh.
- `GET /api/models_list` feeds the combined dashboard catalog.
- `GET /api/providers/m365-copilot/models?refresh=true` returns only Microsoft
  365 models and catalog status.

The cookie jar does not contain the OAuth refresh token used by the Microsoft
web application. Generation therefore also needs an access token plus routing
identity in `.codex/m365-auth.json` or `CODEX_AUTH_M365_AUTH_JSON`. A captured
refresh exchange in `.codex/m365-oauth.json` or
`CODEX_AUTH_M365_OAUTH_JSON` lets the adapter renew the Substrate access token
and atomically persist Microsoft's rotated refresh token. Valid cookies without
a bearer remain useful for web-session validation but produce
`generation_ready: false`.

### Private web attachment endpoint audit

The consumer M365 web client uses several distinct resources for one local-file
turn. They are not interchangeable:

| Stage | Resource | Proven condition |
| --- | --- | --- |
| Destination discovery | `GET /v1.0/me/drive/special/copilotuploads` | Graph-scoped bearer; returned the personal `Microsoft Copilot Chat Files` folder |
| Upload | Graph `createUploadSession` followed by the returned upload URL | Completed with HTTP 201; the client requests preprocessing on the upload PUT |
| Extraction warmup | `GET /v1.0/me/drive/items/{id}/content?format=extractedtextandmetadatav1` | Graph-scoped bearer plus `Prefer: apiversion=2.1`; returned extracted text |
| Search warmup | `POST https://substrate.office.com/search/api/v1/unfurl?domain=File` | Separate Substrate search-audience bearer; returned HTTP 200 |
| Chat | SignalR `chat` plus `ContentAttachment` warmup | Sydney-audience bearer and synthetic personal-drive `SPO_<driveId>_<itemId>` annotation |

The OAuth refresh token can mint the Graph, Substrate search, and Sydney
audiences, but each access token is audience-specific. Microsoft rotates the
refresh token at every exchange, so refresh operations must be serialized and
the replacement refresh token must be persisted atomically.

The first four stages above are live-proven. The final chat binding is not: the
service accepted the turn but reported that no file was attached even after the
current web client's annotation shape, synthetic SPO identifier,
`ContentAttachment` warmup, extraction, and unfurl steps were reproduced.
Therefore `file_uploads` remains false and the provider still rejects file
inputs. A sanitized successful browser WebSocket frame is required to identify
the remaining private protocol field before this capability can be implemented
safely.

Microsoft now documents a Microsoft 365 Copilot Chat API in Microsoft Graph
beta. It supports synchronous and SSE conversations, enterprise/web grounding,
and selected OneDrive or SharePoint file context. It requires delegated work or
school permissions and a Microsoft 365 Copilot add-on license; personal
Microsoft accounts and application permissions are not supported.

The durable implementation path is therefore:

1. Add an Entra delegated OAuth credential store and refresh flow.
2. Implement conversation creation and sync/SSE chat through Microsoft Graph.
3. Map only documented file references and text output.
4. Surface permission/license failures as provider authentication errors.
5. Keep the current web transport isolated as a compatibility adapter.

Reference:
<https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/chat/overview>

## Gemini decision

Google documents the Gemini Developer API, including streaming, model listing,
multimodal input, file upload, tools, and an OpenAI-compatible endpoint. That API
uses a Gemini API key or OAuth and is operationally more stable than reverse
engineering `gemini.google.com`.

The recommended split is:

- `gemini-api`: documented API-key/OAuth adapter, implemented first;
- `gemini-web`: optional cookie-session adapter, isolated because its endpoints
  are private and may change without notice.

Reference:
<https://ai.google.dev/gemini-api/docs/openai>

## Next implementation phases

### Phase 1: finish the shared core

- Move request/content normalization out of `routes_openai.py`.
- Move trace capture out of API application setup.
- Define provider-neutral attachment and generation request objects.
- Add per-provider account, credential update, health, and model schemas.
- Split the monolithic dashboard into static CSS/JS and provider components.

### Phase 2: documented adapters

- Implement `gemini-api` with direct REST calls and no vendor SDK.
- Add delegated Entra OAuth and a Microsoft Graph adapter for durable Microsoft
  365 authentication.
- Add contract tests using recorded, redacted fixtures.
- Add provider-specific retry policies; never reuse one provider's retry rules.

### Phase 3: optional web adapters

- Capture one successful text, streaming, image, file, search, model-list, and
  account request for each upstream web app.
- Redact all cookie values, authorization headers, account IDs, signed URLs,
  request IDs, and tenant IDs before storing fixtures.
- Implement feature flags one at a time and advertise each only after a live
  test and a deterministic fixture test pass.

## Remaining audit risks

1. `openai/provider.py` still combines authentication, transport, uploads,
   model/account discovery, conversation state, and SSE parsing.
2. `routes_openai.py` still combines compatibility schemas, normalization,
   tracing, usage accounting, streaming, and HTTP error encoding.
3. The dashboard remains a large single HTML/CSS/JavaScript file.
4. Usage pricing is model-name based and is not provider-aware.
5. The CLI authentication command is OpenAI-specific.
6. The public raw `/backend-api/*` pass-through is ChatGPT-specific and should
   eventually move below `/providers/openai-web/backend-api/*`.
7. Tests rely on the global OpenAI compatibility alias; new tests should inject
   a registry/provider explicitly.
