import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Iterable

from curl_cffi.requests import Session

from ...config import load_cookie_text
from ..base import BaseProvider

logger = logging.getLogger("codex_auth")

BASE_URL = "https://chatgpt.com"
DEFAULT_BUILD = "8690212"
DEFAULT_VERSION = "prod-01e2e28be691381cd82f4d9cc32c32f65c723ad8"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ChatGPTSessionError(RuntimeError):
    pass


def parse_netscape_cookies(text: str) -> list[dict[str, Any]]:
    """Parse the seven-column Netscape cookie format without writing a temp file."""
    cookies = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"Invalid Netscape cookie record on line {line_number}")
        domain, _, path, secure, expires, name, value = fields
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure.upper() == "TRUE",
                "expires_at": int(expires) if expires.isdigit() and int(expires) > 0 else None,
            }
        )
    if not cookies:
        raise ValueError("The cookie source contains no Netscape cookie records")
    return cookies


def _pow_config() -> list[Any]:
    return [
        random.choice([8, 12, 16, 24, 32]) * random.choice([1920, 2560, 3008, 4010]),
        datetime.now(timezone.utc).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (UTC)"),
        None,
        random.random(),
        USER_AGENT,
        None,
        "dpl=1440a687921de39ff5ee56b92807faaadce73f13",
        "en-US",
        "en-US",
        0,
        random.choice(["webdriver-false", "vendor-Google Inc.", "cookieEnabled-true"]),
        "location",
        random.choice(["innerWidth", "innerHeight", "devicePixelRatio", "navigator"]),
        time.perf_counter(),
        str(uuid.uuid4()),
        "",
        8,
        int(time.time()),
    ]


def _pow_answer(seed: str, difficulty: str, config: list[Any], attempts: int = 100_000) -> str:
    target = bytes.fromhex(difficulty)
    prefix = json.dumps(config[:3], separators=(",", ":"))[:-1] + ","
    middle = "," + json.dumps(config[4:9], separators=(",", ":"))[1:-1] + ","
    suffix = "," + json.dumps(config[10:], separators=(",", ":"))[1:]
    for nonce in range(attempts):
        candidate = f"{prefix}{nonce}{middle}{nonce >> 1}{suffix}".encode()
        encoded = base64.b64encode(candidate)
        if hashlib.sha3_512(seed.encode() + encoded).digest()[: len(target)] <= target:
            return encoded.decode()
    fallback = base64.b64encode(json.dumps(seed).encode()).decode()
    return "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + fallback


def _requirements_token() -> str:
    return "gAAAAAC" + _pow_answer(str(random.random()), "0fffff", _pow_config())


def _proof_token(pow_info: dict[str, Any]) -> str | None:
    if not pow_info.get("required"):
        return None
    config = _pow_config()
    difficulty = str(pow_info["difficulty"])
    seed = str(pow_info["seed"])
    for nonce in range(100_000):
        config[3] = nonce
        encoded = base64.b64encode(json.dumps(config).encode()).decode()
        if hashlib.sha3_512((seed + encoded).encode()).hexdigest()[: len(difficulty)] <= difficulty:
            return "gAAAAAB" + encoded
    fallback = base64.b64encode(json.dumps(seed).encode()).decode()
    return "gAAAAABwQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + fallback


def _message_delta(event: Any, state: dict[str, Any]) -> str:
    if not isinstance(event, dict):
        return ""
    state["conversation_id"] = event.get("conversation_id") or state.get("conversation_id")
    if event.get("message_id"):
        state["message_id"] = event["message_id"]
    value = event.get("v")
    if isinstance(value, dict):
        message = value.get("message")
        if not isinstance(message, dict):
            return ""
        state["role"] = (message.get("author") or {}).get("role")
        content = message.get("content") or {}
        state["content_type"] = content.get("content_type")
        state["message_id"] = message.get("id") or state.get("message_id")
        state["conversation_id"] = value.get("conversation_id") or state.get("conversation_id")
        if state["role"] != "assistant" or state["content_type"] != "text":
            return ""
        parts = content.get("parts") or []
        text = "".join(part for part in parts if isinstance(part, str))
        previous = state.get("text", "")
        state["text"] = text
        return text[len(previous) :] if text.startswith(previous) else text

    patches = value if isinstance(value, list) else [event] if event.get("p") else []
    if not patches or state.get("role") != "assistant" or state.get("content_type") != "text":
        return ""
    chunks = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        path = patch.get("p", "")
        if patch.get("o") == "append" and path.startswith("/message/content/parts/"):
            chunk = patch.get("v")
            if isinstance(chunk, str):
                chunks.append(chunk)
        if patch.get("o") == "replace" and path == "/message/id":
            state["message_id"] = patch.get("v")
    delta = "".join(chunks)
    state["text"] = state.get("text", "") + delta
    return delta


