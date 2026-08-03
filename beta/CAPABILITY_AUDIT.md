# Microsoft 365 bearer beta capability audit

Audit date: 2026-08-03

All live probes used only `beta/ms365-auth.json`. They created zero-cookie
sessions and did not read `.codex`, browser profiles, or production
credentials. Protected URLs, tokens, identities, headers, prompts, and response
text are not stored in this report.

| Capability | Result | Observed protocol evidence |
|---|---|---|
| Bearer-only generation | Passed | SignalR handshake, update frames, completion frame, zero cookies |
| Text updates | Passed | Multiple bot text snapshots arrive before completion |
| Auto | Passed | Completed real text, search, code, and image prompts |
| Quick Response | Passed | 54 frames and 6 bot text snapshots in the representative probe |
| Think Deeper | Passed | 80 frames and 25 bot text snapshots in the representative probe |
| GPT 5.5 Quick Response | Passed | Completed in 4.6 seconds with zero cookies |
| GPT 5.5 Think Deeper | Passed | `Progress` messages and `addToChainOfThought: true` observed |
| Search | Passed | `searchQueries`, `references`, adaptive cards, and `ReferencesListComplete` observed |
| Citations | Passed | Reference objects arrived in live search frames |
| Code interpreter | Passed | `GeneratedCode`, progress, plugin metadata, and result text observed |
| Image generation | Passed | Image progress, file token, polling metadata, and adaptive-card image reference observed |
| Image retrieval | Passed | Zero-cookie fetch returned HTTP 200, `image/png`, 426,213 bytes |
| Generated-image API bytes (2026-08-03) | Passed live | The beta maps generated-card image URLs and live SignalR `contentGenerationProgressList.ImageReferenceUrls` into a bounded zero-cookie resolver. A live run completed with six verified embedded image artifacts and no retrieval phase failures; bytes remain process-local and are emitted only as base64 blocks. |
| Suggestions/cards | Passed | Suggested responses and adaptive-card structures observed |
| OAuth refresh | Passed live | The exact broker query/form schema with Sydney scope rotated the access and refresh pair; the new bearer then completed zero-cookie generation |
| OAuth refresh replay gate | Implemented | Refresh remains local and returns a configuration error unless the ignored credential record explicitly marks an exact successful DevTools form payload as captured |
| Access/refresh independence | Battle-tested and fixed | A fresh chat-hub bearer restored zero-cookie generation while the saved refresh token remained rejected; `generation_ready` no longer depends on refresh health |
| Refresh failure persistence | Battle-tested and fixed | Safe outcome, time, error code, and recovery action persist in the ignored credential record; token values are never returned |
| Lane-aware streaming | Passed | A live reasoning probe produced separate text/reasoning lanes and 14 append operations with no replacement leakage |
| Anthropic compatibility | Passed | A live `gpt-5.5-think-deeper` probe produced 1 `thinking_delta`, 57 `text_delta` events, proper content-block boundaries, and no signature |
| OpenAI compatibility | Passed | The same live probe produced 1 `reasoning_content` delta and 57 regular `content` deltas |
| Model ID to tone routing | Passed | A live namespaced `gpt-5.5-think-deeper` request resolved to `Gpt_5_5_Reasoning` and completed with zero cookies |
| Long forced reasoning | Passed | A 1,130-character optimization prompt produced 2 reasoning updates, 126 text deltas, append-only streaming, and a correct lower-bound proof |
| Bearer model listing | Not found | Bounded research found no M365 equivalent to Antigravity `fetchAvailableModels`; `ValidateGpt` exists but returned opaque 403 |
| OneDrive upload stages | Implemented, Graph credential required | A zero-cookie Graph pipeline covers upload-session creation through the `copilotuploads` special folder, upload, and extraction warmup |
| Graph file input (2026-08-03) | Passed live | Browser-observed consumer Graph headers (`KnownConsumerLocation`, origin/referer, Graph JS SDK version, and a client request ID) produced a zero-cookie upload, extraction, `File` annotation, and exact-marker response |
| Native browser file-to-chat binding | Passed | An authenticated browser upload was read back exactly by M365 Copilot; this proves the upstream product capability only |
| Beta Graph file-to-chat binding | Unverified upstream | The zero-cookie adapter can create the OneDrive upload, request extraction, and construct a `File` annotation, but its generated attachment has not yet been read back by a matching beta generation. It is not advertised as supported. |
| Image upload | Passed live with zero cookies | `POST /m365Copilot/UploadFile` accepted the Sydney bearer, `FileBase64` data URL, captured account scenario, image flight, and three image option sets; no Graph bearer was required |
| Image-to-chat binding | Passed live | The returned `docId` was sent as an `ImageFile` annotation with file metadata and the upload conversation ID; a separate real screenshot prompt correctly identified the DevTools Payload tab and visible form fields with `cookie_count=0` |
| Remote image URL | Passed live | A public HTTPS image was retrieved with redirect, public-DNS, empty-cookie, and 20 MB guards, staged through Substrate, and analyzed through SignalR |
| OpenAI/Anthropic tool-call conversion | Not implemented | M365 plugin/code events are mapped, but no public tool-call contract is exposed yet |
| Public reasoning stream | Partially proven | Reasoning progress flags exist; no Google-style `thoughtSignature` equivalent was observed |
| Structured request IR | Passed with textual transport | System content, ordered roles, turns, and attachment names are preserved and compiled into a response-preferences and conversation transcript |
| Native system instruction | No native field; fidelity passed | The upstream rejected a `System:` label, but the response-preferences envelope followed an exact-marker instruction live; the beta does not claim a native system-role field |
| Usage accounting | Local estimate only | Anthropic and OpenAI responses now contain nonzero lexical estimates labelled `local_lexical_estimate` and `upstream_reported: false`; no upstream token usage is claimed |
| Pre-submit reliability | Implemented | A 401 refreshes once before submission, transient connect failures back off, and a submitted generation is never replayed |
| Persistent telemetry | Implemented | Bounded redacted JSONL powers success rate, latency percentiles, traffic totals, failure phases, recent logs, and live SSE; prompts, responses, credentials, identities, headers, and URLs are excluded |
| Expiry derivation | Battle-tested and fixed | When `captured_at` is absent, expiry now derives from the ID token `iat`; the prior process-start fallback could make an expired token look perpetually active |
| HTTP upstream error boundary | Battle-tested and fixed | A live image-input 401 exposed an uncaught preparation error; attachment upstream failures now return a safe HTTP 502 |
| Local campaign (2026-08-03) | Passed | 23 redacted zero-cookie checks passed: five model routes, reasoning, search/citations, code, generated-image events, Anthropic/OpenAI buffered and streaming APIs, system/history compilation, image input, API-key guard, and telemetry. Model/quota/signature probes remain blocked rather than guessed. |
| Thinking UI taxonomy | Mapped, presentation only | Microsoft CDN chunks identify `chain_of_thought`, search, code, terminal, generated-image, generated-code, and result-block layouts plus `copilotMessageType: thinking`; a separate teaching-moment chunk only toggles UI state for ten seconds |
| Sydney reasoning timing | Confirmed client telemetry | The Sydney performance helper measures first/last chain-of-thought, first task reasoning, first task step, and first task creation independently from first/last main response chunks |

