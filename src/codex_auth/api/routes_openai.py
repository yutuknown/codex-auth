import base64
import binascii
import json
import logging
import time
import urllib.parse
from typing import Any, Dict, List, Union

import tiktoken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..providers.errors import ProviderBusyError as GenericProviderBusyError
from ..providers.errors import ProviderError
from ..providers.openai.provider import ChatGPTSessionError, ProviderBusyError, provider
from ..providers.runtime import registry
from ..usage import record_usage
from .trace_context import request_trace_id

router = APIRouter()
logger = logging.getLogger("codex_auth")
TRACE_TEXT_PART_LIMIT = 4000
TRACE_TEXT_TOTAL_LIMIT = 16000
TRACE_RESPONSE_LIMIT = 16000
TRACE_TOOLS_TOTAL_LIMIT = 8000
TRACE_MESSAGE_LIMIT = 100


# --- Pydantic Schemas for Validation ---
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    provider: str | None = None
    messages: List[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    web_search: bool = False
    tools: List[Dict[str, Any]] | None = None
    tool_choice: Any = None


def _content_text(content: Union[str, List[Dict[str, Any]]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(item.get("text") or "") for item in content if item.get("type") == "text" and item.get("text"))


def _request_input(messages: List[ChatMessage]) -> tuple[str, list[dict[str, Any] | str]]:
    transcript = []
    files: list[dict[str, Any] | str] = []
    for message in messages:
        text = _content_text(message.content).strip()
        if text:
            transcript.append((message.role, text))
        if isinstance(message.content, list):
            for item in message.content:
                if item.get("type") not in {"image_url", "file_url"}:
                    continue
                key = item["type"]
                file_data = item.get(key, {})
                if isinstance(file_data, str):
                    files.append(file_data)
                elif isinstance(file_data, dict):
                    files.append(
                        {
                            "url": file_data.get("url", ""),
                            "name": file_data.get("name") or item.get("name"),
                            "mime_type": file_data.get("mime_type") or item.get("mime_type"),
                        }
                    )

    if len(transcript) <= 1:
        return (transcript[0][1] if transcript else ""), files
    prompt = "\n\n".join(f"{role.upper()}:\n{text}" for role, text in transcript)
    return (
        "Use the following conversation transcript as context. Answer the final user message only.\n\n" + prompt,
        files,
    )


def _estimated_base64_bytes(payload: str) -> int:
    payload = "".join(payload.split())
    return max(0, len(payload) * 3 // 4 - payload[-2:].count("=")) if payload else 0


def _image_metadata(source: str, mime_type: str) -> dict[str, Any]:
    if not source.startswith("data:") or ";base64," not in source:
        return {}
    try:
        encoded = source.split(",", 1)[1]
        sample = base64.b64decode(encoded[:131072], validate=False)
    except (ValueError, TypeError, binascii.Error):
        return {}
    if mime_type == "image/png" and len(sample) >= 24:
        return {
            "width": int.from_bytes(sample[16:20], "big"),
            "height": int.from_bytes(sample[20:24], "big"),
        }
    if mime_type == "image/gif" and len(sample) >= 10:
        return {
            "width": int.from_bytes(sample[6:8], "little"),
            "height": int.from_bytes(sample[8:10], "little"),
        }
    if mime_type == "image/jpeg" and sample.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(sample):
            if sample[offset] != 0xFF:
                offset += 1
                continue
            marker = sample[offset + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return {
                    "width": int.from_bytes(sample[offset + 7 : offset + 9], "big"),
                    "height": int.from_bytes(sample[offset + 5 : offset + 7], "big"),
                }
            if marker in {0xD8, 0xD9}:
                offset += 2
                continue
            segment_length = int.from_bytes(sample[offset + 2 : offset + 4], "big")
            if segment_length < 2:
                break
            offset += 2 + segment_length
    return {}


def _trace_attachment(item_type: str, value: Any, item: dict[str, Any]) -> dict[str, Any]:
    details = value if isinstance(value, dict) else {}
    source = str(value if isinstance(value, str) else details.get("url", ""))
    name = details.get("name") or item.get("name")
    declared_mime = details.get("mime_type") or item.get("mime_type")
    source_kind = "base64"
    mime_type = declared_mime or "application/octet-stream"
    estimated_bytes = None
    safe_location = None
    encoding = None
    if source.startswith("data:"):
        source_kind = "data_url"
        header, _, payload = source.partition(",")
        mime_type = declared_mime or header[5:].split(";", 1)[0] or "text/plain"
        encoding = "base64" if ";base64" in header.lower() else "url_encoded"
        estimated_bytes = _estimated_base64_bytes(payload) if encoding == "base64" else len(payload)
    elif source.startswith(("http://", "https://")):
        source_kind = "public_url"
        parsed = urllib.parse.urlsplit(source)
        safe_location = {
            "origin": f"{parsed.scheme}://{parsed.netloc}",
            "path": parsed.path[:512] or "/",
            "query": "[REDACTED]" if parsed.query else None,
            "fragment": "[REDACTED]" if parsed.fragment else None,
        }
    metadata = {
        "type": item_type,
        "media_kind": "image" if item_type == "image_url" or mime_type.startswith("image/") else "file",
        "name": name,
        "mime_type": mime_type,
        "source": source_kind,
        "encoding": encoding,
        "encoded_characters": len(source),
        "estimated_bytes": estimated_bytes,
        "location": safe_location,
        "content": "[BINARY CONTENT REDACTED]",
    }
    metadata.update(_image_metadata(source, mime_type))
    return metadata


def _trace_messages(messages: List[ChatMessage]) -> list[dict[str, Any]]:
    summaries = []
    remaining_text_characters = TRACE_TEXT_TOTAL_LIMIT

    def capture_text(text: str) -> tuple[str, bool]:
        nonlocal remaining_text_characters
        capture_length = min(TRACE_TEXT_PART_LIMIT, remaining_text_characters)
        captured = text[:capture_length]
        remaining_text_characters -= len(captured)
        return captured, len(captured) < len(text)

    for message in messages[:TRACE_MESSAGE_LIMIT]:
        if isinstance(message.content, str):
            captured_text, text_truncated = capture_text(message.content)
            summaries.append(
                {
                    "role": message.role,
                    "content_type": "text",
                    "characters": len(message.content),
                    "text": captured_text,
                    "text_truncated": text_truncated,
                }
            )
            continue
        parts = []
        for item in message.content:
            item_type = item.get("type")
            if item_type == "text":
                text = str(item.get("text") or "")
                captured_text, text_truncated = capture_text(text)
                parts.append(
                    {
                        "type": "text",
                        "characters": len(text),
                        "text": captured_text,
                        "text_truncated": text_truncated,
                    }
                )
            elif item_type in {"image_url", "file_url"}:
                value = item.get(item_type, {})
                parts.append(_trace_attachment(item_type, value, item))
            else:
                parts.append(
                    {
                        "type": item_type or "unknown",
                        "content": "[UNSUPPORTED CONTENT PART REDACTED]",
                        "keys": sorted(str(key) for key in item.keys()),
                    }
                )
        summaries.append({"role": message.role, "content_type": "multipart", "parts": parts})
    return summaries


def _trace_tools(tools: List[Dict[str, Any]] | None) -> list[dict[str, Any]]:
    summaries = []
    remaining_characters = TRACE_TOOLS_TOTAL_LIMIT
    for tool in (tools or [])[:64]:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        parameters = function.get("parameters")
        serialized_parameters = json.dumps(parameters, ensure_ascii=False) if parameters else ""
        description = str(function.get("description") or "")
        captured_description = description[: min(1000, remaining_characters)]
        remaining_characters -= len(captured_description)
        captured_parameters = serialized_parameters[: min(4000, remaining_characters)]
        remaining_characters -= len(captured_parameters)
        summaries.append(
            {
                "type": tool.get("type"),
                "name": function.get("name"),
                "description": captured_description,
                "description_truncated": len(captured_description) < len(description),
                "parameters": captured_parameters if serialized_parameters else None,
                "parameters_truncated": len(captured_parameters) < len(serialized_parameters),
            }
        )
    return summaries


def _trace_data(
    req: ChatCompletionRequest,
    requested_model: str,
    response: str,
    ttft_ms: int,
    generation_s: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    status: int = 200,
    chunk_count: int = 0,
) -> dict[str, Any]:
    limit = TRACE_RESPONSE_LIMIT
    traced_messages = _trace_messages(req.messages)
    captured_attachment_count = sum(
        1
        for message in traced_messages
        for part in message.get("parts", [])
        if part.get("media_kind") in {"image", "file"}
    )
    attachment_count = sum(
        1
        for message in req.messages
        if isinstance(message.content, list)
        for part in message.content
        if part.get("type") in {"image_url", "file_url"}
    )
    response_capture = response[:limit]
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    return {
        "method": "POST",
        "status": status,
        "path": "/v1/chat/completions",
        "model": requested_model,
        "messages": traced_messages,
        "payload": {
            "model": req.model,
            "normalized_model": requested_model,
            "stream": req.stream,
            "web_search": req.web_search,
            "message_count": len(req.messages),
            "message_capture_truncated": len(req.messages) > TRACE_MESSAGE_LIMIT,
            "attachment_count": attachment_count,
            "captured_attachment_count": captured_attachment_count,
            "tool_count": len(req.tools or []),
            "tool_capture_truncated": len(req.tools or []) > 64,
            "tools": _trace_tools(req.tools),
            "tool_choice": str(req.tool_choice)[:1000] if req.tool_choice is not None else None,
        },
        "response": response_capture,
        "response_data": {
            "object": "chat.completion.chunk.aggregate" if req.stream else "chat.completion",
            "model": requested_model,
            "status": status,
            "finish_reason": "stop" if status < 400 else "error",
            "assistant": {
                "role": "assistant",
                "content": response_capture,
                "characters": len(response),
                "truncated": len(response) > limit,
            },
            "usage": usage,
            "stream": {
                "enabled": req.stream,
                "chunk_count": chunk_count,
                "done": status < 400,
            },
        },
        "response_truncated": len(response) > limit,
        "response_characters": len(response),
        "ttft_ms": ttft_ms,
        "generation_s": round(generation_s, 2),
        **usage,
        "stream": req.stream,
        "web_search": req.web_search,
        "message_count": len(req.messages),
        "attachment_count": attachment_count,
        "tool_count": len(req.tools or []),
        "chunk_count": chunk_count,
    }


@router.get("/v1/models")
async def openai_models(refresh: bool = False):
    models_data = []
    for provider_id in registry.ids():
        candidate = registry.get(provider_id)
        if not candidate.is_configured():
            continue
        try:
            await registry.ensure_initialized(provider_id)
            real_models = await candidate.fetch_models(refresh=refresh)
        except ProviderError as exc:
            if provider_id == registry.default_provider_id:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"message": str(exc), "type": exc.error_type},
                ) from exc
            logger.warning("Skipping unavailable provider %s: %s", provider_id, exc)
            continue
        for m in real_models:
            slug = m.get("slug", "auto")
            max_tokens = m.get("max_tokens", 32768)
            models_data.append(
                {
                    "id": f"{provider_id}:{slug}",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": provider_id,
                    "context_length": max_tokens,
                    "name": m.get("title") or slug,
                    "upstream_id": m.get("upstream_id"),
                }
            )
            if provider_id == registry.default_provider_id:
                models_data.append(
                    {
                        "id": slug,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": provider_id,
                        "context_length": max_tokens,
                        "name": m.get("title") or slug,
                        "upstream_id": m.get("upstream_id"),
                        "alias_for": f"{provider_id}:{slug}",
                    }
                )
    return {"object": "list", "data": models_data}


@router.api_route("/backend-api/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy_backend_api(path: str, request: Request):
    url = f"https://chatgpt.com/backend-api/{path}"
    logger.info(f"Proxying request to [cyan]{url}[/cyan]")

    try:
        if request.method == "OPTIONS":
            return {}
        try:
            body = await request.json() if request.method == "POST" else None
        except Exception:
            body = None
        status, content_type, content = await provider.proxy_request(request.method, path, body)
        if "application/json" in content_type:
            return JSONResponse(status_code=status, content=json.loads(content))
        return Response(status_code=status, content=content, media_type=content_type or None)
    except HTTPException:
        raise
    except ChatGPTSessionError as e:
        raise HTTPException(status_code=502, detail={"message": str(e), "type": "upstream_error"}) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/v1/chat/completions")
async def openai_chat_completions(req: ChatCompletionRequest):
    request_id = request_trace_id.get()
    requested_model = req.model
    if requested_model.endswith("-vision"):
        requested_model = requested_model[:-7]
    try:
        selection = registry.select(requested_model, req.provider)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), "type": exc.error_type},
        ) from exc
    selected_provider = selection.provider
    if selection.provider_id != registry.default_provider_id:
        try:
            await registry.ensure_initialized(selection.provider_id)
        except ProviderError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": str(exc), "type": exc.error_type},
            ) from exc
    provider_model = selection.model
    requested_model = (
        provider_model
        if selection.provider_id == registry.default_provider_id and req.provider is None and ":" not in req.model
        else f"{selection.provider_id}:{provider_model}"
    )
    if req.tools or (req.tool_choice is not None and req.tool_choice != "none"):
        detail = {
            "message": "OpenAI function tools and tool_choice are not implemented by this proxy",
            "type": "unsupported_feature",
        }
        logger.info(
            "[API] Request rejected - unsupported function tools",
            extra={
                "trace_data": _trace_data(
                    req,
                    requested_model,
                    json.dumps({"error": detail}),
                    0,
                    0,
                    status=501,
                ),
                "request_id": request_id,
            },
        )
        raise HTTPException(
            status_code=501,
            detail=detail,
        )

    prompt, files = _request_input(req.messages)

    def get_token_count(text: str) -> int:
        try:
            enc = tiktoken.encoding_for_model("gpt-4o")
        except Exception:
            enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text)) if text else 0

    prompt_tokens = get_token_count(prompt)

    def log_failure(
        status: int,
        error_type: str,
        message: str,
        started_at: float,
        chunk_count: int = 0,
    ) -> dict[str, Any]:
        error = {"message": message, "type": error_type, "code": status}
        logger.info(
            f"[API] Request failed - {status} {error_type}",
            extra={
                "trace_data": _trace_data(
                    req,
                    requested_model,
                    json.dumps({"error": error}),
                    0,
                    time.time() - started_at,
                    prompt_tokens,
                    0,
                    status=status,
                    chunk_count=chunk_count,
                ),
                "request_id": request_id,
            },
        )
        return {"error": error}

    if req.stream:

        async def event_generator():
            full_response = ""
            chunk_count = 0
            created_time = int(time.time())

            start_time = time.time()
            ttft_s = 0.0
            first_token_received = False

            try:
                async for chunk in selected_provider.generate_stream(
                    prompt,
                    files=files,
                    web_search=req.web_search,
                    model=provider_model,
                    realtime=False,
                ):
                    if not first_token_received:
                        ttft_s = time.time() - start_time
                        first_token_received = True

                    full_response += chunk
                    chunk_count += 1

                    data = {
                        "id": "chatcmpl-stealth",
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                # Record Usage before sending final chunk so we have the counts
                generation_s = time.time() - start_time
                completion_tokens = get_token_count(full_response)

                # Final finish event
                final_data = {
                    "id": "chatcmpl-stealth",
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": requested_model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                }
                yield f"data: {json.dumps(final_data)}\n\n"
                yield "data: [DONE]\n\n"

                # Record Usage after stream finishes
                try:
                    record_usage(requested_model, prompt_tokens, completion_tokens, ttft_s, generation_s)
                except Exception as e:
                    logger.error(f"[API] Failed to record usage: {e}")
                logger.info(
                    f"[API] Stream completed - TTFT: {ttft_s * 1000:.0f}ms - {completion_tokens} tok",
                    extra={
                        "trace_data": _trace_data(
                            req,
                            requested_model,
                            full_response,
                            round(ttft_s * 1000),
                            generation_s,
                            prompt_tokens,
                            completion_tokens,
                            chunk_count=chunk_count,
                        ),
                        "request_id": request_id,
                    },
                )

            except (ProviderBusyError, GenericProviderBusyError) as e:
                err = log_failure(429, "rate_limit_error", str(e), start_time, chunk_count)
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            except ChatGPTSessionError as e:
                err = log_failure(502, "upstream_error", str(e), start_time, chunk_count)
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            except ProviderError as e:
                err = log_failure(e.status_code, e.error_type, str(e), start_time, chunk_count)
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err = log_failure(500, "internal_error", str(e), start_time, chunk_count)
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    else:
        # Non-streaming fallback
        full_response = ""
        chunk_count = 0
        start_time = time.time()
        try:
            async for chunk in selected_provider.generate_stream(
                prompt,
                files=files,
                web_search=req.web_search,
                model=provider_model,
                realtime=False,
            ):
                full_response += chunk
                chunk_count += 1

            generation_s = time.time() - start_time
            completion_tokens = get_token_count(full_response)
            try:
                # TTFT is same as full generation time for non-streaming
                record_usage(requested_model, prompt_tokens, completion_tokens, generation_s, generation_s)
            except Exception as e:
                logger.error(f"[API] Failed to record usage: {e}")
            logger.info(
                f"[API] Request completed - {completion_tokens} tok",
                extra={
                    "trace_data": _trace_data(
                        req,
                        requested_model,
                        full_response,
                        round(generation_s * 1000),
                        generation_s,
                        prompt_tokens,
                        completion_tokens,
                        chunk_count=chunk_count,
                    ),
                    "request_id": request_id,
                },
            )

            return {
                "id": "chatcmpl-stealth",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": full_response}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            }
        except (ProviderBusyError, GenericProviderBusyError) as e:
            log_failure(429, "rate_limit_error", str(e), start_time, chunk_count)
            raise HTTPException(
                status_code=429,
                detail={"message": str(e), "type": "rate_limit_error"},
                headers={"Retry-After": "5"},
            )
        except ChatGPTSessionError as e:
            log_failure(502, "upstream_error", str(e), start_time, chunk_count)
            raise HTTPException(status_code=502, detail={"message": str(e), "type": "upstream_error"})
        except ProviderError as e:
            log_failure(e.status_code, e.error_type, str(e), start_time, chunk_count)
            raise HTTPException(
                status_code=e.status_code,
                detail={"message": str(e), "type": e.error_type},
            ) from e
        except Exception as e:
            log_failure(500, "internal_error", str(e), start_time, chunk_count)
            raise HTTPException(status_code=500, detail={"message": str(e), "type": "internal_error"})
