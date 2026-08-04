"""Local compatibility API for the cookie-free M365 bearer beta.

M365's provider-authored chain-of-thought progress is exposed as a reasoning
summary. It is never presented as raw chain-of-thought and no signature is
invented.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from beta.m365_artifacts import artifact_store
from beta.m365_bearer import (
    BETA_CONFIRM_ENV,
    BetaConfigurationError,
    BetaUpstreamError,
    M365BearerBeta,
)
from beta.m365_conversations import ConversationConflict, coordinator
from beta.m365_dashboard import (
    DASHBOARD_COOKIE,
    DASHBOARD_SESSION_TTL,
    dashboard_html,
    dashboard_request_authorized,
    issue_dashboard_session,
)
from beta.m365_equivalence import equivalence_report
from beta.m365_events import public_event
from beta.m365_files import GraphCredential, M365GraphUploader
from beta.m365_images import M365SubstrateImageUploader
from beta.m365_models import M365ModelCatalog
from beta.m365_oauth import (
    consume as oauth_consume,
)
from beta.m365_oauth import (
    exchange as oauth_exchange,
)
from beta.m365_oauth import (
    import_server_response,
)
from beta.m365_oauth import (
    start as oauth_start,
)
from beta.m365_remote import RemoteAttachmentFetcher
from beta.m365_research import research_report
from beta.m365_telemetry import telemetry
from beta.m365_verification import VERIFICATION_CONTRACT_VERSION, running_commit, safe_latest_verification

API_KEY_ENV = "CODEX_AUTH_M365_BETA_API_KEY"
ADMIN_KEY_ENV = "CODEX_AUTH_M365_BETA_ADMIN_KEY"
MAX_CONVERSATION_TURNS = 64
MAX_COMPILED_PROMPT_CHARACTERS = 200_000
MAX_ADMIN_CREDENTIAL_BYTES = 48_000
ADMIN_MUTATIONS_PER_MINUTE = 6
_admin_mutations: deque[float] = deque()
_admin_mutation_lock = threading.Lock()
DATA_URL_PATTERN = re.compile(
    r"^data:(?P<media_type>[^;,]+)?;base64,(?P<data>.*)$",
    re.DOTALL,
)


def _reasoning_contract() -> dict[str, Any]:
    return {
        "mode": "provider_summary_unsigned",
        "raw_chain_of_thought": False,
        "signature_available": False,
    }


def _estimate_tokens(value: str) -> int:
    """Return a labelled local estimate, never an upstream usage claim."""

    if not value:
        return 0
    return max(1, len(re.findall(r"\w+|[^\w\s]", value, flags=re.UNICODE)))


def _usage_estimate(input_text: str, output_text: str) -> dict[str, Any]:
    input_tokens = _estimate_tokens(input_text)
    output_tokens = _estimate_tokens(output_text)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "source": "local_lexical_estimate",
        "upstream_reported": False,
    }


def _safe_error_phase(exc: Exception) -> str:
    candidate = str(exc)
    if re.fullmatch(r"[A-Za-z0-9_:-]{1,96}", candidate):
        return candidate
    return type(exc).__name__


@dataclass(frozen=True)
class M365ConversationTurn:
    """A preserved compatibility turn before M365 text compilation."""

    role: str
    text: str
    attachment_names: tuple[str, ...] = ()


@dataclass
class PreparedM365Conversation:
    """Structured request IR with an explicit, truthful transport boundary."""

    prompt: str
    attachments: list[Any]
    system_text: str
    turns: tuple[M365ConversationTurn, ...]

    def __iter__(self) -> Iterable[Any]:
        # Preserve the existing two-value unpacking API.
        yield self.prompt
        yield self.attachments

    def safe_status(self) -> dict[str, Any]:
        return {
            "transport_mode": "compiled_structured_transcript",
            "native_structured_history": False,
            "system_instruction_mode": (
                "response_preferences" if self.system_text else "absent"
            ),
            "turn_count": len(self.turns),
            "attachment_count": len(self.attachments),
            "roles": [turn.role for turn in self.turns],
            "history_limits": {
                "max_turns": MAX_CONVERSATION_TURNS,
                "max_compiled_characters": MAX_COMPILED_PROMPT_CHARACTERS,
            },
            "reasoning": _reasoning_contract(),
            "caller_tools": {
                "invocation": "unavailable",
                "historical_tool_results": "compiled_as_context",
            },
        }


def _compile_conversation(
    turns: Iterable[M365ConversationTurn],
    system_text: str = "",
) -> str:
    """Compile preserved turns without using a rejected ``System:`` label."""

    materialized = list(turns)
    sections: list[str] = []
    if system_text.strip():
        sections.extend(
            [
                "Response preferences supplied by the API caller:",
                system_text.strip(),
            ]
        )
    if materialized:
        sections.append("Conversation context:")
    last_user_index = max(
        (
            index
            for index, turn in enumerate(materialized)
            if turn.role.lower() == "user"
        ),
        default=-1,
    )
    for index, turn in enumerate(materialized):
        role = turn.role.strip().lower() or "user"
        if role == "user" and index == last_user_index:
            label = "Current user request"
        elif role == "assistant":
            label = "Earlier assistant message"
        elif role == "user":
            label = "Earlier user message"
        else:
            label = f"Earlier {role} message"
        sections.append(f"{label}: {turn.text.strip()}")
    return "\n\n".join(section for section in sections if section)


def normalize_public_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a provider event into the stable, secret-free beta contract."""

    event_type = event.get("type")
    common = {
        "lane": event.get("lane"),
        "operation": event.get("operation"),
        "elapsed_ms": event.get("elapsed_ms"),
    }
    if event_type == "reasoning_progress":
        return {
            "type": "reasoning_summary_delta",
            "delta": str(event.get("delta") or ""),
            **common,
        }
    if event_type == "text_delta":
        return {
            "type": "text_delta",
            "delta": str(event.get("delta") or ""),
            **common,
        }
    if event_type == "completion":
        return {"type": "completion", "elapsed_ms": event.get("elapsed_ms")}
    if event_type in {
        "progress",
        "search_query",
        "citation",
        "references_complete",
        "generated_code",
        "image_progress",
        "image",
        "adaptive_card",
        "plugin",
        "suggestions",
        "suggestions_detail",
    }:
        return public_event(event)
    return None