## Normalized event map

The beta inspector now classifies only fields observed in live frames:

- `text_snapshot`
- `progress`
- `reasoning_progress`
- `reasoning_ui_item`
- `search_query`
- `citation`
- `references_complete`
- `generated_code`
- `image_progress`
- `image`
- `adaptive_card`
- `plugin`
- `suggestions`
- `completion`
- `keepalive`

M365 sends multiple message lanes and replacement snapshots, so text lengths are
not globally monotonic. `M365StreamAssembler` now tracks
`messageId`/`responseIdentifier` lanes and derives append, replace, or regression
operations per lane. The representative live reasoning probe produced only
append operations; replacement handling remains covered by transport tests.

Raw reasoning text is not exposed. Only provider-authored progress messages
explicitly marked for the chain-of-thought UI are eligible for a future
reasoning-summary event. The compatibility API now maps that event to Anthropic
`thinking_delta` and OpenAI `reasoning_content`, but intentionally emits no
`signature_delta` or `thoughtSignature`.

The CDN UI taxonomy is recorded separately as `reasoning_ui_item`. It is useful
for diagnostics but does not by itself prove a raw SignalR reasoning lane. The
`DeepThinking` teaching-moment component only sets a client-side trigger for
ten seconds, so it is not treated as model availability, reasoning-token
metadata, or a provider-issued signature.

The Sydney performance helper confirms that the client treats chain-of-thought
and task execution as distinct timed phases. These timings strengthen the
separate-lane architecture, but they remain browser performance marks rather
than provider-issued reasoning tokens.

## Compatibility stream proof

A zero-cookie live probe through `beta/m365_compat.py` using
`gpt-5.5-think-deeper` produced:

- 1 normalized `reasoning_summary_delta`;
- 57 normalized `text_delta` events;
- Anthropic `message_start`, two correctly bounded content blocks,
  `message_delta`, and `message_stop`;
- 1 Anthropic `thinking_delta` and 57 Anthropic `text_delta` events;
- 1 OpenAI `reasoning_content` and 57 OpenAI `content` deltas;
- no signature field of any kind.

Only event names, counts, timings, and lengths were retained by the proof.
Prompt and response text, credentials, identity, headers, and protected URLs
were not recorded.

## Promotion blockers

1. Obtain verified generated-image bytes before exposing an image output block;
   otherwise keep `availability: unretrievable` metadata only.
2. Re-test image input with representative JPEG, PNG, GIF, and WebP files
   before promotion. The one-pixel PNG live gate now passes, but size and
   sanitizer behavior across every accepted format is not yet battle-tested.
3. Promote the tested reasoning-summary contract into the production provider
   interface only after deciding whether the public API should expose both
   Anthropic and OpenAI compatibility or one canonical format.
4. Preserve the successful broker refresh schema and atomic rotation tests.
