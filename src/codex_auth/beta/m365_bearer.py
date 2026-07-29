"""Cookie-free, local-only M365 Copilot SignalR proof harness.

This module is intentionally not registered with the production provider
registry. It accepts a locally captured OAuth response plus non-secret route
metadata and exposes only safe status and probe output.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from curl_cffi.const import CurlWsFlag
from curl_cffi.requests import Session

from ..providers.microsoft365 import CHAT_HUB, RECORD_SEPARATOR, USER_AGENT, Microsoft365CopilotProvider

EXPIRING_SOON_SECONDS = 300
BETA_CONFIRM_ENV = "CODEX_AUTH_M365_BETA_CONFIRM"
BETA_DIRECTORY_ENV = "CODEX_AUTH_M365_BETA_DIR"


class BetaConfigurationError(ValueError):
    """The local beta files are missing required non-secret protocol metadata."""


class BetaUpstreamError(RuntimeError):
    """Safe phase-only failure from the experimental upstream transport."""


def default_beta_directory() -> Path:
    configured = os.environ.get(BETA_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "beta"


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


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
        if "substrate.office.com/sydney/v2" not in scope:
            raise BetaConfigurationError("M365 beta auth must include a Sydney scope")
        captured = float(raw.get("captured_at") or captured_at or time.time())
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
    ) -> None:
        self.credential = credential
        self.route = route
        self.credential_path = credential_path
        self.session_factory = session_factory
        self.refreshing = False
        self._refresh_lock = threading.Lock()
        self.last_refresh_at: float | None = None
        self.last_refresh_outcome: str | None = None

    @classmethod
    def from_directory(cls, directory: Path | None = None, *, session_factory: Callable[[], Any] | None = None) -> "M365BearerBeta":
        base = directory or default_beta_directory()
        credential_path = base / "ms365-auth.json"
        credential = BetaCredential.from_raw(_read_json(credential_path))
        route = BetaRoute.from_raw(_read_json(base / "ms365-route.json"))
        return cls(credential, route, credential_path, session_factory=session_factory or (lambda: Session(impersonate="chrome")))

    @property
    def seconds_until_expiry(self) -> int:
        return max(0, round(self.credential.expires_at - time.time()))

    def status(self) -> dict[str, Any]:
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
            "access_expires_in_seconds": self.seconds_until_expiry,
            "refresh_available": bool(self.credential.refresh_token and self.route.token_endpoint),
            "last_refresh_at": int(self.last_refresh_at) if self.last_refresh_at else None,
            "last_refresh_outcome": self.last_refresh_outcome,
        }

    def _new_cookie_free_session(self) -> Any:
        session = self.session_factory()
        if list(session.cookies):
            session.close()
            raise BetaUpstreamError("cookie_free_session_failed")
        return session

    def _endpoint(self, session_id: str, conversation_id: str, request_id: str) -> str:
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
        if self.route.variants:
            query["variants"] = self.route.variants
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
                params=self.route.token_query,
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
                    raise BetaUpstreamError("oauth_refresh_rejected")
                token_data = response.json()
            finally:
                response.close()
            updated = {**self.credential.raw, **token_data, "captured_at": int(time.time())}
            rotated_credential = BetaCredential.from_raw(updated)
            _atomic_write_json(self.credential_path, updated)
            self.credential = rotated_credential
            self.last_refresh_at = time.time()
            self.last_refresh_outcome = "succeeded"
            return self.status()
        except (BetaConfigurationError, BetaUpstreamError):
            self.last_refresh_outcome = "failed"
            raise
        except Exception as exc:
            self.last_refresh_outcome = "failed"
            raise BetaUpstreamError("oauth_refresh_failed") from exc
        finally:
            self.refreshing = False
            if session is not None:
                session.close()

    def _connect(self, session: Any, session_id: str, conversation_id: str, request_id: str) -> Any:
        try:
            websocket = session.ws_connect(
                self._endpoint(session_id, conversation_id, request_id),
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
            raise BetaUpstreamError("signalr_connect_failed") from exc

    def generate(self, prompt: str, model: str = "auto") -> str:
        if not prompt.strip():
            raise BetaConfigurationError("probe prompt must not be empty")
        if self.seconds_until_expiry <= EXPIRING_SOON_SECONDS:
            self.refresh()
        session = self._new_cookie_free_session()
        websocket = None
        try:
            session_id, conversation_id, request_id, trace_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4().hex, uuid.uuid4().hex
            try:
                websocket = self._connect(session, session_id, conversation_id, request_id)
            except BetaUpstreamError:
                session.close()
                self.refresh()
                session = self._new_cookie_free_session()
                websocket = self._connect(session, session_id, conversation_id, request_id)
            invocation = {
                "arguments": [Microsoft365CopilotProvider._request_payload(prompt, session_id, request_id, trace_id, model)],
                "invocationId": "0",
                "target": "chat",
                "type": 4,
            }
            websocket.send(json.dumps(invocation) + RECORD_SEPARATOR, CurlWsFlag.TEXT)
            final_text = ""
            while True:
                payload, _ = websocket.recv()
                for frame in self._frames(payload):
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


def _main() -> int:
    parser = argparse.ArgumentParser(description="Local-only, cookie-free M365 bearer beta")
    parser.add_argument("command", choices=("status", "probe"))
    parser.add_argument("--model", default="auto")
    arguments = parser.parse_args()
    try:
        beta = M365BearerBeta.from_directory()
        if arguments.command == "status":
            print(json.dumps(beta.status(), sort_keys=True))
            return 0
        if os.environ.get(BETA_CONFIRM_ENV) != "1":
            raise BetaConfigurationError(f"set {BETA_CONFIRM_ENV}=1 before running the live beta probe")
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