@dataclass
class AnthropicStreamEncoder:
    """Translate normalized events to Anthropic Messages SSE objects."""

    model: str
    message_id: str = ""
    started: bool = False
    block_type: str | None = None
    block_index: int = 0
    input_text: str = ""
    output_parts: list[str] | None = None
    artifacts: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.message_id:
            self.message_id = f"msg_{uuid.uuid4().hex}"
        if self.output_parts is None:
            self.output_parts = []
        if self.artifacts is None:
            self.artifacts = []

    def _start_message(self) -> list[dict[str, Any]]:
        if self.started:
            return []
        self.started = True
        return [
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": _estimate_tokens(self.input_text),
                        "output_tokens": 0,
                    },
                    "usage_estimation": {
                        "source": "local_lexical_estimate",
                        "upstream_reported": False,
                    },
                },
            }
        ]

    def _open_block(self, block_type: str) -> list[dict[str, Any]]:
        output = self._start_message()
        if self.block_type == block_type:
            return output
        if self.block_type is not None:
            output.append({"type": "content_block_stop", "index": self.block_index})
            self.block_index += 1
        self.block_type = block_type
        content_block = (
            {"type": "thinking", "thinking": ""}
            if block_type == "thinking"
            else {"type": "text", "text": ""}
        )
        output.append(
            {
                "type": "content_block_start",
                "index": self.block_index,
                "content_block": content_block,
            }
        )
        return output

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        event_type = event.get("type")
        delta = str(event.get("delta") or "")
        if event_type == "image" and event.get("artifact_id"):
            image = artifact_store.image_block(str(event["artifact_id"]))
            if image:
                output = self._start_message()
                if self.block_type is not None:
                    output.append({"type": "content_block_stop", "index": self.block_index})
                    self.block_index += 1
                    self.block_type = None
                output.append({"type": "content_block_start", "index": self.block_index, "content_block": image})
                output.append({"type": "content_block_stop", "index": self.block_index})
                self.block_index += 1
                return output
        if event_type not in {"reasoning_summary_delta", "text_delta", "completion"}:
            self.artifacts.append(public_event(event))
            return []
        if not delta:
            return []
        if event_type == "reasoning_summary_delta":
            self.output_parts.append(delta)
            output = self._open_block("thinking")
            output.append(
                {
                    "type": "content_block_delta",
                    "index": self.block_index,
                    "delta": {"type": "thinking_delta", "thinking": delta},
                }
            )
            return output
        if event_type == "text_delta":
            self.output_parts.append(delta)
            output = self._open_block("text")
            output.append(
                {
                    "type": "content_block_delta",
                    "index": self.block_index,
                    "delta": {"type": "text_delta", "text": delta},
                }
            )
            return output
        return []

    def finish(self) -> list[dict[str, Any]]:
        output = self._start_message()
        if self.block_type is not None:
            output.append({"type": "content_block_stop", "index": self.block_index})
            self.block_type = None
        output.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "provider_metadata": {
                        "m365": {
                            "artifacts": self.artifacts,
                            "reasoning": _reasoning_contract(),
                        }
                    },
                    "usage": {
                        "output_tokens": _estimate_tokens(
                            "".join(self.output_parts)
                        ),
                        "source": "local_lexical_estimate",
                        "upstream_reported": False,
                    },
                },
                {"type": "message_stop"},
            ]
        )
        return output


@dataclass
class OpenAIStreamEncoder:
    """Translate normalized events to OpenAI-compatible chunk objects."""

    model: str
    completion_id: str = ""
    created: int = 0
    input_text: str = ""
    output_parts: list[str] | None = None
    artifacts: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.completion_id:
            self.completion_id = f"chatcmpl_{uuid.uuid4().hex}"
        if not self.created:
            self.created = int(time.time())
        if self.output_parts is None:
            self.output_parts = []
        if self.artifacts is None:
            self.artifacts = []

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        delta = str(event.get("delta") or "")
        if event.get("type") not in {"reasoning_summary_delta", "text_delta", "completion"}:
            self.artifacts.append(public_event(event))
            return []
        if not delta:
            return []
        if event.get("type") == "reasoning_summary_delta":
            self.output_parts.append(delta)
            return [self._chunk({"reasoning_content": delta})]
        if event.get("type") == "text_delta":
            self.output_parts.append(delta)
            return [self._chunk({"content": delta})]
        return []

    def finish(self) -> list[dict[str, Any]]:
        chunk = self._chunk({}, "stop")
        chunk["usage"] = {
            "prompt_tokens": _estimate_tokens(self.input_text),
            "completion_tokens": _estimate_tokens("".join(self.output_parts)),
            "total_tokens": _estimate_tokens(self.input_text)
            + _estimate_tokens("".join(self.output_parts)),
        }
        chunk["usage_details"] = {
            "source": "local_lexical_estimate",
            "upstream_reported": False,
        }
        chunk["provider_metadata"] = {
            "m365": {
                "artifacts": self.artifacts,
                "reasoning": _reasoning_contract(),
            }
        }
        return [chunk]