class OpenAIProvider(BaseProvider):
    def __init__(self):
        self.session: Session | None = None
        self.access_token = ""
        self.device_id = ""
        self.model = "auto"
        self.conversation_id: str | None = None
        self.parent_message_id: str | None = None
        self.lock = asyncio.Lock()
        self.auth_mode = "cookie_exchange"
        self.cookie_metadata: list[dict[str, Any]] = []
        self.initialized_at = time.time()
        self._account_cache: dict[str, Any] | None = None
        self._account_cache_time = 0.0

    @staticmethod
    def _jwt_claims(token: str) -> dict[str, Any]:
        try:
            payload = token.split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            return json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _expiry_details(expires_at: int | float | None) -> dict[str, Any]:
        if not expires_at:
            return {"expires_at": None, "seconds_remaining": None, "expired": None}
        remaining = int(expires_at - time.time())
        return {
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "seconds_remaining": remaining,
            "expired": remaining <= 0,
        }

    def _headers(self, target: str, *, accept: str = "*/*") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "accept": accept,
            "content-type": "application/json",
            "oai-device-id": self.device_id,
            "oai-language": "en-US",
            "oai-client-build-number": os.environ.get("CODEX_AUTH_CLIENT_BUILD", DEFAULT_BUILD),
            "oai-client-version": os.environ.get("CODEX_AUTH_CLIENT_VERSION", DEFAULT_VERSION),
            "origin": BASE_URL,
            "referer": BASE_URL + (f"/c/{self.conversation_id}" if self.conversation_id else "/"),
        }
        if target == "/backend-api/f/conversation":
            headers.update(
                {
                    "x-openai-target-path": target,
                    "x-openai-target-route": target,
                    "x-oai-is-client-observation": "false",
                    "x-oai-turn-trace-id": str(uuid.uuid4()),
                    "oai-session-id": str(uuid.uuid4()),
                }
            )
        return headers

    def _initialize_sync(self) -> None:
        self.session = Session(impersonate="chrome")
        self.cookie_metadata = parse_netscape_cookies(load_cookie_text())
        for cookie in self.cookie_metadata:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
            )
        self.access_token = os.environ.get("CODEX_AUTH_ACCESS_TOKEN", "").strip()
        self.auth_mode = "hosted_bearer" if self.access_token else "cookie_exchange"
        if not self.access_token:
            response = self.session.get(BASE_URL + "/api/auth/session", timeout=30)
            if response.status_code != 200:
                raise ChatGPTSessionError(
                    f"ChatGPT session validation failed with HTTP {response.status_code}; "
                    "refresh cookies.txt or set CODEX_AUTH_ACCESS_TOKEN for hosted deployments"
                )
            self.access_token = (response.json() or {}).get("accessToken", "")
        self.device_id = self.session.cookies.get("oai-did") or ""
        if not self.access_token or not self.device_id:
            raise ChatGPTSessionError("ChatGPT session did not provide an access token and oai-did cookie")
        self.initialized_at = time.time()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        logger.info("[OpenAI] HTTP-only provider authenticated; no browser process started")

    async def close(self) -> None:
        if self.session:
            await asyncio.to_thread(self.session.close)

    async def reset_session(self, model: str):
        self.model = model or "auto"
        self.conversation_id = None
        self.parent_message_id = None

    def _chat_requirements(self) -> tuple[str, str | None]:
        assert self.session
        response = self.session.post(
            BASE_URL + "/backend-api/sentinel/chat-requirements",
            headers=self._headers("/backend-api/sentinel/chat-requirements"),
            json={"p": _requirements_token()},
            timeout=30,
        )
        if response.status_code != 200:
            raise ChatGPTSessionError(f"Chat requirements failed with HTTP {response.status_code}")
        data = response.json()
        return data["token"], _proof_token(data.get("proofofwork") or {})

    def _prepare(
        self,
        prompt: str,
        message_id: str,
        chat_token: str,
        proof_token: str | None,
    ) -> str:
        assert self.session and self.conversation_id and self.parent_message_id
        headers = self._headers("/backend-api/f/conversation/prepare")
        headers["openai-sentinel-chat-requirements-token"] = chat_token
        if proof_token:
            headers["openai-sentinel-proof-token"] = proof_token
        payload = {
            "action": "next",
            "fork_from_shared_post": False,
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_message_id,
            "model": self.model,
            "client_prepare_state": "none",
            "timezone_offset_min": -330,
            "timezone": "Asia/Calcutta",
            "conversation_mode": {"kind": "primary_assistant"},
            "partial_query": {
                "id": message_id,
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
            },
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        response = self.session.post(
            BASE_URL + "/backend-api/f/conversation/prepare",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            raise ChatGPTSessionError(f"Conversation prepare failed with HTTP {response.status_code}")
        conduit = (response.json() or {}).get("conduit_token")
        if not conduit:
            raise ChatGPTSessionError("Conversation prepare did not return a conduit token")
        return conduit

    def _generate_sync(self, prompt: str) -> Iterable[str]:
        assert self.session
        message_id = str(uuid.uuid4())
        parent_id = self.parent_message_id or str(uuid.uuid4())
        is_continuation = bool(self.conversation_id and self.parent_message_id)
        target = "/backend-api/f/conversation" if is_continuation else "/backend-api/conversation"
        chat_token, proof_token = self._chat_requirements()
        headers = self._headers(target, accept="text/event-stream")
        headers["openai-sentinel-chat-requirements-token"] = chat_token
        if proof_token:
            headers["openai-sentinel-proof-token"] = proof_token
        client_prepare_state = None
        if is_continuation:
            headers["x-conduit-token"] = self._prepare(prompt, message_id, chat_token, proof_token)
            client_prepare_state = "sent"
        payload = {
            "action": "next",
            "messages": [
                {
                    "id": message_id,
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {"content_type": "text", "parts": [prompt]},
                    "metadata": {"serialization_metadata": {"custom_symbol_offsets": []}},
                }
            ],
            "conversation_id": self.conversation_id,
            "parent_message_id": parent_id,
            "model": self.model,
            "timezone_offset_min": -330,
            "timezone": "Asia/Calcutta",
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        if client_prepare_state:
            payload["client_prepare_state"] = client_prepare_state
        response = self.session.post(
            BASE_URL + target,
            headers=headers,
            json=payload,
            stream=True,
            timeout=180,
        )
        if response.status_code != 200:
            raise ChatGPTSessionError(f"Conversation request failed with HTTP {response.status_code}")

        state: dict[str, Any] = {}
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = _message_delta(event, state)
            if delta:
                yield delta
        self.conversation_id = state.get("conversation_id") or self.conversation_id
        self.parent_message_id = state.get("message_id") or self.parent_message_id
        if not state.get("text"):
            raise ChatGPTSessionError("ChatGPT returned no assistant text")

    async def generate_stream(
        self,
        prompt: str,
        files: list | None = None,
        web_search: bool = False,
    ) -> AsyncGenerator[str, None]:
        if files:
            raise ChatGPTSessionError("HTTP-only mode does not yet support file uploads")
        if web_search:
            raise ChatGPTSessionError("HTTP-only mode does not yet support web search")
        if not prompt:
            raise ChatGPTSessionError("Prompt cannot be empty")

        async with self.lock:
            queue: asyncio.Queue[tuple[str | None, BaseException | None]] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def worker() -> None:
                try:
                    for chunk in self._generate_sync(prompt):
                        asyncio.run_coroutine_threadsafe(queue.put((chunk, None)), loop).result()
                    asyncio.run_coroutine_threadsafe(queue.put((None, None)), loop).result()
                except BaseException as exc:
                    asyncio.run_coroutine_threadsafe(queue.put((None, exc)), loop).result()

            task = asyncio.create_task(asyncio.to_thread(worker))
            while True:
                chunk, error = await queue.get()
                if error:
                    await task
                    raise error
                if chunk is None:
                    break
                yield chunk
            await task

    async def fetch_models(self) -> list[Dict[str, Any]]:
        def fetch() -> list[Dict[str, Any]]:
            assert self.session
            response = self.session.get(
                BASE_URL + "/backend-api/models",
                headers=self._headers("/backend-api/models"),
                timeout=30,
            )
            if response.status_code != 200:
                raise ChatGPTSessionError(f"Model discovery failed with HTTP {response.status_code}")
            return (response.json() or {}).get("models", [])

        return await asyncio.to_thread(fetch)

    def runtime_status(self) -> dict[str, Any]:
        token_expiry = self._jwt_claims(self.access_token).get("exp")
        session_cookie = next(
            (cookie for cookie in self.cookie_metadata if cookie["name"] == "__Secure-next-auth.session-token"),
            {},
        )
        device_cookie = next(
            (cookie for cookie in self.cookie_metadata if cookie["name"] == "oai-did"),
            {},
        )
        return {
            "transport": "curl-cffi",
            "browser_process": False,
            "max_concurrent_generations": 1,
            "auth_mode": self.auth_mode,
            "initialized": bool(self.session and self.access_token and self.device_id),
            "uptime_seconds": int(time.time() - self.initialized_at),
            "selected_model": self.model,
            "conversation_active": bool(self.conversation_id),
            "access_token": self._expiry_details(token_expiry),
            "session_cookie": self._expiry_details(session_cookie.get("expires_at")),
            "device_cookie": self._expiry_details(device_cookie.get("expires_at")),
            "proxy_capabilities": {
                "text": True,
                "streaming": True,
                "ollama_compatibility": True,
                "file_uploads": False,
                "web_search": False,
                "image_input": False,
            },
        }

    def _fetch_account_details_sync(self) -> dict[str, Any]:
        assert self.session
        headers = self._headers("/backend-api/accounts/check/v4-2023-04-27")
        account_response = self.session.get(
            BASE_URL + "/backend-api/accounts/check/v4-2023-04-27",
            headers=headers,
            timeout=30,
        )
        me_response = self.session.get(
            BASE_URL + "/backend-api/me",
            headers=self._headers("/backend-api/me"),
            timeout=30,
        )
        settings_response = self.session.get(
            BASE_URL + "/backend-api/settings/user",
            headers=self._headers("/backend-api/settings/user"),
            timeout=30,
        )
        failures = [
            response.status_code
            for response in (account_response, me_response, settings_response)
            if response.status_code != 200
        ]
        if failures:
            raise ChatGPTSessionError(f"Account discovery failed with HTTP {failures[0]}")

        account_data = (account_response.json() or {}).get("accounts", {}).get("default", {})
        account = account_data.get("account") or {}
        entitlement = account_data.get("entitlement") or {}
        subscription = account_data.get("last_active_subscription") or {}
        profile = me_response.json() or {}
        user_settings = (settings_response.json() or {}).get("settings") or {}
        return {
            "profile": {
                "name": profile.get("name") or profile.get("first_name"),
                "email": profile.get("email"),
                "created_at": self._expiry_details(profile.get("created")).get("expires_at"),
                "country": profile.get("country"),
                "region": profile.get("region"),
                "region_code": profile.get("region_code"),
                "mfa_enabled": bool(profile.get("mfa_flag_enabled")),
                "email_domain_type": profile.get("email_domain_type"),
            },
            "account": {
                "id": account.get("account_id"),
                "role": account.get("account_user_role"),
                "structure": account.get("structure"),
                "plan_type": account.get("plan_type"),
                "is_deactivated": bool(account.get("is_deactivated")),
                "residency": account.get("account_compute_residency_display_name"),
                "residency_description": account.get("account_compute_residency_description"),
            },
            "entitlement": {
                "subscription_plan": entitlement.get("subscription_plan"),
                "has_active_subscription": bool(entitlement.get("has_active_subscription")),
                "expires_at": entitlement.get("expires_at"),
                "renews_at": entitlement.get("renews_at"),
                "will_renew": bool(subscription.get("will_renew")),
                "is_delinquent": bool(entitlement.get("is_delinquent")),
            },
            "privacy": {
                "training_allowed": user_settings.get("training_allowed"),
                "codex_training_allowed": user_settings.get("codex_training_allowed_v2"),
                "connector_search_enabled": user_settings.get("connector_search_enabled"),
                "voice_enabled": user_settings.get("voice_enabled"),
            },
            "feature_count": len(account_data.get("features") or []),
            "can_access_with_session": bool(account_data.get("can_access_with_session")),
            "runtime": self.runtime_status(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    async def fetch_account_details(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and self._account_cache and time.time() - self._account_cache_time < 60:
            cached = dict(self._account_cache)
            cached["runtime"] = self.runtime_status()
            return cached
        async with self.lock:
            details = await asyncio.to_thread(self._fetch_account_details_sync)
            self._account_cache = details
            self._account_cache_time = time.time()
            return details

    async def proxy_request(
        self,
        method: str,
        path: str,
        body: Any = None,
    ) -> tuple[int, str, bytes]:
        def request() -> tuple[int, str, bytes]:
            assert self.session
            response = self.session.request(
                method,
                BASE_URL + "/backend-api/" + path.lstrip("/"),
                headers=self._headers("/backend-api/" + path.lstrip("/")),
                json=body,
                timeout=60,
            )
            return response.status_code, response.headers.get("content-type", ""), response.content

        return await asyncio.to_thread(request)


provider = OpenAIProvider()
