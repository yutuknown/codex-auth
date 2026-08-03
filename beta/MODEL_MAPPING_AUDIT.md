# M365 model discovery and mapping audit

Audit date: 2026-07-29

## Antigravity reference pipeline

The Antigravity Claude Proxy keeps model availability and model aliases
separate:

1. Select an authenticated Google account.
2. Call `POST /v1internal:fetchAvailableModels` with its OAuth bearer.
3. Read the account-visible `models` object and per-model quota metadata.
4. Filter to supported Claude/Gemini families.
5. Cache validation results for five minutes.
6. Apply a configured public alias before validating the resulting upstream ID.
7. Route only a validated target to `streamGenerateContent`.

Its upstream call is a real account-scoped discovery API. An alias cannot make
an unavailable model available.

## M365 evidence

Confirmed model sources:

- Production authenticated shell: the server-rendered chat page contains
  `modelSelectorMetadata.availableModelSelectionOptions` and
  `defaultModelSelectionId`. This is account-visible, dynamically parsed, and
  cached for 15 minutes.
- Bearer-only SignalR: known stable public slugs route to the exact upstream
  `tone`. A live zero-cookie probe confirmed
  `m365-copilot:gpt-5.5-think-deeper` routes as
  `Gpt_5_5_Reasoning` and completes with reasoning and text deltas.
- Public research: Microsoft PyRIT uses the same M365 `Chathub`, but exposes it
  generically as model `copilot`; it contains no catalog discovery.

Bounded route checks:

| Candidate | Unauthenticated `OPTIONS` | Bearer result | Conclusion |
|---|---:|---:|---|
| `/m365Copilot/ValidateGpt` | 401 | GET 403, empty POST 403 | Real route, contract and purpose unproven |
| `/m365Copilot/ValidateGptId` | 404 | Not attempted | Route absent |
| `/m365Copilot/ValidateGptIdOverride` | 404 | Not attempted | Route absent |
| `/m365Copilot/GetModels` | 404 | Not attempted | Route absent |
| `/m365Copilot/GetAvailableModels` | 404 | Not attempted | Route absent |
| `/m365Copilot/GetModelMetadata` | 404 | Not attempted | Route absent |

`ValidateGpt` must not be advertised as a model catalog. It may validate custom
GPTs/agents, and its two safe bearer probes returned no schema or data.

## Implemented beta architecture

`beta/m365_models.py` now provides:

- a source-labelled, account-scoped catalog record;
- future-safe `Gpt_X_Y_{Auto,Chat,Reasoning}` slug generation;
- namespaced and unnamespaced public model resolution;
- user aliases resolved before availability validation;
- alias target and cycle validation;
- exact canonical slug to SignalR `tone` routing;
- OpenAI-compatible `GET /v1/models`;
- safe catalog health metadata.

Catalog source meanings:

| Source | Account scoped | Dynamic |
|---|---:|---:|
| `authenticated_chat_shell` | Yes | Yes |
| `captured_chat_shell` | Yes | No |
| `live_probe` | Yes | No |
| `fallback` | No | No |

The current `beta/ms365-auth.json` has no captured catalog, so the safe status
truthfully reports five fallback models. It does not claim Antigravity-equivalent
dynamic discovery.

## Long forced-reasoning proof

A 1,130-character scheduling and optimality prompt was sent twice with the
explicit public model `m365-copilot:gpt-5.5-think-deeper`. The catalog resolved
it to canonical `gpt-5.5-think-deeper` and passed the exact upstream tone
`Gpt_5_5_Reasoning`.

The UTF-8 proof run completed in 23,679 ms with:

- zero cookies;
- 2 provider `reasoning_progress` updates;
- 126 `text_delta` updates;
- 129 append-only stream operations;
- 1 completion event;
- 419 reasoning-summary characters;
- 1,933 final-answer characters;
- streamed text exactly equal to the final response.

The response independently verified precedence, worker utilization, a
critical-path lower bound, and the optimal makespan. Prompt/answer text and
credentials are not stored in this audit.

## Remaining parity work

1. Capture the successful M365 browser request that supplies
   `modelSelectorMetadata`, if it is separate from the server-rendered shell.
2. If a bearer REST call exists, implement it as the highest-priority
   account-scoped source and cache per account—not globally.
3. Otherwise, add an explicit shell-catalog import command and store the
   captured model block in the ignored beta credential record.
4. Track per-model live probe status and last successful use without treating a
   generation success as discovery of unknown models.
5. Add quota/rate-limit metadata only if M365 returns model-specific evidence.