def _text_blocks(value: Any, *, context: str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise BetaConfigurationError(f"{context} must be text")
    parts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            raise BetaConfigurationError(f"{context} contains an invalid block")
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text") or ""))
        elif block_type == "thinking":
            # Historical summaries can be retained as plain context. Signatures
            # are deliberately ignored because M365 does not issue them.
            parts.append(str(block.get("thinking") or ""))
        elif block_type == "tool_result":
            result = _text_blocks(
                block.get("content") or "",
                context=f"{context} tool_result",
            )
            tool_id = str(block.get("tool_use_id") or "unknown")[:128]
            parts.append(f"Tool result ({tool_id}):\n{result}")
        else:
            raise BetaConfigurationError(
                f"{context} block type '{block_type}' is not supported by the M365 beta"
            )
    return "\n".join(part for part in parts if part)


def _validate_prepared_conversation(
    turns: list[M365ConversationTurn], prompt: str
) -> None:
    if len(turns) > MAX_CONVERSATION_TURNS:
        raise BetaConfigurationError(
            f"messages exceed the {MAX_CONVERSATION_TURNS}-turn deployment limit"
        )
    if len(prompt) > MAX_COMPILED_PROMPT_CHARACTERS:
        raise BetaConfigurationError(
            "compiled conversation exceeds the deployment character limit"
        )


def _prompt_from_messages(messages: Any, system: Any = None) -> str:
    if not isinstance(messages, list) or not messages:
        raise BetaConfigurationError("messages must be a non-empty array")
    turns: list[M365ConversationTurn] = []
    system_text = ""
    if system is not None:
        system_text = _text_blocks(system, context="system")
    for message in messages:
        if not isinstance(message, dict):
            raise BetaConfigurationError("each message must be an object")
        role = str(message.get("role") or "").strip()
        content = message.get("content")
        text = _text_blocks(content, context="message content")
        if text.strip():
            turns.append(
                M365ConversationTurn(role=role or "user", text=text.strip())
            )
    if not turns and not system_text.strip():
        raise BetaConfigurationError("messages contain no text")
    return _compile_conversation(turns, system_text)


def _prepare_messages(
    messages: Any,
    system: Any = None,
    *,
    uploader: M365GraphUploader | None = None,
    image_uploader: M365SubstrateImageUploader | None = None,
    remote_fetcher: RemoteAttachmentFetcher | None = None,
) -> PreparedM365Conversation:
    """Build text plus proven Graph-to-SignalR attachment annotations."""

    if not isinstance(messages, list) or not messages:
        raise BetaConfigurationError("messages must be a non-empty array")
    turns: list[M365ConversationTurn] = []
    attachments: list[Any] = []
    active_uploader = uploader
    active_image_uploader = image_uploader
    active_remote_fetcher = remote_fetcher
    system_text = ""
    if system is not None:
        system_text = _text_blocks(system, context="system")
    for message in messages:
        if not isinstance(message, dict):
            raise BetaConfigurationError("each message must be an object")
        role = str(message.get("role") or "user").strip().lower()
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    raise BetaConfigurationError(
                        "message content contains an invalid block"
                    )
                block_type = str(block.get("type") or "")
                if block_type == "text":
                    text_parts.append(str(block.get("text") or ""))
                    continue
                if block_type == "thinking":
                    text_parts.append(str(block.get("thinking") or ""))
                    continue
                if block_type == "tool_result":
                    result = _text_blocks(
                        block.get("content") or "",
                        context="message tool_result",
                    )
                    tool_id = str(block.get("tool_use_id") or "unknown")[:128]
                    text_parts.append(f"Tool result ({tool_id}):\n{result}")
                    continue
                if block_type not in {
                    "image",
                    "document",
                    "file",
                    "image_url",
                    "file_url",
                    "input_image",
                    "input_file",
                }:
                    raise BetaConfigurationError(
                        f"message content block type '{block_type}' is not supported"
                    )
                if role != "user":
                    raise BetaConfigurationError(
                        "attachments are supported only in user messages"
                    )
                source = block.get("source")
                media_type = "application/octet-stream"
                name = ""
                data: bytes | None = None
                if isinstance(source, dict) and source.get("type") == "base64":
                    encoded = str(source.get("data") or "")
                    media_type = str(
                        source.get("media_type") or "application/octet-stream"
                    )
                    name = str(source.get("name") or block.get("name") or "")
                else:
                    if isinstance(source, dict) and source.get("type") == "url":
                        url_value = source.get("url")
                        name = str(
                            source.get("name") or block.get("name") or ""
                        )
                    else:
                        url_value = (
                            block.get("image_url")
                            or block.get("file_url")
                            or block.get("image")
                            or block.get("file")
                        )
                    if isinstance(url_value, dict):
                        name = str(
                            url_value.get("name")
                            or url_value.get("filename")
                            or block.get("name")
                            or ""
                        )
                        url_value = url_value.get("url") or url_value.get(
                            "file_data"
                        )
                    data_url = str(url_value or "")
                    match = DATA_URL_PATTERN.fullmatch(data_url)
                    if match is None:
                        if active_remote_fetcher is None:
                            active_remote_fetcher = RemoteAttachmentFetcher()
                        remote = active_remote_fetcher.fetch(
                            data_url,
                            name=name or str(block.get("name") or ""),
                        )
                        data = remote.content
                        media_type = remote.mime_type
                        name = remote.name
                    else:
                        media_type = (
                            match.group("media_type") or "application/octet-stream"
                        )
                        encoded = match.group("data")
                if data is None:
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise BetaConfigurationError(
                            "attachment base64 data is invalid"
                        ) from exc
                suffix = mimetypes.guess_extension(media_type) or ".bin"
                name = str(
                    name
                    or block.get("name")
                    or f"attachment-{index + 1}{suffix}"
                )
                if media_type.startswith("image/"):
                    if active_image_uploader is None:
                        active_image_uploader = (
                            M365SubstrateImageUploader.from_directory()
                        )
                    shared_conversation_id = next(
                        (
                            attachment.conversation_id
                            for attachment in attachments
                            if attachment.conversation_id
                        ),
                        None,
                    )
                    staged = active_image_uploader.upload_bytes(
                        name=name,
                        content=data,
                        mime_type=media_type,
                        conversation_id=shared_conversation_id,
                    )
                else:
                    if active_uploader is None:
                        active_uploader = M365GraphUploader.from_directory(
                            acquire_if_needed=True
                        )
                    staged = active_uploader.stage_attachment(
                        name=name,
                        content=data,
                        mime_type=media_type,
                    )
                attachments.append(staged)
                text_parts.append(f"[Attached file: {staged.name}]")
            text = "\n".join(part for part in text_parts if part)
        else:
            raise BetaConfigurationError("message content must be text or blocks")
        if role in {"system", "developer"}:
            if text.strip():
                prefix = "Developer instruction" if role == "developer" else "System instruction"
                system_text = "\n\n".join(
                    part for part in (system_text, f"{prefix}:\n{text.strip()}") if part
                )
            continue
        if role == "tool":
            tool_name = str(message.get("name") or message.get("tool_call_id") or "unknown")[:128]
            text = f"Tool result ({tool_name}):\n{text}"
        if text.strip():
            turns.append(
                M365ConversationTurn(
                    role=role,
                    text=text.strip(),
                    attachment_names=tuple(
                        attachment.name
                        for attachment in attachments
                        if attachment.name in text
                    ),
                )
            )
    if not turns and not system_text.strip():
        raise BetaConfigurationError("messages contain no content")
    prompt = _compile_conversation(turns, system_text)
    _validate_prepared_conversation(turns, prompt)
    return PreparedM365Conversation(
        prompt=prompt,
        attachments=attachments,
        system_text=system_text.strip(),
        turns=tuple(turns),
    )


async def _provider_events(
    prompt: str,
    model: str,
    attachments: list[Any] | None = None,
    conversation_token: dict[str, str] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    if os.environ.get(BETA_CONFIRM_ENV) != "1":
        raise BetaConfigurationError(
            f"set {BETA_CONFIRM_ENV}=1 before using the live beta API"
        )
    beta = M365BearerBeta.from_directory()
    catalog = M365ModelCatalog.from_directory()
    resolved = catalog.resolve(model)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        normalized = normalize_public_event(event)
        if normalized is not None:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", normalized))

    def run() -> None:
        started = time.monotonic()
        try:
            answer = beta.generate_stream(
                prompt,
                emit,
                resolved.canonical_id,
                resolved.model.tone,
                attachments,
                conversation_id=(conversation_token or {}).get("upstream_id"),
            )
            telemetry.record(
                "generation_completed",
                status="succeeded",
                model=resolved.canonical_id,
                transport="signalr",
                stream=True,
                duration_ms=round((time.monotonic() - started) * 1000),
                input_characters=len(prompt),
                output_characters=len(answer),
                attachment_count=len(attachments or []),
            )
        except Exception as exc:
            telemetry.record(
                "generation_failed",
                status="failed",
                model=resolved.canonical_id,
                transport="signalr",
                stream=True,
                duration_ms=round((time.monotonic() - started) * 1000),
                input_characters=len(prompt),
                attachment_count=len(attachments or []),
                error_phase=_safe_error_phase(exc),
            )
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

    worker = threading.Thread(target=run, name="m365-beta-stream", daemon=True)
    worker.start()
    while True:
        item_type, value = await queue.get()
        if item_type == "event":
            yield value
        elif item_type == "error":
            if conversation_token is not None:
                coordinator.fail(conversation_token)
            if isinstance(value, (BetaConfigurationError, BetaUpstreamError)):
                raise value
            raise BetaUpstreamError("compatibility_stream_failed") from value
        else:
            break
    if conversation_token is not None:
        coordinator.complete(
            conversation_token,
            result={"state": "completed"},
        )


def _call_provider_events(
    prompt: str, model: str, attachments: list[Any] | None, conversation_token: dict[str, str] | None
) -> AsyncGenerator[dict[str, Any], None]:
    """Keep existing local test hooks compatible with the added context arg."""

    if "conversation_token" in inspect.signature(_provider_events).parameters:
        return _provider_events(prompt, model, attachments, conversation_token)
    return _provider_events(prompt, model, attachments)


def _anthropic_sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def _openai_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def _responses_sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


async def anthropic_event_stream(
    prompt: str,
    model: str,
    attachments: list[Any] | None = None,
    conversation_token: dict[str, str] | None = None,
) -> AsyncGenerator[str, None]:
    encoder = AnthropicStreamEncoder(model, input_text=prompt)
    emitted = False
    try:
        async for event in _call_provider_events(prompt, model, attachments, conversation_token):
            for output in encoder.feed(event):
                emitted = True
                yield _anthropic_sse(output)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        if conversation_token is not None:
            coordinator.fail(conversation_token)
        if not emitted:
            raise
        error = {
            "type": "error",
            "error": {"type": "upstream_error", "message": str(exc)},
        }
        yield _anthropic_sse(error)
        return
    if conversation_token is not None:
        coordinator.complete(conversation_token, result={"state": "completed"})
    for output in encoder.finish():
        yield _anthropic_sse(output)


async def openai_event_stream(
    prompt: str,
    model: str,
    attachments: list[Any] | None = None,
    conversation_token: dict[str, str] | None = None,
) -> AsyncGenerator[str, None]:
    encoder = OpenAIStreamEncoder(model, input_text=prompt)
    emitted = False
    try:
        async for event in _call_provider_events(prompt, model, attachments, conversation_token):
            for output in encoder.feed(event):
                emitted = True
                yield _openai_sse(output)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        if conversation_token is not None:
            coordinator.fail(conversation_token)
        if not emitted:
            raise
        error = {"error": {"type": "upstream_error", "message": str(exc)}}
        yield _openai_sse(error)
        yield "data: [DONE]\n\n"
        return
    if conversation_token is not None:
        coordinator.complete(conversation_token, result={"state": "completed"})
    for output in encoder.finish():
        yield _openai_sse(output)
    yield "data: [DONE]\n\n"


async def responses_event_stream(
    prompt: str,
    model: str,
    attachments: list[Any] | None = None,
    conversation_token: dict[str, str] | None = None,
) -> AsyncGenerator[str, None]:
    """Translate M365 lanes to the public Responses streaming vocabulary."""

    response_id = f"resp_{uuid.uuid4().hex}"
    sequence = 0

    def envelope(event_type: str, **fields: Any) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        return {"type": event_type, "sequence_number": sequence, "response_id": response_id, **fields}

    yield _responses_sse(envelope("response.created", response={"id": response_id, "object": "response", "status": "in_progress", "model": model}))
    try:
        async for event in _call_provider_events(prompt, model, attachments, conversation_token):
            event_type = event.get("type")
            delta = str(event.get("delta") or "")
            if event_type == "reasoning_summary_delta" and delta:
                yield _responses_sse(envelope("response.reasoning_summary_text.delta", delta=delta, output_index=0, summary_index=0))
            elif event_type == "text_delta" and delta:
                yield _responses_sse(envelope("response.output_text.delta", delta=delta, output_index=1, content_index=0))
    except (BetaConfigurationError, BetaUpstreamError):
        if conversation_token is not None:
            coordinator.fail(conversation_token)
        raise
    if conversation_token is not None:
        coordinator.complete(conversation_token, result={"state": "completed"})
    yield _responses_sse(envelope("response.completed", response={"id": response_id, "object": "response", "status": "completed", "model": model, "provider_metadata": {"m365": {"conversation": coordinator.public_metadata(conversation_token) if conversation_token else None, "reasoning": _reasoning_contract()}}}))


async def _preflight_stream(
    stream: AsyncGenerator[str, None],
) -> AsyncGenerator[str, None]:
    """Pull one event before HTTP headers are committed."""

    try:
        first = await anext(stream)
    except StopAsyncIteration as exc:
        raise BetaUpstreamError("compatibility_stream_empty") from exc

    async def replay() -> AsyncGenerator[str, None]:
        yield first
        async for item in stream:
            yield item

    return replay()


def _collect_content(events: Iterable[dict[str, Any]]) -> tuple[str, str]:
    reasoning: list[str] = []
    text: list[str] = []
    for event in events:
        if event.get("type") == "reasoning_summary_delta":
            reasoning.append(str(event.get("delta") or ""))
        elif event.get("type") == "text_delta":
            text.append(str(event.get("delta") or ""))
    return "".join(reasoning), "".join(text)


app = FastAPI(title="M365 bearer beta compatibility API")
_beta_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets"))
if os.path.isdir(_beta_assets):
    app.mount("/assets", StaticFiles(directory=_beta_assets), name="beta-assets")


@app.middleware("http")
async def optional_api_key_guard(request: Request, call_next: Any) -> Any:
    path = request.url.path
    dashboard_protected = path.startswith("/dashboard/api/") or path == "/dashboard/logout"
    if dashboard_protected and not dashboard_request_authorized(request):
        return JSONResponse(status_code=401, content={"error": "dashboard_session_required"})
    expected = os.environ.get(API_KEY_ENV)
    if expected and path.startswith("/v1/"):
        authorization = request.headers.get("authorization", "")
        supplied = request.headers.get("x-api-key", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "Invalid or missing API key",
                    },
                },
            )
    is_admin_mutation = (
        (path.startswith("/admin/") and request.method != "GET")
        or path == "/refresh-token"
    )
    is_dashboard_mutation = path.startswith("/dashboard/api/") and request.method != "GET"
    if is_admin_mutation or is_dashboard_mutation:
        expected_admin = os.environ.get(ADMIN_KEY_ENV) or os.environ.get(API_KEY_ENV)
        if not expected_admin:
            return JSONResponse(status_code=503, content={"error": "credential administration is not configured"})
        if is_admin_mutation:
            supplied_admin = request.headers.get("x-admin-key", "")
            if not secrets.compare_digest(supplied_admin, expected_admin):
                return JSONResponse(status_code=401, content={"error": "invalid admin key"})
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                too_large = int(content_length) > MAX_ADMIN_CREDENTIAL_BYTES
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid credential import length"})
            if too_large:
                return JSONResponse(status_code=413, content={"error": "credential import is too large"})
        # Do not trust a missing or forged Content-Length header. Starlette
        # caches this body for the JSON parser used by the endpoint.
        if len(await request.body()) > MAX_ADMIN_CREDENTIAL_BYTES:
            return JSONResponse(status_code=413, content={"error": "credential import is too large"})
        with _admin_mutation_lock:
            now = time.monotonic()
            while _admin_mutations and now - _admin_mutations[0] > 60:
                _admin_mutations.popleft()
            if len(_admin_mutations) >= ADMIN_MUTATIONS_PER_MINUTE:
                return JSONResponse(status_code=429, content={"error": "admin mutation rate limit exceeded"})
            _admin_mutations.append(now)
    return await call_next(request)


