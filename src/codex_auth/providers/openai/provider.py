import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import random
import socket
import struct
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
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
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 4
MAX_ADMITTED_GENERATIONS = 4
METADATA_CACHE_SECONDS = 300
AUTH_FAILURE_STATUS_CODES = {401, 403}


class ChatGPTSessionError(RuntimeError):
    pass


class ProviderBusyError(ChatGPTSessionError):
    pass


def _image_dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png" and len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    if mime_type == "image/gif" and len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return struct.unpack("<HH", data[6:10])
    if mime_type == "image/jpeg" and data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return struct.unpack(">HH", data[offset + 5 : offset + 9])[::-1]
            if marker in {0xD8, 0xD9}:
                offset += 2
                continue
            segment_length = int.from_bytes(data[offset + 2 : offset + 4], "big")
            if segment_length < 2:
                break
            offset += 2 + segment_length
    return 0, 0


def _sniff_mime_type(data: bytes, declared: str | None = None) -> str:
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),
        (b"%PDF-", "application/pdf"),
    )
    for signature, mime_type in signatures:
        if data.startswith(signature):
            return mime_type
    if b"\x00" not in data[:1024]:
        return "text/plain"
    return "application/octet-stream"


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
        self.admission = asyncio.Semaphore(MAX_ADMITTED_GENERATIONS)
        self.metadata_lock = asyncio.Lock()
        self.token_refresh_lock = threading.Lock()
        self.auth_mode = "cookie_exchange"
        self.cookie_metadata: list[dict[str, Any]] = []
        self.initialized_at = time.time()
        self._account_cache: dict[str, Any] | None = None
        self._account_cache_time = 0.0
        self._models_cache: list[Dict[str, Any]] | None = None
        self._models_cache_time = 0.0

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
            "accept": accept,
            "content-type": "application/json",
            "oai-device-id": self.device_id,
            "oai-language": "en-US",
            "oai-client-build-number": os.environ.get("CODEX_AUTH_CLIENT_BUILD", DEFAULT_BUILD),
            "oai-client-version": os.environ.get("CODEX_AUTH_CLIENT_VERSION", DEFAULT_VERSION),
            "origin": BASE_URL,
            "referer": BASE_URL + (f"/c/{self.conversation_id}" if self.conversation_id else "/"),
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
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

    def _refresh_access_token_sync(self) -> bool:
        assert self.session
        response = self.session.get(BASE_URL + "/api/auth/session", timeout=30)
        if response.status_code != 200:
            return False
        access_token = (response.json() or {}).get("accessToken", "")
        if not access_token or access_token == self.access_token:
            return False
        self.access_token = access_token
        self.auth_mode = "cookie_refresh"
        self._account_cache = None
        self._models_cache = None
        logger.info("[OpenAI] Refreshed the access token from the authenticated cookie session")
        return True

    def _authenticated_request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        assert self.session
        request_headers = dict(headers or self._headers(target))
        token_used = self.access_token
        response = self.session.request(
            method,
            BASE_URL + target,
            headers=request_headers,
            **kwargs,
        )
        if response.status_code not in AUTH_FAILURE_STATUS_CODES:
            return response
        with self.token_refresh_lock:
            refreshed = self.access_token != token_used or self._refresh_access_token_sync()
        if refreshed:
            response.close()
            request_headers["Authorization"] = f"Bearer {self.access_token}"
            response = self.session.request(
                method,
                BASE_URL + target,
                headers=request_headers,
                **kwargs,
            )
            if response.status_code not in AUTH_FAILURE_STATUS_CODES:
                return response
        response.close()
        cookie_headers = dict(request_headers)
        cookie_headers.pop("Authorization", None)
        response = self.session.request(
            method,
            BASE_URL + target,
            headers=cookie_headers,
            **kwargs,
        )
        if response.status_code not in AUTH_FAILURE_STATUS_CODES:
            self.auth_mode = "cookie_only"
        return response

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
            if response.status_code == 200:
                self.access_token = (response.json() or {}).get("accessToken", "")
            if not self.access_token:
                self.auth_mode = "cookie_only"
        self.device_id = self.session.cookies.get("oai-did") or ""
        if not self.device_id:
            raise ChatGPTSessionError("ChatGPT session did not provide an oai-did cookie")
        self.initialized_at = time.time()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        logger.info("[OpenAI] HTTP-only provider authenticated; no browser process started")

    async def close(self) -> None:
        if self.session:
            await asyncio.to_thread(self.session.close)

    async def reset_session(self, model: str):
        async with self.lock:
            self.model = model or "auto"
            self.conversation_id = None
            self.parent_message_id = None

    def _chat_requirements(self) -> tuple[str, str | None]:
        assert self.session
        response = self._authenticated_request(
            "POST",
            "/backend-api/sentinel/chat-requirements",
            headers=self._headers("/backend-api/sentinel/chat-requirements"),
            json={"p": _requirements_token()},
            timeout=30,
        )
        if response.status_code != 200:
            raise ChatGPTSessionError(f"Chat requirements failed with HTTP {response.status_code}")
        data = response.json()
        return data["token"], _proof_token(data.get("proofofwork") or {})

    @staticmethod
    def _validate_remote_file_url(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ChatGPTSessionError("File URL must be a public HTTP or HTTPS URL")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            }
        except socket.gaierror as exc:
            raise ChatGPTSessionError("File URL hostname could not be resolved") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ChatGPTSessionError("File URL must not resolve to a private or local address")

    def _download_remote_file(self, url: str) -> tuple[bytes, str | None, str]:
        download_session = Session(impersonate="chrome")
        current_url = url
        try:
            for _ in range(4):
                self._validate_remote_file_url(current_url)
                response = download_session.get(current_url, stream=True, allow_redirects=False, timeout=30)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        raise ChatGPTSessionError("File URL redirect did not include a destination")
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue
                if response.status_code != 200:
                    response.close()
                    raise ChatGPTSessionError(f"File URL returned HTTP {response.status_code}")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_UPLOAD_BYTES:
                            response.close()
                            raise ChatGPTSessionError("File exceeds the 20 MB upload limit")
                    except ValueError as exc:
                        response.close()
                        raise ChatGPTSessionError("File URL returned an invalid Content-Length header") from exc
                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        response.close()
                        raise ChatGPTSessionError("File exceeds the 20 MB upload limit")
                    chunks.append(chunk)
                mime_type = response.headers.get("content-type")
                response.close()
                name = Path(urllib.parse.unquote(urllib.parse.urlparse(current_url).path)).name
                return b"".join(chunks), mime_type, name
            raise ChatGPTSessionError("File URL followed too many redirects")
        finally:
            download_session.close()

    def _decode_input_file(self, source: Any, index: int) -> tuple[bytes, str, str]:
        name = ""
        declared_mime = None
        if isinstance(source, dict):
            name = str(source.get("name") or "")
            declared_mime = source.get("mime_type")
            source = source.get("url") or ""
        if not isinstance(source, str) or not source:
            raise ChatGPTSessionError("Image or file input is missing its URL or data")

        if source.startswith("data:"):
            header, separator, encoded = source.partition(",")
            if not separator:
                raise ChatGPTSessionError("Invalid data URL")
            media_header = header[5:]
            declared_mime = media_header.split(";", 1)[0] or declared_mime
            try:
                if ";base64" in media_header.lower():
                    if len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
                        raise ChatGPTSessionError("File exceeds the 20 MB upload limit")
                    data = base64.b64decode(encoded, validate=True)
                else:
                    if len(encoded) > MAX_UPLOAD_BYTES * 3:
                        raise ChatGPTSessionError("File exceeds the 20 MB upload limit")
                    data = urllib.parse.unquote_to_bytes(encoded)
            except (binascii.Error, ValueError) as exc:
                raise ChatGPTSessionError("Invalid base64 file data") from exc
        elif source.startswith(("http://", "https://")):
            data, response_mime, response_name = self._download_remote_file(source)
            declared_mime = declared_mime or response_mime
            name = name or response_name
        else:
            try:
                if len(source) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
                    raise ChatGPTSessionError("File exceeds the 20 MB upload limit")
                data = base64.b64decode(source, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ChatGPTSessionError(
                    "File input must be a data URL, public HTTP(S) URL, or raw base64"
                ) from exc

        if not data:
            raise ChatGPTSessionError("Uploaded file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ChatGPTSessionError("File exceeds the 20 MB upload limit")
        mime_type = _sniff_mime_type(data, declared_mime)
        extension = mimetypes.guess_extension(mime_type) or ""
        safe_name = Path(name).name[:128] if name else f"upload-{index}{extension}"
        return data, mime_type, safe_name

    def _upload_file(self, source: Any, index: int) -> dict[str, Any]:
        assert self.session
        data, mime_type, file_name = self._decode_input_file(source, index)
        width, height = _image_dimensions(data, mime_type)
        use_case = "multimodal" if mime_type.startswith("image/") else "my_files"
        create_body: dict[str, Any] = {
            "file_name": file_name,
            "file_size": len(data),
            "use_case": use_case,
        }
        if width and height:
            create_body.update({"width": width, "height": height})

        create_response = self._authenticated_request(
            "POST",
            "/backend-api/files",
            headers=self._headers("/backend-api/files", accept="application/json"),
            json=create_body,
            timeout=30,
        )
        if create_response.status_code != 200:
            raise ChatGPTSessionError(f"File registration failed with HTTP {create_response.status_code}")
        created = create_response.json() or {}
        file_id = created.get("file_id")
        upload_url = created.get("upload_url")
        if not file_id or not upload_url:
            raise ChatGPTSessionError("File registration did not return an upload destination")

        time.sleep(0.5)
        upload_session = Session(impersonate="chrome")
        try:
            upload_response = upload_session.put(
                upload_url,
                headers={
                    "Content-Type": mime_type,
                    "x-ms-blob-type": "BlockBlob",
                    "x-ms-version": "2020-04-08",
                    "Origin": BASE_URL,
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.8",
                    "Referer": BASE_URL + "/",
                },
                data=data,
                timeout=60,
            )
        finally:
            upload_session.close()
        if upload_response.status_code not in {200, 201}:
            raise ChatGPTSessionError(f"File blob upload failed with HTTP {upload_response.status_code}")

        uploaded_response = self._authenticated_request(
            "POST",
            f"/backend-api/files/{file_id}/uploaded",
            headers=self._headers(f"/backend-api/files/{file_id}/uploaded", accept="application/json"),
            json={},
            timeout=30,
        )
        if uploaded_response.status_code != 200:
            raise ChatGPTSessionError(f"File finalization failed with HTTP {uploaded_response.status_code}")
        return {
            "id": file_id,
            "mime_type": mime_type,
            "name": file_name,
            "size": len(data),
            "width": width,
            "height": height,
            "is_image": mime_type.startswith("image/"),
        }

    @staticmethod
    def _user_message(prompt: str, message_id: str, uploads: list[dict[str, Any]]) -> dict[str, Any]:
        attachments = []
        image_parts = []
        for upload in uploads:
            attachment = {
                "id": upload["id"],
                "mimeType": upload["mime_type"],
                "name": upload["name"],
                "size": upload["size"],
            }
            if upload["width"] and upload["height"]:
                attachment.update({"width": upload["width"], "height": upload["height"]})
            attachments.append(attachment)
            if upload["is_image"]:
                image_parts.append(
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"file-service://{upload['id']}",
                        "size_bytes": upload["size"],
                        "width": upload["width"],
                        "height": upload["height"],
                    }
                )
        content = (
            {"content_type": "multimodal_text", "parts": [*image_parts, prompt]}
            if image_parts
            else {"content_type": "text", "parts": [prompt]}
        )
        metadata: dict[str, Any] = {"serialization_metadata": {"custom_symbol_offsets": []}}
        if attachments:
            metadata["attachments"] = attachments
        return {
            "id": message_id,
            "author": {"role": "user"},
            "create_time": time.time(),
            "content": content,
            "metadata": metadata,
        }

    def _prepare(
        self,
        user_message: dict[str, Any],
        chat_token: str,
        proof_token: str | None,
        web_search: bool,
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
                "id": user_message["id"],
                "author": user_message["author"],
                "content": user_message["content"],
                "metadata": user_message["metadata"],
            },
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        if web_search:
            payload["force_use_tool"] = "web"
        response = self._authenticated_request(
            "POST",
            "/backend-api/f/conversation/prepare",
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

    @staticmethod
    def _canonical_assistant_text(
        conversation: dict[str, Any],
        preferred_message_id: str | None = None,
    ) -> str:
        candidates = []
        for node in (conversation.get("mapping") or {}).values():
            message = (node or {}).get("message") or {}
            if (message.get("author") or {}).get("role") != "assistant":
                continue
            content = message.get("content") or {}
            if content.get("content_type") not in {"text", "multimodal_text"}:
                continue
            text = "".join(part for part in (content.get("parts") or []) if isinstance(part, str))
            if not text:
                continue
            candidates.append(
                (
                    message.get("id") == preferred_message_id,
                    float(message.get("create_time") or 0),
                    text,
                )
            )
        if not candidates:
            return ""
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _fetch_canonical_response(
        self,
        conversation_id: str,
        message_id: str | None,
        minimum_length: int = 0,
    ) -> str:
        assert self.session
        target = f"/backend-api/conversation/{conversation_id}"
        best_text = ""
        for attempt in range(3):
            response = self._authenticated_request(
                "GET",
                target,
                headers=self._headers(target, accept="application/json"),
                timeout=30,
            )
            if response.status_code == 200:
                text = self._canonical_assistant_text(response.json() or {}, message_id)
                if len(text) > len(best_text):
                    best_text = text
                if text and len(text) >= minimum_length:
                    return text
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        return best_text

    @staticmethod
    def _text_chunks(text: str, size: int = 512) -> Iterable[str]:
        for offset in range(0, len(text), size):
            yield text[offset : offset + size]

    def _generate_sync(
        self,
        prompt: str,
        files: list[Any] | None = None,
        web_search: bool = False,
        buffered: bool = False,
    ) -> Iterable[str]:
        assert self.session
        message_id = str(uuid.uuid4())
        parent_id = self.parent_message_id or str(uuid.uuid4())
        uploads = [self._upload_file(source, index) for index, source in enumerate(files or [], 1)]
        user_message = self._user_message(prompt, message_id, uploads)
        is_continuation = bool(self.conversation_id and self.parent_message_id)
        target = "/backend-api/f/conversation" if is_continuation else "/backend-api/conversation"
        chat_token, proof_token = self._chat_requirements()
        headers = self._headers(target, accept="text/event-stream")
        headers["openai-sentinel-chat-requirements-token"] = chat_token
        if proof_token:
            headers["openai-sentinel-proof-token"] = proof_token
        client_prepare_state = None
        if is_continuation:
            headers["x-conduit-token"] = self._prepare(user_message, chat_token, proof_token, web_search)
            client_prepare_state = "sent"
        payload = {
            "action": "next",
            "messages": [user_message],
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
        if web_search:
            payload["force_use_tool"] = "web"
        if client_prepare_state:
            payload["client_prepare_state"] = client_prepare_state
        response = self._authenticated_request(
            "POST",
            target,
            headers=headers,
            json=payload,
            stream=True,
            timeout=180,
        )
        if response.status_code != 200:
            raise ChatGPTSessionError(f"Conversation request failed with HTTP {response.status_code}")

        state: dict[str, Any] = {}
        streamed_chunks = []
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
                streamed_chunks.append(delta)
                if not buffered:
                    yield delta
        self.conversation_id = state.get("conversation_id") or self.conversation_id
        self.parent_message_id = state.get("message_id") or self.parent_message_id
        streamed_text = "".join(streamed_chunks)
        canonical_text = ""
        if buffered and self.conversation_id:
            canonical_text = self._fetch_canonical_response(
                self.conversation_id,
                self.parent_message_id,
                minimum_length=len(streamed_text),
            )
        final_text = canonical_text or streamed_text
        if buffered:
            yield from self._text_chunks(final_text)
        if not final_text:
            raise ChatGPTSessionError("ChatGPT returned no assistant text")

    async def generate_stream(
        self,
        prompt: str,
        files: list | None = None,
        web_search: bool = False,
        model: str | None = None,
        realtime: bool = False,
    ) -> AsyncGenerator[str, None]:
        if not prompt and not files:
            raise ChatGPTSessionError("Prompt and file input cannot both be empty")
        if len(files or []) > MAX_ATTACHMENTS:
            raise ChatGPTSessionError(f"A maximum of {MAX_ATTACHMENTS} attachments is supported")

        try:
            await asyncio.wait_for(self.admission.acquire(), timeout=0.01)
        except TimeoutError as exc:
            raise ProviderBusyError("Generation queue is full; retry later") from exc
        try:
            async with self.lock:
                self.model = model or "auto"
                self.conversation_id = None
                self.parent_message_id = None
                queue: asyncio.Queue[tuple[str | None, BaseException | None]] = asyncio.Queue(maxsize=8)
                loop = asyncio.get_running_loop()
                stop_event = threading.Event()

                def worker() -> None:
                    def enqueue(item: tuple[str | None, BaseException | None]) -> bool:
                        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                        while True:
                            try:
                                future.result(timeout=0.25)
                                return True
                            except FutureTimeoutError:
                                if stop_event.is_set():
                                    future.cancel()
                                    return False

                    try:
                        for chunk in self._generate_sync(
                            prompt,
                            files=files,
                            web_search=web_search,
                            buffered=not realtime,
                        ):
                            if stop_event.is_set() or not enqueue((chunk, None)):
                                return
                        enqueue((None, None))
                    except BaseException as exc:
                        enqueue((None, exc))

                task = asyncio.create_task(asyncio.to_thread(worker))
                try:
                    while True:
                        chunk, error = await queue.get()
                        if error:
                            await task
                            raise error
                        if chunk is None:
                            break
                        yield chunk
                    await task
                finally:
                    stop_event.set()
                    if not task.done():
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
        finally:
            self.admission.release()

    async def fetch_models(self, *, refresh: bool = False) -> list[Dict[str, Any]]:
        if (
            not refresh
            and self._models_cache is not None
            and time.time() - self._models_cache_time < METADATA_CACHE_SECONDS
        ):
            return list(self._models_cache)

        def fetch() -> list[Dict[str, Any]]:
            assert self.session
            response = self._authenticated_request(
                "GET",
                "/backend-api/models",
                headers=self._headers("/backend-api/models"),
                timeout=30,
            )
            if response.status_code != 200:
                raise ChatGPTSessionError(f"Model discovery failed with HTTP {response.status_code}")
            return (response.json() or {}).get("models", [])

        async with self.metadata_lock:
            if (
                not refresh
                and self._models_cache is not None
                and time.time() - self._models_cache_time < METADATA_CACHE_SECONDS
            ):
                return list(self._models_cache)
            models = await asyncio.to_thread(fetch)
            self._models_cache = models
            self._models_cache_time = time.time()
            return list(models)

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
            "max_pending_generations": MAX_ADMITTED_GENERATIONS - 1,
            "max_attachments": MAX_ATTACHMENTS,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "auth_mode": self.auth_mode,
            "initialized": bool(
                self.session
                and self.device_id
                and (self.access_token or self.auth_mode == "cookie_only")
            ),
            "uptime_seconds": int(time.time() - self.initialized_at),
            "selected_model": self.model,
            "conversation_active": bool(self.conversation_id),
            "access_token": self._expiry_details(token_expiry),
            "session_cookie": self._expiry_details(session_cookie.get("expires_at")),
            "device_cookie": self._expiry_details(device_cookie.get("expires_at")),
            "proxy_capabilities": {
                "text": True,
                "streaming": True,
                "realtime_text_streaming": False,
                "canonical_buffered_streaming": True,
                "ollama_compatibility": True,
                "file_uploads": True,
                "web_search": True,
                "image_input": True,
                "function_tools": False,
                "canvas": False,
                "image_generation": False,
            },
        }

    def _fetch_account_details_sync(self) -> dict[str, Any]:
        assert self.session
        headers = self._headers("/backend-api/accounts/check/v4-2023-04-27")
        account_response = self._authenticated_request(
            "GET",
            "/backend-api/accounts/check/v4-2023-04-27",
            headers=headers,
            timeout=30,
        )
        me_response = self._authenticated_request(
            "GET",
            "/backend-api/me",
            headers=self._headers("/backend-api/me"),
            timeout=30,
        )
        settings_response = self._authenticated_request(
            "GET",
            "/backend-api/settings/user",
            headers=self._headers("/backend-api/settings/user"),
            timeout=30,
        )
        endpoint_status = {
            "account": account_response.status_code,
            "profile": me_response.status_code,
            "settings": settings_response.status_code,
        }
        if account_response.status_code != 200 and me_response.status_code != 200:
            raise ChatGPTSessionError(
                f"Account discovery failed with HTTP {account_response.status_code}/{me_response.status_code}"
            )

        account_payload = account_response.json() if account_response.status_code == 200 else {}
        profile = me_response.json() if me_response.status_code == 200 else {}
        settings_payload = settings_response.json() if settings_response.status_code == 200 else {}
        account_data = (account_payload or {}).get("accounts", {}).get("default", {})
        account = account_data.get("account") or {}
        entitlement = account_data.get("entitlement") or {}
        subscription = account_data.get("last_active_subscription") or {}
        user_settings = (settings_payload or {}).get("settings") or {}
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
            "endpoint_status": endpoint_status,
            "warnings": [
                f"{name} endpoint returned HTTP {status}"
                for name, status in endpoint_status.items()
                if status != 200
            ],
            "runtime": self.runtime_status(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    async def fetch_account_details(self, *, refresh: bool = False) -> dict[str, Any]:
        if (
            not refresh
            and self._account_cache
            and time.time() - self._account_cache_time < METADATA_CACHE_SECONDS
        ):
            cached = dict(self._account_cache)
            cached["runtime"] = self.runtime_status()
            return cached
        async with self.metadata_lock:
            if (
                not refresh
                and self._account_cache
                and time.time() - self._account_cache_time < METADATA_CACHE_SECONDS
            ):
                cached = dict(self._account_cache)
                cached["runtime"] = self.runtime_status()
                return cached
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
            response = self._authenticated_request(
                method,
                "/backend-api/" + path.lstrip("/"),
                headers=self._headers("/backend-api/" + path.lstrip("/")),
                json=body,
                timeout=60,
            )
            return response.status_code, response.headers.get("content-type", ""), response.content

        return await asyncio.to_thread(request)


provider = OpenAIProvider()
