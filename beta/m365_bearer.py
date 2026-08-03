"""Cookie-free, local-only M365 Copilot SignalR proof harness.

This module is intentionally not registered with the production provider
registry. It accepts a locally captured OAuth response plus non-secret route
metadata and exposes only safe status and probe output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

REPOSITORY_DIRECTORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIRECTORY))
SOURCE_DIRECTORY = REPOSITORY_DIRECTORY / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from curl_cffi.const import CurlWsFlag
from curl_cffi.requests import Session

from beta.m365_artifacts import artifact_store
from beta.m365_events import citation_metadata, suggestions_metadata
from codex_auth.providers.microsoft365 import CHAT_HUB, RECORD_SEPARATOR, USER_AGENT, Microsoft365CopilotProvider

EXPIRING_SOON_SECONDS = 300
MAX_PRE_SUBMIT_CONNECT_ATTEMPTS = 2
BETA_CONFIRM_ENV = "CODEX_AUTH_M365_BETA_CONFIRM"
BETA_DIRECTORY_ENV = "CODEX_AUTH_M365_BETA_DIR"
M365_AUTH_JSON_ENV = "CODEX_AUTH_M365_BETA_AUTH_JSON"
M365_STATE_FILE_ENV = "CODEX_AUTH_M365_BETA_STATE_FILE"
M365_BROKER_CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"
M365_REDIRECT_URI = "https://m365.cloud.microsoft/spalanding"
M365_REFRESH_REDIRECT_URI = "brk-multihub://Outlook.office.com"
M365_REFRESH_SCOPE = (
    "https://substrate.office.com/sydney/v2/.default openid profile offline_access"
)
M365_GRAPH_REFRESH_SCOPE = (
    "https://graph.microsoft.com/.default openid profile offline_access"
)
MSAL_BROWSER_VERSION = "5.9.0"
MSAL_CURRENT_TELEMETRY = "5|61,0,,,"
MSAL_LAST_TELEMETRY = "5|0|||0,0"
REASONING_UI_LAYOUTS = {
    "chain_of_thought",
    "chain_of_thought_search",
    "chain_of_thought_code",
    "chain_of_thought_cua_terminal",
    "generated_code",
    "chain_of_thought_cua_generated_image",
    "generated_code_result_block",
}
IMAGE_CHAT_VARIANTS = {
    "feature.EnableBase64DataInMessageAnnotations",
    "feature.EnableClientFileURLSupportForOfficeWebPaidCopilot",
}


class BetaConfigurationError(ValueError):
    """The local beta files are missing required non-secret protocol metadata."""


class BetaUpstreamError(RuntimeError):
    """Safe phase-only failure from the experimental upstream transport."""


_RUNTIME_CREDENTIAL_LOCK = threading.Lock()
_RUNTIME_CREDENTIAL: dict[str, Any] | None = None


def _environment_credential() -> tuple[dict[str, Any], Path | None]:
    """Load a deployment seed without ever returning it through status APIs."""

    global _RUNTIME_CREDENTIAL
    seed = os.environ.get(M365_AUTH_JSON_ENV)
    if not seed:
        raise BetaConfigurationError(f"{M365_AUTH_JSON_ENV} is not configured")
    state_value = os.environ.get(M365_STATE_FILE_ENV)
    state_path = Path(state_value).expanduser().resolve() if state_value else None
    with _RUNTIME_CREDENTIAL_LOCK:
        if _RUNTIME_CREDENTIAL is None:
            if state_path is not None and state_path.is_file():
                _RUNTIME_CREDENTIAL = _read_json(state_path)
            else:
                try:
                    parsed = json.loads(seed)
                except json.JSONDecodeError as exc:
                    raise BetaConfigurationError(
                        f"{M365_AUTH_JSON_ENV} must contain a JSON object"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise BetaConfigurationError(
                        f"{M365_AUTH_JSON_ENV} must contain a JSON object"
                    )
                _RUNTIME_CREDENTIAL = dict(parsed)
        return dict(_RUNTIME_CREDENTIAL), state_path


def _save_environment_credential(value: dict[str, Any], state_path: Path | None) -> None:
    global _RUNTIME_CREDENTIAL
    with _RUNTIME_CREDENTIAL_LOCK:
        _RUNTIME_CREDENTIAL = dict(value)
        if state_path is not None:
            _atomic_write_json(state_path, value)


def _safe_http_failure(prefix: str, exc: Exception) -> BetaUpstreamError:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return BetaUpstreamError(f"{prefix}_http_{status}")
    match = re.search(r"\b(400|401|403|404|408|409|429|500|502|503|504)\b", str(exc))
    if match:
        return BetaUpstreamError(f"{prefix}_http_{match.group(1)}")
    return BetaUpstreamError(f"{prefix}_failed")


@dataclass(frozen=True)
class M365Attachment:
    """An uploaded file or image bound to one M365 chat turn."""

    annotation_id: str
    name: str
    mime_type: str
    url: str | None = field(default=None, repr=False)
    annotation_type: str = "File"
    conversation_id: str | None = field(default=None, repr=False)

    def message_annotation(self) -> dict[str, Any]:
        file_type = Path(self.name).suffix.lstrip(".").lower() or "bin"
        is_image = self.annotation_type == "ImageFile"
        annotation = {
            "id": self.annotation_id,
            "messageAnnotationType": self.annotation_type,
            "text": self.name,
            "messageAnnotationMetadata": {
                "@type": "File" if is_image else self.annotation_type,
                "fileType": file_type,
            },
        }
        if is_image:
            annotation["messageAnnotationMetadata"].update(
                {
                    "annotationType": "File",
                    "fileName": self.name,
                }
            )
        if self.url:
            annotation["url"] = self.url
        return annotation


class M365StreamAssembler:
    """Convert per-message replacement snapshots into lane-aware deltas."""

    def __init__(self) -> None:
        self.snapshots: dict[str, str] = {}

    @staticmethod
    def _lane(message: dict[str, Any], kind: str) -> str:
        identity = (
            message.get("responseIdentifier")
            or message.get("messageId")
            or message.get("requestId")
            or f"{kind}:anonymous"
        )
        digest = hashlib.sha256(str(identity).encode()).hexdigest()[:12]
        return f"{kind}:{digest}"

    def _snapshot_event(
        self,
        message: dict[str, Any],
        kind: str,
        elapsed_ms: int,
    ) -> dict[str, Any] | None:
        text = message.get("text")
        if not isinstance(text, str) or not text:
            return None
        lane = self._lane(message, kind)
        previous = self.snapshots.get(lane, "")
        self.snapshots[lane] = text
        if text.startswith(previous):
            delta = text[len(previous) :]
            operation = "append"
        elif previous.startswith(text):
            delta = ""
            operation = "regression"
        else:
            delta = text
            operation = "replace"
        if not delta and operation != "replace":
            return None
        return {
            "type": kind,
            "lane": lane,
            "operation": operation,
            "delta": delta,
            "snapshot_characters": len(text),
            "elapsed_ms": elapsed_ms,
        }

    def consume(
        self,
        frame: dict[str, Any],
        messages: list[dict[str, Any]],
        elapsed_ms: int,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for message in messages:
            message_type = str(message.get("messageType") or "")
            if message.get("addToChainOfThought") is True:
                event = self._snapshot_event(message, "reasoning_progress", elapsed_ms)
                if event:
                    events.append(event)
                continue
            if message_type == "Progress":
                event = self._snapshot_event(message, "progress", elapsed_ms)
                if event:
                    events.append(event)
            if message.get("searchQueries"):
                events.append(
                    {
                        "type": "search_query",
                        "count": len(message["searchQueries"]),
                        "elapsed_ms": elapsed_ms,
                    }
                )
            references = message.get("references") or message.get("sourceAttributions") or []
            if references:
                events.append(
                    {
                        "type": "citation",
                        "count": len(references),
                        "citations": citation_metadata(references),
                        "elapsed_ms": elapsed_ms,
                    }
                )
            if message_type == "GeneratedCode" or message.get("hiddenText"):
                events.append(
                    {
                        "type": "generated_code",
                        "metadata": {"language": str(message.get("language") or "")[:40]},
                        "elapsed_ms": elapsed_ms,
                    }
                )
            if message.get("contentGenerationProgressList"):
                generated = artifact_store.resolve_generated_cards(
                    message["contentGenerationProgressList"]
                )
                events.append(
                    {
                        "type": "image_progress",
                        "artifact": (
                            generated[0]
                            if generated
                            else artifact_store.unretrievable("image")
                        ),
                        "elapsed_ms": elapsed_ms,
                    }
                )
                for artifact in generated:
                    event: dict[str, Any] = {
                        "type": "image",
                        "artifact": artifact,
                        "elapsed_ms": elapsed_ms,
                    }
                    if artifact.get("availability") == "embedded":
                        event["artifact_id"] = artifact["id"]
                    events.append(event)
            if message.get("adaptiveCards"):
                events.append({"type": "adaptive_card", "elapsed_ms": elapsed_ms})
                artifacts = artifact_store.resolve_generated_cards(
                    message["adaptiveCards"]
                )
                for artifact in artifacts:
                    event: dict[str, Any] = {
                        "type": "image",
                        "artifact": artifact,
                        "elapsed_ms": elapsed_ms,
                    }
                    if artifact.get("availability") == "embedded":
                        event["artifact_id"] = artifact["id"]
                    events.append(event)
            if (
                message.get("pluginInfo")
                or message_type
                in {
                    "TriggerPlugin",
                    "TriggerPluginAuth",
                    "ResumePluginAuth",
                    "TriggerConfirmation",
                    "ResumeInvokeAction",
                    "TriggerUserInputRequest",
                    "ResumeUserInputRequest",
                }
            ):
                events.append(
                    {
                        "type": "plugin",
                        "count": 1,
                        "elapsed_ms": elapsed_ms,
                    }
                )
            if message.get("suggestedResponses"):
                events.append(
                    {
                        "type": "suggestions",
                        "count": len(message["suggestedResponses"]),
                        "elapsed_ms": elapsed_ms,
                    }
                )
                suggestions = suggestions_metadata(message["suggestedResponses"])
                if suggestions:
                    events.append(
                        {
                            "type": "suggestions_detail",
                            "count": len(suggestions),
                            "suggestions": suggestions,
                            "elapsed_ms": elapsed_ms,
                        }
                    )
            if message_type == "ReferencesListComplete":
                events.append(
                    {
                        "type": "references_complete",
                        "elapsed_ms": elapsed_ms,
                    }
                )
            if (
                message.get("author") == "bot"
                and message_type
                not in {
                    "Progress",
                    "GeneratedCode",
                    "InternalSearchQuery",
                    "ReferencesListComplete",
                }
            ):
                event = self._snapshot_event(message, "text_delta", elapsed_ms)
                if event:
                    events.append(event)
        if frame.get("type") == 3:
            events.append({"type": "completion", "elapsed_ms": elapsed_ms})
        return events


def default_beta_directory() -> Path:
    configured = os.environ.get(BETA_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BetaConfigurationError("credential files are not configured") from exc
    except json.JSONDecodeError as exc:
        raise BetaConfigurationError("credential files must contain JSON objects") from exc
    if not isinstance(value, dict):
        raise BetaConfigurationError("credential files must contain JSON objects")
    return value


def _id_token_claims(raw: dict[str, Any]) -> dict[str, Any]:
    token = str(raw.get("id_token") or "")
    parts = token.split(".")
    if len(parts) != 3:
        raise BetaConfigurationError("M365 beta auth requires a valid id_token")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BetaConfigurationError("M365 beta id_token payload is invalid") from exc
    if not isinstance(claims, dict):
        raise BetaConfigurationError("M365 beta id_token payload must be an object")
    return claims


def _route_from_credential(raw: dict[str, Any]) -> dict[str, Any]:
    claims = _id_token_claims(raw)
    override = raw.get("route")
    if override is not None and not isinstance(override, dict):
        raise BetaConfigurationError("M365 beta route override must be an object")
    override = override or {}
    oauth_override = override.get("oauth")
    if oauth_override is not None and not isinstance(oauth_override, dict):
        raise BetaConfigurationError("M365 beta OAuth override must be an object")
    oauth_override = oauth_override or {}
    oauth_form = oauth_override.get("form") or {}
    if not isinstance(oauth_form, dict):
        raise BetaConfigurationError("M365 beta OAuth form override must be an object")

    tenant_id = str(claims.get("tid") or "").strip()
    client_id = str(claims.get("aud") or "").strip()
    object_id = str(claims.get("oid") or "").strip()
    identity = str(
        override.get("identity")
        or raw.get("identity")
        or claims.get("preferred_username")
        or claims.get("email")
        or ""
    ).strip()
    if not tenant_id or not client_id or not identity:
        raise BetaConfigurationError("M365 beta id_token is missing tenant, client, or identity claims")

    broker_client_id = str(
        oauth_override.get("brk_client_id")
        or raw.get("brk_client_id")
        or M365_BROKER_CLIENT_ID
    )
    redirect_uri = str(
        oauth_override.get("brk_redirect_uri")
        or raw.get("brk_redirect_uri")
        or M365_REDIRECT_URI
    )
    query = dict(oauth_override.get("query") or {})
    query.setdefault("brk_client_id", broker_client_id)
    query.setdefault("brk_redirect_uri", redirect_uri)
    query.setdefault("client_id", client_id)
    captured_form = {
        str(key): str(value)
        for key, value in oauth_form.items()
        if key != "refresh_token"
    }
    default_form = {
        "client_id": client_id,
        "redirect_uri": M365_REFRESH_REDIRECT_URI,
        "brk_client_id": broker_client_id,
        "brk_redirect_uri": redirect_uri,
        "scope": M365_REFRESH_SCOPE,
        "grant_type": "refresh_token",
        "client_info": "1",
        "x-client-SKU": "msal.js.browser",
        "x-client-VER": MSAL_BROWSER_VERSION,
        "x-ms-lib-capability": "retry-after, h429",
        "x-client-current-telemetry": MSAL_CURRENT_TELEMETRY,
        "x-client-last-telemetry": MSAL_LAST_TELEMETRY,
    }
    if object_id:
        default_form["X-AnchorMailbox"] = f"Oid:{object_id}@{tenant_id}"

    return {
        "identity": identity,
        "variants": override.get("variants") or raw.get("variants") or "",
        "oauth": {
            "capture_complete": bool(oauth_override.get("capture_complete")),
            "token_endpoint": oauth_override.get("token_endpoint")
            or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "query": query,
            # Preserve optional captured additions while keeping the
            # protocol-critical client, broker, redirect, and Sydney scope
            # fields authoritative.
            "form": {**captured_form, **default_form},
        },
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_captured_refresh_form(
    directory: Path | None = None,
) -> dict[str, Any]:
    """Persist the screenshot-proven non-secret broker refresh metadata."""

    base = directory or default_beta_directory()
    credential_path = base / "ms365-auth.json"
    raw = _read_json(credential_path)
    claims = _id_token_claims(raw)
    tenant_id = str(claims.get("tid") or "").strip()
    client_id = str(claims.get("aud") or "").strip()
    object_id = str(claims.get("oid") or "").strip()
    if not tenant_id or not client_id or not object_id:
        raise BetaConfigurationError(
            "captured refresh form requires tenant, client, and object claims"
        )
    route = dict(raw.get("route") or {})
    oauth = dict(route.get("oauth") or {})
    oauth["capture_complete"] = True
    oauth["query"] = {
        "brk_client_id": M365_BROKER_CLIENT_ID,
        "brk_redirect_uri": M365_REDIRECT_URI,
        "client_id": client_id,
    }
    oauth["form"] = {
        "client_id": client_id,
        "redirect_uri": M365_REFRESH_REDIRECT_URI,
        "brk_client_id": M365_BROKER_CLIENT_ID,
        "brk_redirect_uri": M365_REDIRECT_URI,
        "scope": M365_REFRESH_SCOPE,
        "grant_type": "refresh_token",
        "client_info": "1",
        "x-client-SKU": "msal.js.browser",
        "x-client-VER": MSAL_BROWSER_VERSION,
        "x-ms-lib-capability": "retry-after, h429",
        "x-client-current-telemetry": MSAL_CURRENT_TELEMETRY,
        "x-client-last-telemetry": MSAL_LAST_TELEMETRY,
        "X-AnchorMailbox": f"Oid:{object_id}@{tenant_id}",
    }
    route["oauth"] = oauth
    raw["route"] = route
    _atomic_write_json(credential_path, raw)
    return {
        "capture_complete": True,
        "query_fields": sorted(oauth["query"]),
        "form_fields": sorted(oauth["form"]),
        "secrets_written": False,
    }


@dataclass
class BetaCredential:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    refresh_expires_at: float | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, captured_at: float | None = None) -> "BetaCredential":
        access_token = str(raw.get("access_token") or "").strip()
        refresh_token = str(raw.get("refresh_token") or "").strip()
        scope = str(raw.get("scope") or "")
        if not access_token or not refresh_token:
            raise BetaConfigurationError("M365 beta auth requires access_token and refresh_token")
        accepted_scopes = (
            "substrate.office.com/sydney/v2",
            "substrate.office.com/.default",
        )
        if not any(candidate in scope for candidate in accepted_scopes):
            raise BetaConfigurationError(
                "M365 beta auth must include a Sydney or brokered Substrate scope"
            )
        captured_value = raw.get("captured_at")
        if captured_value is None:
            captured_value = captured_at
        if captured_value is None:
            try:
                issued_at = _id_token_claims(raw).get("iat")
                captured_value = int(issued_at) if issued_at is not None else None
            except BetaConfigurationError:
                captured_value = None
        captured = float(
            captured_value if captured_value is not None else time.time()
        )
        try:
            expires_in = int(raw.get("expires_in") or 0)
        except (TypeError, ValueError) as exc:
            raise BetaConfigurationError("M365 beta auth expires_in must be an integer") from exc
        if expires_in <= 0:
            raise BetaConfigurationError("M365 beta auth expires_in must be positive")
        refresh_expires = raw.get("refresh_token_expires_in")
        try:
            refresh_expires_at = captured + int(refresh_expires) if refresh_expires is not None else None
        except (TypeError, ValueError) as exc:
            raise BetaConfigurationError("refresh_token_expires_in must be an integer") from exc
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=captured + expires_in,
            refresh_expires_at=refresh_expires_at,
            raw=dict(raw),
        )


@dataclass(frozen=True)
class BetaRoute:
    identity: str
    variants: str
    token_endpoint: str
    token_query: dict[str, str]
    token_form: dict[str, str]
    refresh_capture_complete: bool

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "BetaRoute":
        identity = str(raw.get("identity") or "").strip()
        oauth = raw.get("oauth")
        if not identity or not isinstance(oauth, dict):
            raise BetaConfigurationError("M365 beta route requires identity and oauth metadata")
        endpoint = str(oauth.get("token_endpoint") or "")
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "login.microsoftonline.com" or not parsed.path.endswith("/oauth2/v2.0/token"):
            raise BetaConfigurationError("M365 beta route must use a Microsoft OAuth v2 token endpoint")
        query = oauth.get("query") or {}
        form = oauth.get("form") or {}
        if not isinstance(query, dict) or not isinstance(form, dict) or not str(form.get("client_id") or "").strip():
            raise BetaConfigurationError("M365 beta route requires OAuth query/form metadata and form.client_id")
        return cls(
            identity=identity,
            variants=str(raw.get("variants") or ""),
            token_endpoint=endpoint,
            token_query={str(key): str(value) for key, value in query.items()},
            token_form={str(key): str(value) for key, value in form.items() if key != "refresh_token"},
            refresh_capture_complete=bool(oauth.get("capture_complete")),
        )


class M365BearerBeta:
    """A single-account experimental adapter which never imports cookies."""

    def __init__(
        self,
        credential: BetaCredential,
        route: BetaRoute,
        credential_path: Path,
        *,
        session_factory: Callable[[], Any] = lambda: Session(impersonate="chrome"),
        credential_writer: Callable[[dict[str, Any]], None] | None = None,
        persistence_status: dict[str, Any] | None = None,
    ) -> None:
        self.credential = credential
        self.route = route
        self.credential_path = credential_path
        self.session_factory = session_factory
        self._credential_writer = credential_writer
        self.persistence_status = persistence_status or {
            "source": "file",
            "rotation_persistence": "host_filesystem",
            "restart_durable": False,
        }
        self.refreshing = False
        self._refresh_lock = threading.Lock()
        refresh_last_at = credential.raw.get("refresh_last_at")
        try:
            self.last_refresh_at = (
                float(refresh_last_at) if refresh_last_at is not None else None
            )
        except (TypeError, ValueError):
            self.last_refresh_at = None
        refresh_last_outcome = str(
            credential.raw.get("refresh_last_outcome") or ""
        ).strip()
        self.last_refresh_outcome = (
            refresh_last_outcome
            if refresh_last_outcome in {"succeeded", "failed"}
            else None
        )
        self.last_refresh_error_code = str(
            credential.raw.get("refresh_last_error_code") or ""
        ).strip() or None
        # A replaced raw OAuth response may retain diagnostics copied from the
        # previous credential. A real refresh failure always records its
        # timestamp atomically, so an outcome/error without that timestamp is
        # stale import metadata and must not block the new refresh token.
        if self.last_refresh_at is None:
            self.last_refresh_outcome = None
            self.last_refresh_error_code = None
        self.last_connect_attempts = 0
        self.last_connect_failure: str | None = None
        self.pre_submit_refreshes = 0

    @classmethod
    def from_directory(cls, directory: Path | None = None, *, session_factory: Callable[[], Any] | None = None) -> "M365BearerBeta":
        writer: Callable[[dict[str, Any]], None] | None = None
        if directory is None and os.environ.get(M365_AUTH_JSON_ENV):
            raw, state_path = _environment_credential()
            credential_path = state_path or (default_beta_directory() / "ms365-auth.json")

            def environment_writer(value: dict[str, Any]) -> None:
                _save_environment_credential(value, state_path)

            writer = environment_writer
            persistence = {
                "source": "environment",
                "rotation_persistence": (
                    "state_file" if state_path is not None else "process_memory"
                ),
                "restart_durable": state_path is not None,
            }
        else:
            base = directory or default_beta_directory()
            credential_path = base / "ms365-auth.json"
            raw = _read_json(credential_path)
            persistence = {
                "source": "file",
                "rotation_persistence": "host_filesystem",
                # A file is only restart-durable when the operator mounted
                # persistent storage; the beta cannot infer that safely.
                "restart_durable": False,
            }
        credential = BetaCredential.from_raw(raw)
        route = BetaRoute.from_raw(_route_from_credential(raw))
        return cls(
            credential,
            route,
            credential_path,
            session_factory=session_factory or (lambda: Session(impersonate="chrome")),
            credential_writer=writer,
            persistence_status=persistence,
        )

    def _persist_credential(self, value: dict[str, Any]) -> None:
        if self._credential_writer is not None:
            self._credential_writer(value)
        else:
            _atomic_write_json(self.credential_path, value)

    @property
    def seconds_until_expiry(self) -> int:
        return max(0, round(self.credential.expires_at - time.time()))

    def status(self) -> dict[str, Any]:
        refresh_terminal = self.last_refresh_error_code in {
            "invalid_grant:AADSTS70000",
        }
        if self.refreshing:
            state = "refreshing"
        elif self.seconds_until_expiry == 0:
            state = "re_import_required"
        elif self.last_refresh_outcome == "failed":
            state = "refresh_failed"
        elif self.seconds_until_expiry <= EXPIRING_SOON_SECONDS:
            state = "expiring_soon"
        else:
            state = "active"
        return {
            "state": state,
            "cookie_count": 0,
            "generation_ready": self.seconds_until_expiry > 0,
            "access_expires_in_seconds": self.seconds_until_expiry,
            "access_expiry_estimated": bool(
                self.credential.raw.get("access_expiry_estimated")
            ),
            "refresh_available": bool(self.credential.refresh_token and self.route.token_endpoint),
            "refresh_ready": bool(
                self.credential.refresh_token
                and self.route.token_endpoint
                and self.route.refresh_capture_complete
                and not refresh_terminal
            ),
            "refresh_capture_state": (
                "captured_form_payload"
                if self.route.refresh_capture_complete
                else "missing_form_payload"
            ),
            "last_refresh_at": int(self.last_refresh_at) if self.last_refresh_at else None,
            "last_refresh_outcome": self.last_refresh_outcome,
            "last_refresh_error_code": self.last_refresh_error_code,
            "recovery_action": (
                "capture_fresh_oauth_response"
                if refresh_terminal or self.seconds_until_expiry == 0
                else None
            ),
            "last_connect_attempts": self.last_connect_attempts,
            "last_connect_failure": self.last_connect_failure,
            "pre_submit_refreshes": self.pre_submit_refreshes,
            "generation_replay_policy": "never_after_submit",
            "credential_persistence": dict(self.persistence_status),
        }

    def _persist_refresh_diagnostic(
        self,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        now = int(time.time())
        self.last_refresh_at = now
        self.last_refresh_outcome = outcome
        self.last_refresh_error_code = error_code
        updated = {
            **self.credential.raw,
            "refresh_last_at": now,
            "refresh_last_outcome": outcome,
        }
        if error_code:
            updated["refresh_last_error_code"] = error_code
        else:
            updated.pop("refresh_last_error_code", None)
        self._persist_credential(updated)
        self.credential.raw = updated

    def _new_cookie_free_session(self) -> Any:
        session = self.session_factory()
        if list(session.cookies):
            session.close()
            raise BetaUpstreamError("cookie_free_session_failed")
        return session

    def _endpoint(
        self,
        session_id: str,
        conversation_id: str,
        request_id: str,
        additional_variants: set[str] | None = None,
    ) -> str:
        query = {
            "chatsessionid": request_id,
            "XRoutingParameterSessionKey": request_id,
            "clientrequestid": request_id,
            "X-SessionId": session_id,
            "ConversationId": conversation_id,
            "access_token": self.credential.access_token,
            "source": '"officeweb"',
            "product": "Office",
            "agentHost": "Bizchat.FullScreen",
            "licenseType": "Starter",
            "isEdu": "false",
            "agent": "web",
            "scenario": "OfficeWebPaidConsumerCopilot",
        }
        variants = {
            item
            for item in self.route.variants.split(",")
            if item
        }
        variants.update(additional_variants or set())
        if variants:
            query["variants"] = ",".join(sorted(variants))
        return f"{CHAT_HUB}/{urllib.parse.quote(self.route.identity, safe='')}?{urllib.parse.urlencode(query)}"

    @staticmethod
    def _frames(payload: bytes) -> Iterable[dict[str, Any]]:
        for item in payload.decode("utf-8", errors="replace").split(RECORD_SEPARATOR):
            if not item:
                continue
            try:
                value = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value

    def refresh(self) -> dict[str, Any]:
        if not self.route.refresh_capture_complete:
            self.last_refresh_outcome = "failed"
            raise BetaConfigurationError(
                "OAuth refresh requires the exact successful DevTools form payload"
            )
        if not self._refresh_lock.acquire(blocking=False):
            raise BetaUpstreamError("refresh_in_progress")
        try:
            return self._refresh_serial()
        finally:
            self._refresh_lock.release()

    def _refresh_serial(self) -> dict[str, Any]:
        self.refreshing = True
        session = None
        try:
            session = self._new_cookie_free_session()
            form = {**self.route.token_form, "grant_type": "refresh_token", "refresh_token": self.credential.refresh_token}
            response = session.post(
                self.route.token_endpoint,
                params={**self.route.token_query, "client-request-id": str(uuid.uuid4())},
                data=form,
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "Origin": "https://m365.cloud.microsoft",
                    "Referer": "https://m365.cloud.microsoft/",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
            try:
                if response.status_code != 200:
                    error_code = "unknown"
                    try:
                        error_body = response.json()
                        if isinstance(error_body, dict):
                            candidate = str(error_body.get("error") or "unknown")
                            if candidate.replace("_", "").replace("-", "").isalnum():
                                error_code = candidate[:80]
                            description = str(error_body.get("error_description") or "")
                            aadsts = re.search(r"AADSTS\d+", description)
                            if aadsts:
                                error_code = f"{error_code}:{aadsts.group(0)}"
                            error_codes = error_body.get("error_codes")
                            if isinstance(error_codes, list) and error_codes:
                                numeric_code = str(error_codes[0])
                                if numeric_code.isdigit() and numeric_code not in error_code:
                                    error_code = f"{error_code}:{numeric_code}"
                    except (TypeError, ValueError):
                        pass
                    self._persist_refresh_diagnostic("failed", error_code)
                    raise BetaUpstreamError(f"oauth_refresh_rejected:{error_code}")
                token_data = response.json()
            finally:
                response.close()
            updated = {**self.credential.raw, **token_data, "captured_at": int(time.time())}
            rotated_credential = BetaCredential.from_raw(updated)
            self._persist_credential(updated)
            self.credential = rotated_credential
            self._persist_refresh_diagnostic("succeeded")
            return self.status()
        except (BetaConfigurationError, BetaUpstreamError):
            if self.last_refresh_outcome != "failed":
                self.last_refresh_outcome = "failed"
            raise
        except Exception as exc:
            self.last_refresh_outcome = "failed"
            raise BetaUpstreamError("oauth_refresh_failed") from exc
        finally:
            self.refreshing = False
            if session is not None:
                session.close()

    def acquire_graph_credential(self) -> dict[str, Any]:
        """Acquire a Graph bearer from the same renewable broker session."""

        if not self.route.refresh_capture_complete:
            raise BetaConfigurationError(
                "Graph acquisition requires the captured OAuth refresh form"
            )
        if not self._refresh_lock.acquire(blocking=False):
            raise BetaUpstreamError("refresh_in_progress")
        session = None
        try:
            session = self._new_cookie_free_session()
            form = {
                **self.route.token_form,
                "grant_type": "refresh_token",
                "refresh_token": self.credential.refresh_token,
                "scope": M365_GRAPH_REFRESH_SCOPE,
            }
            response = session.post(
                self.route.token_endpoint,
                params={
                    **self.route.token_query,
                    "client-request-id": str(uuid.uuid4()),
                },
                data=form,
                headers={
                    "Accept": "*/*",
                    "Content-Type": (
                        "application/x-www-form-urlencoded;charset=utf-8"
                    ),
                    "Origin": "https://m365.cloud.microsoft",
                    "Referer": "https://m365.cloud.microsoft/",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
            try:
                if response.status_code != 200:
                    raise BetaUpstreamError(
                        f"graph_oauth_refresh_http_{response.status_code}"
                    )
                token_data = response.json()
            finally:
                response.close()
            if not isinstance(token_data, dict) or not str(
                token_data.get("access_token") or ""
            ):
                raise BetaUpstreamError(
                    "graph_oauth_refresh_missing_access_token"
                )
            captured_at = int(time.time())
            expires_in = int(token_data.get("expires_in") or 0)
            if expires_in <= 0:
                raise BetaUpstreamError(
                    "graph_oauth_refresh_missing_expiry"
                )
            resources = dict(self.credential.raw.get("resources") or {})
            resources["graph"] = {
                "access_token": str(token_data["access_token"]),
                "captured_at": captured_at,
                "expires_in": expires_in,
                "expires_at": captured_at + expires_in,
                "scope": str(token_data.get("scope") or M365_GRAPH_REFRESH_SCOPE),
                "source": "broker_refresh",
            }
            updated = {
                **self.credential.raw,
                "resources": resources,
            }
            rotated_refresh = str(token_data.get("refresh_token") or "")
            if rotated_refresh:
                updated["refresh_token"] = rotated_refresh
            self._persist_credential(updated)
            self.credential = BetaCredential.from_raw(updated)
            return {
                "state": "active",
                "source": "broker_refresh",
                "expires_in_seconds": expires_in,
                "cookie_count": 0,
            }
        except (BetaConfigurationError, BetaUpstreamError):
            raise
        except Exception as exc:
            raise BetaUpstreamError("graph_oauth_refresh_failed") from exc
        finally:
            if session is not None:
                session.close()
            self._refresh_lock.release()

    def _connect(
        self,
        session: Any,
        session_id: str,
        conversation_id: str,
        request_id: str,
        additional_variants: set[str] | None = None,
    ) -> Any:
        try:
            websocket = session.ws_connect(
                self._endpoint(
                    session_id,
                    conversation_id,
                    request_id,
                    additional_variants,
                ),
                headers={"Origin": "https://m365.cloud.microsoft", "User-Agent": USER_AGENT, "Cache-Control": "no-cache", "Pragma": "no-cache"},
                timeout=30,
            )
            websocket.send(json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR, CurlWsFlag.TEXT)
            handshake, _ = websocket.recv()
            if not any(True for _ in self._frames(handshake)):
                websocket.close()
                raise BetaUpstreamError("signalr_handshake_rejected")
            websocket.send(json.dumps({"type": 6}) + RECORD_SEPARATOR, CurlWsFlag.TEXT)
            return websocket
        except BetaUpstreamError:
            raise
        except Exception as exc:
            raise _safe_http_failure("signalr_connect", exc) from exc

    @staticmethod
    def _schema_paths(value: Any, prefix: str = "$", depth: int = 0) -> set[str]:
        if depth > 10:
            return {f"{prefix}:depth_limit"}
        if isinstance(value, dict):
            paths = {f"{prefix}:object"}
            for key, child in value.items():
                paths.update(M365BearerBeta._schema_paths(child, f"{prefix}.{key}", depth + 1))
            return paths
        if isinstance(value, list):
            paths = {f"{prefix}:array"}
            for child in value[:20]:
                paths.update(M365BearerBeta._schema_paths(child, f"{prefix}[]", depth + 1))
            return paths
        if value is None:
            kind = "null"
        elif isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, (int, float)):
            kind = "number"
        elif isinstance(value, str):
            kind = "string"
        else:
            kind = type(value).__name__
        return {f"{prefix}:{kind}"}

    @staticmethod
    def _frame_messages(frame: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        for argument in frame.get("arguments") or []:
            if isinstance(argument, dict):
                candidates.extend(argument.get("messages") or [])
        item = frame.get("item")
        if isinstance(item, dict):
            candidates.extend(item.get("messages") or [])

        messages: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, int]] = set()
        for message in candidates:
            if not isinstance(message, dict):
                continue
            text = message.get("text")
            key = (
                str(message.get("messageId") or ""),
                str(message.get("responseIdentifier") or ""),
                str(message.get("messageType") or ""),
                len(text) if isinstance(text, str) else -1,
            )
            if key in seen:
                continue
            seen.add(key)
            messages.append(message)
        return messages

    @classmethod
    def _normalized_event_types(cls, frame: dict[str, Any]) -> list[str]:
        events: list[str] = []
        frame_type = frame.get("type")
        if frame_type == 3:
            events.append("completion")
        elif frame_type == 6:
            events.append("keepalive")
        for message in cls._frame_messages(frame):
            message_type = str(message.get("messageType") or "")
            layout = str(message.get("layout") or "")
            copilot_message_type = str(
                message.get("copilotMessageType") or ""
            )
            if (
                layout in REASONING_UI_LAYOUTS
                or copilot_message_type == "thinking"
            ):
                events.append("reasoning_ui_item")
            if message.get("addToChainOfThought") is True:
                events.append("reasoning_progress")
            elif message_type == "Progress":
                events.append("progress")
            if message.get("searchQueries"):
                events.append("search_query")
            if message.get("references") or message.get("sourceAttributions"):
                events.append("citation")
            if message_type == "ReferencesListComplete":
                events.append("references_complete")
            if message_type == "GeneratedCode" or message.get("hiddenText"):
                events.append("generated_code")
            if message.get("contentGenerationProgressList"):
                events.append("image_progress")
            adaptive_cards = message.get("adaptiveCards") or []
            if adaptive_cards:
                events.append("adaptive_card")
                if any(
                    isinstance(body, dict) and body.get("images")
                    for card in adaptive_cards
                    if isinstance(card, dict)
                    for body in card.get("body") or []
                ):
                    events.append("image")
            if message.get("pluginInfo"):
                events.append("plugin")
            if message.get("suggestedResponses"):
                events.append("suggestions")
            if message.get("author") == "bot" and isinstance(message.get("text"), str):
                events.append("text_snapshot")
        return events

    def _exchange(
        self,
        prompt: str,
        model: str,
        observer: Callable[[dict[str, Any], int], None] | None = None,
        tone: str | None = None,
        attachments: list[M365Attachment] | None = None,
    ) -> str:
        if not prompt.strip():
            raise BetaConfigurationError("probe prompt must not be empty")
        if self.seconds_until_expiry <= EXPIRING_SOON_SECONDS:
            self.refresh()
        session = self._new_cookie_free_session()
        websocket = None
        try:
            attachment_conversation_ids = {
                attachment.conversation_id
                for attachment in attachments or []
                if attachment.conversation_id
            }
            if len(attachment_conversation_ids) > 1:
                raise BetaConfigurationError(
                    "all image attachments must share one conversation ID"
                )
            session_id = str(uuid.uuid4())
            conversation_id = (
                next(iter(attachment_conversation_ids))
                if attachment_conversation_ids
                else str(uuid.uuid4())
            )
            request_id, trace_id = uuid.uuid4().hex, uuid.uuid4().hex
            connection_variants = (
                IMAGE_CHAT_VARIANTS
                if any(
                    attachment.annotation_type == "ImageFile"
                    for attachment in attachments or []
                )
                else set()
            )
            self.last_connect_attempts = 0
            self.last_connect_failure = None
            self.pre_submit_refreshes = 0
            for attempt in range(1, MAX_PRE_SUBMIT_CONNECT_ATTEMPTS + 1):
                self.last_connect_attempts = attempt
                try:
                    websocket = self._connect(
                        session,
                        session_id,
                        conversation_id,
                        request_id,
                        connection_variants,
                    )
                    break
                except BetaUpstreamError as exc:
                    failure = str(exc)
                    self.last_connect_failure = failure
                    session.close()
                    if attempt == MAX_PRE_SUBMIT_CONNECT_ATTEMPTS:
                        raise
                    if (
                        failure == "signalr_connect_http_401"
                        and self.credential.refresh_token
                        and self.route.token_endpoint
                        and self.route.refresh_capture_complete
                    ):
                        self.refresh()
                        self.pre_submit_refreshes += 1
                    elif failure in {
                        "signalr_connect_http_429",
                        "signalr_connect_http_502",
                        "signalr_connect_http_503",
                        "signalr_connect_http_504",
                    }:
                        time.sleep(0.25 * attempt)
                    session = self._new_cookie_free_session()
            if websocket is None:
                raise BetaUpstreamError("signalr_connect_failed")
            request_payload = Microsoft365CopilotProvider._request_payload(
                prompt,
                session_id,
                request_id,
                trace_id,
                model,
                tone,
            )
            if attachments:
                request_payload["message"]["messageAnnotations"] = [
                    attachment.message_annotation() for attachment in attachments
                ]
                if any(
                    attachment.annotation_type == "ImageFile"
                    for attachment in attachments
                ):
                    request_payload["message"][
                        "connectedFederatedConnections"
                    ] = ["dummyId"]
            invocation = {
                "arguments": [
                    request_payload
                ],
                "invocationId": "0",
                "target": "chat",
                "type": 4,
            }
            websocket.send(json.dumps(invocation) + RECORD_SEPARATOR, CurlWsFlag.TEXT)
            final_text = ""
            started = time.monotonic()
            while True:
                payload, _ = websocket.recv()
                for frame in self._frames(payload):
                    if observer is not None:
                        observer(frame, round((time.monotonic() - started) * 1000))
                    if frame.get("type") == 3:
                        if not final_text:
                            raise BetaUpstreamError("signalr_completed_without_text")
                        return final_text
                    if frame.get("target") != "update":
                        continue
                    for argument in frame.get("arguments") or []:
                        for message in argument.get("messages") or []:
                            if message.get("author") == "bot" and isinstance(message.get("text"), str):
                                final_text = message["text"]
        except BetaUpstreamError:
            raise
        except Exception as exc:
            raise BetaUpstreamError("signalr_generation_failed") from exc
        finally:
            if websocket is not None:
                websocket.close()
            session.close()

    def generate(self, prompt: str, model: str = "auto") -> str:
        return self._exchange(prompt, model)

    def generate_stream(
        self,
        prompt: str,
        emit: Callable[[dict[str, Any]], None],
        model: str = "auto",
        tone: str | None = None,
        attachments: list[M365Attachment] | None = None,
    ) -> str:
        assembler = M365StreamAssembler()

        def observe(frame: dict[str, Any], elapsed_ms: int) -> None:
            for event in assembler.consume(
                frame,
                self._frame_messages(frame),
                elapsed_ms,
            ):
                emit(event)

        return self._exchange(prompt, model, observe, tone, attachments)

    def inspect(self, prompt: str, model: str = "auto") -> dict[str, Any]:
        frame_types: Counter[str] = Counter()
        targets: Counter[str] = Counter()
        message_types: Counter[str] = Counter()
        authors: Counter[str] = Counter()
        chain_of_thought_flags: Counter[str] = Counter()
        normalized_event_types: Counter[str] = Counter()
        stream_event_types: Counter[str] = Counter()
        stream_operations: Counter[str] = Counter()
        artifact_availability: Counter[str] = Counter()
        artifact_phases: Counter[str] = Counter()
        stream_delta_characters = 0
        stream_assembler = M365StreamAssembler()
        schema_paths: set[str] = set()
        message_keys: set[str] = set()
        bot_text_lengths: list[int] = []
        reference_count = 0
        source_attribution_count = 0
        search_query_count = 0
        adaptive_card_count = 0
        first_bot_update_ms: int | None = None
        completion_ms: int | None = None

        def observe(frame: dict[str, Any], elapsed_ms: int) -> None:
            nonlocal adaptive_card_count, completion_ms, first_bot_update_ms
            nonlocal reference_count, search_query_count, source_attribution_count
            nonlocal stream_delta_characters
            frame_types[str(frame.get("type", "missing"))] += 1
            targets[str(frame.get("target", "missing"))] += 1
            schema_paths.update(self._schema_paths(frame))
            normalized_event_types.update(self._normalized_event_types(frame))
            for event in stream_assembler.consume(
                frame,
                self._frame_messages(frame),
                elapsed_ms,
            ):
                stream_event_types[event["type"]] += 1
                artifact = event.get("artifact")
                if isinstance(artifact, dict):
                    availability = str(artifact.get("availability") or "unknown")
                    artifact_availability[availability] += 1
                    metadata = artifact.get("metadata")
                    if isinstance(metadata, dict) and metadata.get("phase"):
                        artifact_phases[str(metadata["phase"])] += 1
                if event.get("operation"):
                    stream_operations[str(event["operation"])] += 1
                stream_delta_characters += len(str(event.get("delta") or ""))
            if frame.get("type") == 3:
                completion_ms = elapsed_ms
            for message in self._frame_messages(frame):
                message_keys.update(str(key) for key in message)
                authors[str(message.get("author", "missing"))] += 1
                message_types[str(message.get("messageType", "missing"))] += 1
                if "addToChainOfThought" in message:
                    chain_of_thought_flags[str(bool(message["addToChainOfThought"])).lower()] += 1
                reference_count += len(message.get("references") or [])
                source_attribution_count += len(message.get("sourceAttributions") or [])
                search_query_count += len(message.get("searchQueries") or [])
                adaptive_card_count += len(message.get("adaptiveCards") or [])
                text = message.get("text")
                if message.get("author") == "bot" and isinstance(text, str):
                    bot_text_lengths.append(len(text))
                    if first_bot_update_ms is None:
                        first_bot_update_ms = elapsed_ms

        try:
            answer = self._exchange(prompt, model, observe)
            result = "passed"
        except BetaUpstreamError as exc:
            if str(exc) != "signalr_completed_without_text":
                raise
            answer = ""
            result = "completed_without_text"
        cumulative_text = all(
            later >= earlier for earlier, later in zip(bot_text_lengths, bot_text_lengths[1:])
        )
        marker_terms = ("thought", "reason", "progress", "analysis", "search", "citation", "code", "plugin", "adaptive")
        marker_paths = sorted(
            path for path in schema_paths if any(term in path.lower() for term in marker_terms)
        )
        return {
            "result": result,
            "model": model,
            "cookie_count": self.status()["cookie_count"],
            "response_characters": len(answer),
            "frame_count": sum(frame_types.values()),
            "frame_types": dict(sorted(frame_types.items())),
            "targets": dict(sorted(targets.items())),
            "message_types": dict(sorted(message_types.items())),
            "normalized_event_types": dict(sorted(normalized_event_types.items())),
            "stream_event_types": dict(sorted(stream_event_types.items())),
            "artifact_availability": dict(sorted(artifact_availability.items())),
            "artifact_phases": dict(sorted(artifact_phases.items())),
            "stream_operations": dict(sorted(stream_operations.items())),
            "stream_delta_characters": stream_delta_characters,
            "authors": dict(sorted(authors.items())),
            "add_to_chain_of_thought": dict(sorted(chain_of_thought_flags.items())),
            "message_keys": sorted(message_keys),
            "reference_count": reference_count,
            "source_attribution_count": source_attribution_count,
            "search_query_count": search_query_count,
            "adaptive_card_count": adaptive_card_count,
            "bot_text_update_count": len(bot_text_lengths),
            "bot_text_lengths": bot_text_lengths,
            "text_updates_are_nonshrinking": cumulative_text,
            "first_bot_update_ms": first_bot_update_ms,
            "completion_ms": completion_ms,
            "interesting_schema_paths": marker_paths,
            "schema_paths": sorted(schema_paths),
        }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Local-only, cookie-free M365 bearer beta")
    parser.add_argument(
        "command",
        choices=(
            "status",
            "probe",
            "inspect",
            "import-refresh-capture",
            "refresh",
        ),
    )
    parser.add_argument("--model", default="auto")
    parser.add_argument("--prompt")
    parser.add_argument(
        "--report",
        type=Path,
        help="write a redacted inspection report (valid only with inspect)",
    )
    arguments = parser.parse_args()
    try:
        if arguments.command == "import-refresh-capture":
            print(json.dumps(apply_captured_refresh_form(), sort_keys=True))
            return 0
        beta = M365BearerBeta.from_directory()
        if arguments.command == "status":
            print(json.dumps(beta.status(), sort_keys=True))
            return 0
        if os.environ.get(BETA_CONFIRM_ENV) != "1":
            raise BetaConfigurationError(f"set {BETA_CONFIRM_ENV}=1 before running the live beta probe")
        if arguments.command == "refresh":
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "phase": "oauth_refresh",
                        **beta.refresh(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "inspect":
            if not arguments.prompt:
                raise BetaConfigurationError("--prompt is required for inspect")
            report = beta.inspect(arguments.prompt, arguments.model)
            if arguments.report is not None:
                _atomic_write_json(arguments.report, report)
            print(json.dumps(report, sort_keys=True))
            return 0
        started = time.monotonic()
        beta.generate("Reply exactly with: M365_BETA_OK", arguments.model)
        print(json.dumps({"result": "passed", "phase": "completed", "latency_ms": round((time.monotonic() - started) * 1000), **beta.status()}))
        return 0
    except BetaConfigurationError as exc:
        if arguments.command == "status":
            print(json.dumps({"state": "not_configured", "cookie_count": 0, "refresh_available": False}))
            return 0
        print(json.dumps({"result": "failed", "phase": str(exc)}))
        return 1
    except BetaUpstreamError as exc:
        print(json.dumps({"result": "failed", "phase": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
