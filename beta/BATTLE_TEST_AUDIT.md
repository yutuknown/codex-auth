# M365 beta live battle-test audit

Audit date: 2026-08-03

The campaign used only `beta/ms365-auth.json`, zero browser cookies, and
redacted result storage. Prompts, response text, credentials, identity,
headers, conversation IDs, and protected URLs were not retained.

## Outcome

The exact Microsoft broker refresh request is now implemented with the Sydney
resource scope and broker fields in both query and form. It rotated the local
access and refresh pair, and the new access bearer completed a zero-cookie
generation. The full campaign then completed 23 passing checks, zero failures,
four blocked checks, and one intentionally out-of-scope check.

## Real results

| Area | Result | Evidence |
|---|---|---|
| Zero-cookie enforcement | Passed | Every constructed provider session had an empty cookie jar |
| Fallback catalog truthfulness | Passed | Five fallback routes were labelled non-dynamic and non-account-scoped |
| Client-tool rejection | Passed | Actual HTTP request returned 400 before upstream submission |
| Sampling-control rejection | Passed | Actual HTTP request returned 400 before upstream submission |
| API-key guard | Passed | Missing key returned 401; the ephemeral local test key returned 200 |
| Persistent telemetry | Passed | Metrics, latency/failure health, recent logs, and live log SSE expose only bounded redacted records |
| SignalR authentication | Passed after bearer recovery | Each live route connected once with zero cookies |
| OAuth refresh | Passed | Exact broker query/form refresh rotated the pair and the new bearer completed generation with zero cookies |
| Five model routes | Passed | Auto, Quick Response, Think Deeper, GPT 5.5 Quick Response, and GPT 5.5 Think Deeper completed |
| Reasoning/search/code/image generation | Passed | Live frames contained the mapped progress, citation, code, image, and completion events |
| Anthropic/OpenAI buffered and streaming routes | Passed | All four compatibility paths completed against the real upstream |
| System/multi-turn transcript | Passed | Preserved roles and response preferences produced the exact requested live marker without using the rejected `System:` label |
| Image input | Passed | The zero-cookie upload and `ImageFile` chat binding completed through `/v1/messages` with HTTP 200 in 14.7 seconds |
| Remote image URL | Passed | A public HTTPS PNG was retrieved with DNS, redirect, cookie, and size guards, uploaded through Substrate, and analyzed with zero cookies |
| Native browser file input | Passed | A manually uploaded marker text file was read back exactly by authenticated M365 Copilot |
| Beta Graph file-to-chat binding | Unverified upstream | Zero-cookie Graph upload and extraction complete, but the adapter's generated `File` annotation has not yet been proven by a matching beta response |
| Dynamic model discovery | Blocked by upstream evidence | No confirmed bearer catalog endpoint |
| Model quota | Blocked by upstream evidence | No confirmed bearer quota endpoint |
| Provider reasoning signature | Unavailable | M365 did not expose one |
| Multi-account routing | Out of scope | The beta intentionally uses one personal account |

A separate semantic image proof uploaded a real DevTools screenshot and asked
M365 to identify the selected tab and three visible fields. With
`cookie_count=0`, it correctly reported the Payload tab plus `client_id`,
`redirect_uri`, and `grant_type`. No prompt, response, credential, identity,
header, or protected URL was added to the machine-readable report.

## Bugs found by battle testing

1. Image upload upstream failures escaped the FastAPI handler and aborted the
   request. They now return a safe 502 response.
2. Missing `captured_at` reset token age on every process start. Expiry now
   derives from the ID-token issuance time.
3. SignalR connection errors were too generic. Safe HTTP classification now
   distinguishes authentication failures such as `signalr_connect_http_401`.
4. A known refresh failure incorrectly blocked a still-valid generation
   bearer. Campaign readiness now uses `generation_ready`, independently from
   refresh readiness.
5. Refresh failure state existed only in memory. The ignored local record now
   persists safe outcome/error metadata and reports
   `capture_fresh_oauth_response` without exposing either token.
6. The rejected `System:` label was replaced with a response-preferences and
   structured-transcript envelope. A live exact-marker test now proves
   instruction fidelity while still not claiming a native upstream system role.
7. The image uploader used raw file-part fields and emitted an `Image`
   annotation. M365 expects a `FileBase64` data URL plus image option sets, then
   an `ImageFile` annotation containing file metadata and a federated-connection
   marker.

## Rerun gate

Keep the ignored `beta/ms365-auth.json` local and run:

```powershell
$env:CODEX_AUTH_M365_BETA_CONFIRM = "1"
python -m beta.m365_battle_test
```

The generated `beta/battle-test-report.json` is ignored and contains only
redacted counts, statuses, timings, and safe phases.
