"""Local compatibility API for the cookie-free M365 bearer beta.

M365's provider-authored chain-of-thought progress is exposed as a reasoning
summary. It is never presented as raw chain-of-thought and no signature is
invented.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from beta.m365_artifacts import artifact_store
from beta.m365_bearer import (
    BETA_CONFIRM_ENV,
    BetaConfigurationError,
    BetaUpstreamError,
    M365BearerBeta,
)
from beta.m365_equivalence import equivalence_report
from beta.m365_events import public_event
from beta.m365_files import GraphCredential, M365GraphUploader
from beta.m365_images import M365SubstrateImageUploader
from beta.m365_models import M365ModelCatalog
from beta.m365_remote import RemoteAttachmentFetcher
from beta.m365_research import research_report
from beta.m365_telemetry import telemetry

API_KEY_ENV = "CODEX_AUTH_M365_BETA_API_KEY"
MAX_CONVERSATION_TURNS = 64
MAX_COMPILED_PROMPT_CHARACTERS = 200_000
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
            if isinstance(value, (BetaConfigurationError, BetaUpstreamError)):
                raise value
            raise BetaUpstreamError("compatibility_stream_failed") from value
        else:
            break


def _anthropic_sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def _openai_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


async def anthropic_event_stream(
    prompt: str,
    model: str,
    attachments: list[Any] | None = None,
) -> AsyncGenerator[str, None]:
    encoder = AnthropicStreamEncoder(model, input_text=prompt)
    emitted = False
    try:
        async for event in _provider_events(prompt, model, attachments):
            for output in encoder.feed(event):
                emitted = True
                yield _anthropic_sse(output)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        if not emitted:
            raise
        error = {
            "type": "error",
            "error": {"type": "upstream_error", "message": str(exc)},
        }
        yield _anthropic_sse(error)
        return
    for output in encoder.finish():
        yield _anthropic_sse(output)


async def openai_event_stream(
    prompt: str,
    model: str,
    attachments: list[Any] | None = None,
) -> AsyncGenerator[str, None]:
    encoder = OpenAIStreamEncoder(model, input_text=prompt)
    emitted = False
    try:
        async for event in _provider_events(prompt, model, attachments):
            for output in encoder.feed(event):
                emitted = True
                yield _openai_sse(output)
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        if not emitted:
            raise
        error = {"error": {"type": "upstream_error", "message": str(exc)}}
        yield _openai_sse(error)
        yield "data: [DONE]\n\n"
        return
    for output in encoder.finish():
        yield _openai_sse(output)
    yield "data: [DONE]\n\n"


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


@app.middleware("http")
async def optional_api_key_guard(request: Request, call_next: Any) -> Any:
    expected = os.environ.get(API_KEY_ENV)
    if expected and request.url.path.startswith("/v1/"):
        authorization = request.headers.get("authorization", "")
        supplied = request.headers.get("x-api-key", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if supplied != expected:
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


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "M365 bearer beta compatibility API",
        "provider": "m365-copilot",
        "scope": "local beta",
        "endpoints": [
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
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        return {
            "status": "ok",
            "provider": "m365-copilot",
            **M365BearerBeta.from_directory().status(),
            "catalog": M365ModelCatalog.from_directory().safe_status(),
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
async def anthropic_messages(request: dict[str, Any]) -> Any:
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
    if request.get("stream"):
        try:
            stream = await _preflight_stream(
                anthropic_event_stream(prompt, model, attachments)
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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    events: list[dict[str, Any]] = []
    try:
        async for event in _provider_events(prompt, model, attachments):
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
        "provider_metadata": prepared.safe_status(),
    }


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: dict[str, Any]) -> Any:
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
    if request.get("stream"):
        try:
            stream = await _preflight_stream(
                openai_event_stream(prompt, model, attachments)
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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    events: list[dict[str, Any]] = []
    try:
        async for event in _provider_events(prompt, model, attachments):
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
        "provider_metadata": prepared.safe_status(),
    }