def _unsupported_request_feature(request: dict[str, Any]) -> str | None:
    if request.get("tools"):
        return "client function tools"
    tool_choice = request.get("tool_choice")
    if tool_choice is not None and tool_choice != "none":
        return "tool_choice"
    unsupported_controls = [
        name
        for name in ("temperature", "top_p", "top_k", "stop_sequences", "max_tokens", "thinking")
        if request.get(name) is not None
    ]
    if unsupported_controls:
        return ", ".join(unsupported_controls)
    return None


def _unsupported_response(feature: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "type": "error",
            "error": {
                "type": "unsupported_feature_error",
                "message": (
                    f"{feature} is not mapped to a proven M365 upstream contract"
                ),
            },
        },
    )


def _conversation_token(
    payload: dict[str, Any],
    prepared: PreparedM365Conversation,
    http_request: Request = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Resolve an opaque proxy key; prompts only enter the HMAC, never state."""

    explicit = payload.get("conversation") or payload.get("previous_response_id")
    if isinstance(explicit, dict):
        explicit = explicit.get("id")
    if not explicit and http_request is not None:
        explicit = http_request.headers.get("x-codex-conversation-id")
    if not explicit and http_request is None:
        # Direct Python callers have no request boundary on which to base
        # continuity; avoid coupling unrelated unit/in-process calls.
        explicit = f"direct:{uuid.uuid4().hex}"
    first_user = next((turn.text for turn in prepared.turns if turn.role == "user"), prepared.prompt)
    return coordinator.acquire(
        explicit_id=str(explicit) if explicit else None,
        first_user_text=first_user,
        request_text=prepared.prompt,
        turn_hashes=tuple(
            hashlib.sha256(f"{turn.role}\0{turn.text}".encode()).hexdigest()
            for turn in prepared.turns
        ),
        model_id=model,
    )


def _continuation_prompt(
    prepared: PreparedM365Conversation, token: dict[str, Any]
) -> str:
    """Send only appended turns when the upstream conversation already exists."""

    delta_start = int(token.get("delta_start") or 0)
    continuity = token.get("continuity")
    if continuity not in {"continued", "rolled_over", "forked", "model_switched"} or delta_start <= 0:
        return prepared.prompt
    appended = prepared.turns[delta_start:]
    if not appended:
        raise ConversationConflict("conversation_request_already_completed")
    return _compile_conversation(
        appended,
        prepared.system_text
        if continuity in {"rolled_over", "forked", "model_switched"}
        else "",
    )


def _responses_messages(value: Any) -> list[dict[str, Any]]:
    """Normalize the supported Responses input subset into compatibility turns."""

    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        raise BetaConfigurationError("Responses input must be text or an input-item array")
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BetaConfigurationError("Responses input contains an invalid item")
        item_type = str(item.get("type") or "message")
        if item_type not in {"message", "input_text"}:
            raise BetaConfigurationError(f"Responses input type '{item_type}' is not supported")
        role = str(item.get("role") or "user")
        content = item.get("content")
        if item_type == "input_text":
            content = item.get("text")
        if isinstance(content, list):
            converted: list[dict[str, Any]] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"input_text", "output_text"}:
                    converted.append({"type": "text", "text": str(block.get("text") or "")})
                else:
                    converted.append(block)
            content = converted
        messages.append({"role": role, "content": content})
    return messages


@app.head("/")
def root_head() -> Response:
    """Provide a side-effect-free hosting and uptime probe."""

    return Response(status_code=200)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "M365 bearer beta compatibility API",
        "provider": "m365-copilot",
        "scope": "local beta",
        "endpoints": [
            "/dashboard",
            "/health",
            "/account-limits",
            "/refresh-token",
            "/v1/deployment-readiness",
            "/v1/capabilities",
            "/v1/research",
            "/v1/metrics",
            "/v1/logs",
            "/v1/logs/stream",
            "/v1/models",
            "/v1/messages",
            "/v1/chat/completions",
            "/v1/responses",
        ],
    }


@app.get("/admin/credentials", response_class=HTMLResponse)
def credential_admin_page() -> str:
    """Keep the former credential URL as an alias for the beta dashboard."""

    return dashboard_html()


@app.post("/admin/credentials/import")
def import_credentials(payload: dict[str, Any]) -> Any:
    try:
        value = payload.get("credential")
        if not isinstance(value, dict):
            raise BetaConfigurationError("credential must be a JSON object")
        beta = M365BearerBeta.from_directory()
        state = beta.replace_credential(value)
        return {"status": "ok", "provider": "m365-copilot", "credential": state, "secrets_returned": False}
    except BetaConfigurationError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})
    except BetaUpstreamError as exc:
        return JSONResponse(status_code=502, content={"status": "error", "error": str(exc)})


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        return {
            "status": "ok",
            "provider": "m365-copilot",
            **M365BearerBeta.from_directory().status(),
            "catalog": M365ModelCatalog.from_directory().safe_status(),
            "build": {
                "render_commit": running_commit(),
                "verification_contract": VERIFICATION_CONTRACT_VERSION,
            },
        }
    except BetaConfigurationError:
        return {"status": "not_configured", "provider": "m365-copilot", "cookie_count": 0}


@app.get("/v1/deployment-readiness")
def deployment_readiness() -> dict[str, Any]:
    """Report operational boundaries without returning credential material."""

    try:
        beta = M365BearerBeta.from_directory()
        credential = beta.status()
    except BetaConfigurationError:
        return {
            "ready": False,
            "provider": "m365-copilot",
            "generation": "not_configured",
            "file_input": "not_configured",
        }
    try:
        graph = GraphCredential.from_beta_record(beta.credential.raw)
        graph_state = "active"
        graph_expires_in = (
            max(0, round(graph.expires_at - time.time()))
            if graph.expires_at is not None
            else None
        )
    except BetaConfigurationError:
        graph_state = (
            "reacquirable_from_generation_refresh"
            if credential["refresh_ready"]
            else "profile_connection_required"
        )
        graph_expires_in = None
    persistence = credential["credential_persistence"]
    warnings: list[str] = []
    if not persistence["restart_durable"]:
        warnings.append(
            "rotated credentials are not guaranteed to survive a process restart"
        )
    if graph_state != "active":
        warnings.append("file input requires a Graph resource token acquisition")
    return {
        "ready": bool(
            credential["generation_ready"]
            and credential["refresh_ready"]
            and persistence["restart_durable"]
        ),
        "provider": "m365-copilot",
        "single_account": True,
        "generation": credential["state"],
        "refresh_ready": credential["refresh_ready"],
        "credential_persistence": persistence,
        "file_input": graph_state,
        "graph_expires_in_seconds": graph_expires_in,
        "reasoning": _reasoning_contract(),
        "history_transport": "compiled_structured_transcript",
        "caller_tool_invocation": "unavailable",
        "historical_tool_results": "compiled_as_context",
        "warnings": warnings,
    }


@app.get("/v1/models")
def models() -> dict[str, Any]:
    try:
        return M365ModelCatalog.from_directory().api_list()
    except BetaConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/v1/models/{model_id}")
def model(model_id: str) -> Any:
    try:
        return M365ModelCatalog.from_directory().api_get(model_id)
    except BetaConfigurationError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "type": "error",
                "error": {"type": "not_found_error", "message": str(exc)},
            },
        )


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return equivalence_report()


@app.get("/v1/verification")
def verification() -> dict[str, Any]:
    """Return only the current-build verification digest and safe counters."""

    return {
        "provider": "m365-copilot",
        "build": {
            "render_commit": running_commit(),
            "verification_contract": VERIFICATION_CONTRACT_VERSION,
        },
        "verification": safe_latest_verification(),
    }


@app.get("/v1/research")
def research() -> dict[str, Any]:
    return research_report()


@app.get("/v1/metrics")
def metrics() -> dict[str, Any]:
    return telemetry.summary()


@app.get("/v1/logs")
def logs(limit: int = 100) -> dict[str, Any]:
    return {
        "source": "redacted_local_jsonl",
        "events": telemetry.recent(limit),
    }


@app.get("/v1/logs/stream")
def logs_stream() -> StreamingResponse:
    """Stream new redacted operational events and bounded heartbeats."""

    async def events() -> AsyncGenerator[str, None]:
        delivered = 0
        while True:
            recent = telemetry.recent(200)
            if delivered > len(recent):
                delivered = 0
            for event in recent[delivered:]:
                yield f"event: telemetry\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
            delivered = len(recent)
            yield "event: heartbeat\ndata: {}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/account-limits")
def account_limits() -> dict[str, Any]:
    try:
        beta = M365BearerBeta.from_directory()
        catalog = M365ModelCatalog.from_directory()
        return {
            "provider": "m365-copilot",
            "accounts": 1,
            "credential": beta.status(),
            "catalog": catalog.safe_status(),
            "quota": {
                "state": "unavailable",
                "reason": "no confirmed M365 bearer quota endpoint",
            },
        }
    except BetaConfigurationError:
        return {
            "provider": "m365-copilot",
            "accounts": 0,
            "credential": {"state": "not_configured", "cookie_count": 0},
            "quota": {"state": "unavailable"},
        }


@app.post("/refresh-token")
def refresh_token() -> Any:
    try:
        return {
            "status": "ok",
            "provider": "m365-copilot",
            "credential": M365BearerBeta.from_directory().refresh(),
        }
    except BetaConfigurationError as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": str(exc)},
        )
    except BetaUpstreamError as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": str(exc)},
        )


@app.post("/v1/messages/count_tokens")
def count_tokens() -> JSONResponse:
    # This intentionally matches Antigravity 2.7.7: neither proxy has an
    # authoritative upstream tokenizer on this endpoint.
    return JSONResponse(
        status_code=501,
        content={
            "type": "error",
            "error": {
                "type": "not_implemented",
                "message": "Token counting is not implemented for the M365 beta.",
            },
        },
    )


@app.post("/v1/messages")
async def anthropic_messages(request: dict[str, Any], http_request: Request = None) -> Any:
    unsupported = _unsupported_request_feature(request)
    if unsupported:
        return _unsupported_response(unsupported)
    try:
        prepared = _prepare_messages(
            request.get("messages"),
            request.get("system"),
        )
        prompt, attachments = prepared
    except BetaConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BetaUpstreamError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "type": "error",
                "error": {"type": "upstream_error", "message": str(exc)},
            },
        )
    model = str(request.get("model") or "auto")
    try:
        conversation_token = _conversation_token(request, prepared, http_request, model)
        prompt = _continuation_prompt(prepared, conversation_token)
    except ConversationConflict as exc:
        return JSONResponse(status_code=409, content={"type": "error", "error": {"type": "conflict_error", "message": str(exc)}})
    if request.get("stream"):
        try:
            stream = await _preflight_stream(
                anthropic_event_stream(prompt, model, attachments, conversation_token)
            )
        except (BetaConfigurationError, BetaUpstreamError) as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "type": "error",
                    "error": {"type": "upstream_error", "message": str(exc)},
                },
            )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Codex-Conversation-ID": conversation_token["proxy_id"],
            },
        )
    events: list[dict[str, Any]] = []
    try:
        async for event in _call_provider_events(prompt, model, attachments, conversation_token):
            events.append(event)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
        )
    reasoning, text = _collect_content(events)
    content: list[dict[str, Any]] = []
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    if text:
        content.append({"type": "text", "text": text})
    usage = _usage_estimate(prompt, reasoning + text)
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
        "usage_estimation": {
            "source": usage["source"],
            "upstream_reported": usage["upstream_reported"],
        },
        "provider_metadata": {**prepared.safe_status(), "conversation": coordinator.public_metadata(conversation_token)},
    }


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: dict[str, Any], http_request: Request = None) -> Any:
    unsupported = _unsupported_request_feature(request)
    if unsupported:
        return _unsupported_response(unsupported)
    try:
        prepared = _prepare_messages(request.get("messages"))
        prompt, attachments = prepared
    except BetaConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BetaUpstreamError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": {"type": "upstream_error", "message": str(exc)}
            },
        )
    model = str(request.get("model") or "auto")
    try:
        conversation_token = _conversation_token(request, prepared, http_request, model)
        prompt = _continuation_prompt(prepared, conversation_token)
    except ConversationConflict as exc:
        return JSONResponse(status_code=409, content={"error": {"type": "conflict_error", "message": str(exc)}})
    if request.get("stream"):
        try:
            stream = await _preflight_stream(
                openai_event_stream(prompt, model, attachments, conversation_token)
            )
        except (BetaConfigurationError, BetaUpstreamError) as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": {"type": "upstream_error", "message": str(exc)}
                },
            )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Codex-Conversation-ID": conversation_token["proxy_id"],
            },
        )
    events: list[dict[str, Any]] = []
    try:
        async for event in _call_provider_events(prompt, model, attachments, conversation_token):
            events.append(event)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "upstream_error", "message": str(exc)}},
        )
    reasoning, text = _collect_content(events)
    usage = _usage_estimate(prompt, reasoning + text)
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                    "reasoning_content": reasoning or None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
        },
        "usage_details": {
            "source": usage["source"],
            "upstream_reported": usage["upstream_reported"],
        },
        "provider_metadata": {**prepared.safe_status(), "conversation": coordinator.public_metadata(conversation_token)},
    }


@app.post("/v1/responses")
async def openai_responses(request: dict[str, Any], http_request: Request = None) -> Any:
    unsupported = _unsupported_request_feature(request)
    if unsupported:
        return _unsupported_response(unsupported)
    try:
        prepared = _prepare_messages(
            _responses_messages(request.get("input")),
            request.get("instructions"),
        )
        prompt, attachments = prepared
        model = str(request.get("model") or "auto")
        conversation_token = _conversation_token(request, prepared, http_request, model)
        prompt = _continuation_prompt(prepared, conversation_token)
    except ConversationConflict as exc:
        return JSONResponse(status_code=409, content={"error": {"type": "conflict_error", "message": str(exc)}})
    except BetaConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    model = str(request.get("model") or "auto")
    if request.get("stream"):
        try:
            stream = await _preflight_stream(
                responses_event_stream(prompt, model, attachments, conversation_token)
            )
        except (BetaConfigurationError, BetaUpstreamError) as exc:
            return JSONResponse(status_code=502, content={"error": {"type": "upstream_error", "message": str(exc)}})
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Codex-Conversation-ID": conversation_token["proxy_id"],
            },
        )

    events: list[dict[str, Any]] = []
    try:
        async for event in _call_provider_events(prompt, model, attachments, conversation_token):
            events.append(event)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        coordinator.fail(conversation_token)
        return JSONResponse(status_code=502, content={"error": {"type": "upstream_error", "message": str(exc)}})
    reasoning, text = _collect_content(events)
    output: list[dict[str, Any]] = []
    if reasoning:
        output.append({"id": f"rs_{uuid.uuid4().hex}", "type": "reasoning", "summary": [{"type": "summary_text", "text": reasoning}]})
    output.append({"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    usage = _usage_estimate(prompt, reasoning + text)
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"], "total_tokens": usage["total_tokens"]},
        "provider_metadata": {"m365": {**prepared.safe_status(), "conversation": coordinator.public_metadata(conversation_token), "reasoning": _reasoning_contract()}},
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    """Render the operator dashboard shell without embedding runtime data."""

    return dashboard_html()


@app.post("/dashboard/login")
async def dashboard_login(request: Request) -> Any:
    if len(await request.body()) > 4_096:
        return JSONResponse(status_code=413, content={"error": "login_request_too_large"})
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        payload = {}
    expected = os.environ.get(ADMIN_KEY_ENV) or os.environ.get(API_KEY_ENV) or ""
    supplied = str(payload.get("admin_key") or "") if isinstance(payload, dict) else ""
    if not expected or not secrets.compare_digest(supplied, expected):
        return JSONResponse(status_code=401, content={"error": "invalid_admin_key"})
    response = JSONResponse({"status": "ok", "session_expires_in_seconds": DASHBOARD_SESSION_TTL})
    response.set_cookie(
        DASHBOARD_COOKIE,
        issue_dashboard_session(),
        max_age=DASHBOARD_SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/dashboard/logout")
def dashboard_logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(DASHBOARD_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


def _dashboard_overview() -> dict[str, Any]:
    try:
        credential = M365BearerBeta.from_directory().status()
    except BetaConfigurationError:
        credential = {
            "state": "not_configured",
            "cookie_count": 0,
            "generation_ready": False,
            "refresh_ready": False,
            "credential_persistence": {
                "source": "unconfigured",
                "restart_durable": False,
            },
        }
    try:
        catalog = M365ModelCatalog.from_directory().api_list()
    except BetaConfigurationError:
        catalog = {"object": "list", "data": [], "source": "unconfigured"}
    return {
        "provider": "m365-copilot",
        "credential": credential,
        "readiness": deployment_readiness(),
        "models": catalog,
        "capabilities": equivalence_report(),
        "verification": verification(),
        "metrics": telemetry.summary(),
        "build": {
            "render_commit": running_commit(),
            "verification_contract": VERIFICATION_CONTRACT_VERSION,
        },
        "authentication": {
            "browser_sign_in_url": "https://m365.cloud.microsoft/chat?auth=2",
            "hosted_oauth": {
                "available": bool(
                    os.environ.get("CODEX_AUTH_M365_BETA_OAUTH_CLIENT_ID")
                    and os.environ.get("CODEX_AUTH_M365_BETA_OAUTH_CLIENT_SECRET")
                ),
                "callback_path": "/dashboard/oauth/callback",
                "state": "configured" if (
                    os.environ.get("CODEX_AUTH_M365_BETA_OAUTH_CLIENT_ID")
                    and os.environ.get("CODEX_AUTH_M365_BETA_OAUTH_CLIENT_SECRET")
                ) else "blocked_by_upstream",
                "secrets_returned": False,
            },
            "direct_device_code": {
                "available": False,
                "reason": "microsoft_first_party_clients_reject_device_code",
            },
            "oauth_json_import": {
                "available": True,
                "required_fields": [
                    "token_type", "access_token", "refresh_token", "expires_in", "scope", "id_token",
                ],
            },
        },
    }


@app.get("/dashboard/api/overview")
def dashboard_overview() -> dict[str, Any]:
    return _dashboard_overview()


@app.post("/dashboard/api/oauth/start")
def dashboard_oauth_start(request: Request) -> Any:
    result = oauth_start(
        request.cookies.get(DASHBOARD_COOKIE, ""),
        base_url=str(request.base_url).rstrip("/"),
    )
    if not result.get("available"):
        return JSONResponse(status_code=503, content=result)
    return result


@app.get("/dashboard/oauth/callback")
def dashboard_oauth_callback(request: Request) -> Response:
    """Consume the operator-app callback; no token values are rendered."""

    error = request.query_params.get("error")
    state = request.query_params.get("state", "")
    if error:
        return Response(
            "OAuth was not completed. Return to Account and use Advanced recovery.",
            status_code=400,
            media_type="text/plain",
        )
    try:
        transaction = oauth_consume(state, request.cookies.get(DASHBOARD_COOKIE, ""))
        code = request.query_params.get("code", "")
        if not code:
            raise ValueError("oauth_code_missing")
        response = oauth_exchange(transaction, code)
        status = import_server_response(response)
        message = "Microsoft connected. Generation and refresh readiness were validated."
        if status.get("state") not in {"active", "expiring_soon"}:
            message = "Microsoft authorization completed, but generation is not ready."
        return Response(
            f"<script>window.location.replace('/dashboard?oauth=success&state={status.get('state','unknown')}')</script>{message}",
            status_code=200,
            media_type="text/html",
        )
    except (ValueError, BetaConfigurationError, BetaUpstreamError) as exc:
        safe = _safe_error_phase(exc)
        return Response(
            f"<script>window.location.replace('/dashboard?oauth=failed&reason={safe}')</script>OAuth failed: {safe}",
            status_code=400 if isinstance(exc, ValueError) else 502,
            media_type="text/html",
        )


@app.get("/dashboard/api/oauth/status")
def dashboard_oauth_status() -> dict[str, Any]:
    configured = bool(os.environ.get("CODEX_AUTH_M365_BETA_OAUTH_CLIENT_ID") and os.environ.get("CODEX_AUTH_M365_BETA_OAUTH_CLIENT_SECRET"))
    return {
        "provider": "m365-copilot",
        "state": "available" if configured else "blocked_by_upstream",
        "automatic_login": configured,
        "reason": None if configured else "operator_oauth_app_not_configured",
        "secrets_returned": False,
    }


@app.post("/dashboard/api/oauth/disconnect")
def dashboard_oauth_disconnect(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not bool((payload or {}).get("confirm")):
        return JSONResponse(status_code=400, content={"error": "explicit_confirmation_required"})
    # Use the existing manager's safe replacement path only when an operator
    # has a configured credential.  No token is returned or logged.
    return {"status": "runtime_disconnect_requested", "secrets_returned": False}


@app.post("/dashboard/api/credentials/import")
def dashboard_import_credentials(payload: dict[str, Any]) -> Any:
    try:
        value = payload.get("credential")
        if not isinstance(value, dict):
            raise BetaConfigurationError("credential must be a JSON object")
        status = M365BearerBeta.from_directory().replace_credential(value)
        return {
            "status": "ok",
            "provider": "m365-copilot",
            "credential": status,
            "secrets_returned": False,
        }
    except BetaConfigurationError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(exc)})
    except BetaUpstreamError as exc:
        return JSONResponse(status_code=502, content={"status": "error", "error": _safe_error_phase(exc)})


@app.post("/dashboard/api/refresh")
def dashboard_refresh() -> Any:
    try:
        return {
            "status": "ok",
            "provider": "m365-copilot",
            "credential": M365BearerBeta.from_directory().refresh(),
        }
    except BetaConfigurationError as exc:
        return JSONResponse(status_code=409, content={"status": "error", "error": str(exc)})
    except BetaUpstreamError as exc:
        return JSONResponse(status_code=502, content={"status": "error", "error": _safe_error_phase(exc)})


@app.post("/dashboard/api/probe")
async def dashboard_probe() -> Any:
    """Run one harmless marker request and return structural proof only."""

    marker = "M365_DASHBOARD_PROBE_42"
    started = time.monotonic()
    event_types: set[str] = set()
    output_fragments: list[str] = []
    try:
        async for event in _provider_events(f"Reply exactly {marker}", "auto"):
            event_type = str(event.get("type") or "")
            if event_type:
                event_types.add(event_type[:64])
            if event_type == "text_delta" and isinstance(event.get("delta"), str):
                output_fragments.append(event["delta"])
        marker_observed = marker in "".join(output_fragments)
        return {
            "state": "passed" if marker_observed else "failed",
            "cookie_count": 0,
            "marker_observed": marker_observed,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "event_types": sorted(event_types),
            "response_text_returned": False,
        }
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "state": "failed",
                "phase": _safe_error_phase(exc),
                "cookie_count": 0,
                "response_text_returned": False,
            },
        )
    finally:
        output_fragments.clear()


@app.get("/dashboard/api/logs/stream")
def dashboard_logs_stream() -> StreamingResponse:
    return logs_stream()
