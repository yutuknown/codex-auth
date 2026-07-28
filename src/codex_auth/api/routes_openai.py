import json
import logging
import time
from typing import Any, Dict, List, Union

import tiktoken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..providers.openai.provider import ChatGPTSessionError, ProviderBusyError, provider
from ..usage import record_usage

router = APIRouter()
logger = logging.getLogger("codex_auth")

# --- Pydantic Schemas for Validation ---
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]

class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: List[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    web_search: bool = False
    tools: List[Dict[str, Any]] | None = None
    tool_choice: Any = None


def _content_text(content: Union[str, List[Dict[str, Any]]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if item.get("type") == "text" and item.get("text")
    )


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


def _trace_messages(messages: List[ChatMessage]) -> list[dict[str, Any]]:
    summaries = []
    for message in messages:
        if isinstance(message.content, str):
            summaries.append({"role": message.role, "text": message.content[:1000]})
            continue
        parts = []
        for item in message.content:
            item_type = item.get("type")
            if item_type == "text":
                parts.append({"type": "text", "text": str(item.get("text") or "")[:1000]})
            elif item_type in {"image_url", "file_url"}:
                value = item.get(item_type, {})
                source = value if isinstance(value, str) else value.get("url", "")
                parts.append(
                    {
                        "type": item_type,
                        "name": value.get("name") if isinstance(value, dict) else None,
                        "source": (
                            "data_url"
                            if str(source).startswith("data:")
                            else "public_url"
                            if str(source).startswith(("http://", "https://"))
                            else "base64"
                        ),
                        "encoded_characters": len(str(source)),
                    }
                )
        summaries.append({"role": message.role, "parts": parts})
    return summaries


def _trace_data(
    req: ChatCompletionRequest,
    requested_model: str,
    response: str,
    ttft_ms: int,
    generation_s: float,
) -> dict[str, Any]:
    limit = 8000
    return {
        "method": "POST",
        "status": 200,
        "path": "/v1/chat/completions",
        "model": requested_model,
        "messages": _trace_messages(req.messages),
        "response": response[:limit],
        "response_truncated": len(response) > limit,
        "ttft_ms": ttft_ms,
        "generation_s": round(generation_s, 2),
    }

@router.get("/v1/models")
async def openai_models():
    real_models = await provider.fetch_models()
    models_data = []
    for m in real_models:
        slug = m.get("slug", "auto")
        max_tokens = m.get("max_tokens", 32768)
        models_data.append({
            "id": slug,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "openai",
            "context_length": max_tokens,
        })
    return {
        "object": "list",
        "data": models_data
    }

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/v1/chat/completions")
async def openai_chat_completions(req: ChatCompletionRequest):
    if req.tools or (req.tool_choice is not None and req.tool_choice != "none"):
        raise HTTPException(
            status_code=501,
            detail={
                "message": "OpenAI function tools and tool_choice are not implemented by this proxy",
                "type": "unsupported_feature",
            },
        )
    requested_model = req.model
    if requested_model.endswith("-vision"):
        requested_model = requested_model[:-7]
    
    prompt, files = _request_input(req.messages)
    
    def get_token_count(text: str) -> int:
        try:
            enc = tiktoken.encoding_for_model("gpt-4o")
        except Exception:
            enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text)) if text else 0

    prompt_tokens = get_token_count(prompt)
    
    if req.stream:
        async def event_generator():
            full_response = ""
            created_time = int(time.time())
            
            start_time = time.time()
            ttft_s = 0.0
            first_token_received = False
            
            try:
                async for chunk in provider.generate_stream(
                    prompt,
                    files=files,
                    web_search=req.web_search,
                    model=requested_model,
                    realtime=False,
                ):
                    if not first_token_received:
                        ttft_s = time.time() - start_time
                        first_token_received = True
                        
                    full_response += chunk
                    
                    data = {
                        "id": "chatcmpl-stealth",
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": requested_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": chunk},
                                "finish_reason": None
                            }
                        ]
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
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "prompt_tokens_details": {
                            "cached_tokens": 0
                        },
                        "completion_tokens_details": {
                            "reasoning_tokens": 0
                        }
                    }
                }
                yield f"data: {json.dumps(final_data)}\n\n"
                yield "data: [DONE]\n\n"
                
                # Record Usage after stream finishes
                try:
                    record_usage(requested_model, prompt_tokens, completion_tokens, ttft_s, generation_s)
                    logger.info(f"[API] Stream completed - TTFT: {ttft_s*1000:.0f}ms - {completion_tokens} tok", extra={
                        "trace_data": _trace_data(
                            req,
                            requested_model,
                            full_response,
                            round(ttft_s * 1000),
                            generation_s,
                        )
                    })
                except Exception as e:
                    logger.error(f"[API] Failed to record usage: {e}")
                    
            except ProviderBusyError as e:
                err = {"error": {"message": str(e), "type": "rate_limit_error", "code": 429}}
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            except ChatGPTSessionError as e:
                err = {"error": {"message": str(e), "type": "upstream_error", "code": 502}}
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err = {"error": {"message": str(e), "type": "internal_error", "code": 500}}
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    else:
        # Non-streaming fallback
        full_response = ""
        start_time = time.time()
        try:
            async for chunk in provider.generate_stream(
                prompt,
                files=files,
                web_search=req.web_search,
                model=requested_model,
                realtime=False,
            ):
                full_response += chunk
                
            generation_s = time.time() - start_time
            completion_tokens = get_token_count(full_response)
            try:
                # TTFT is same as full generation time for non-streaming
                record_usage(requested_model, prompt_tokens, completion_tokens, generation_s, generation_s)
                logger.info(f"[API] Request completed - {completion_tokens} tok", extra={
                    "trace_data": _trace_data(
                        req,
                        requested_model,
                        full_response,
                        round(generation_s * 1000),
                        generation_s,
                    )
                })
            except Exception as e:
                logger.error(f"[API] Failed to record usage: {e}")

            return {
                "id": "chatcmpl-stealth",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_response
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "prompt_tokens_details": {
                        "cached_tokens": 0
                    },
                    "completion_tokens_details": {
                        "reasoning_tokens": 0
                    }
                }
            }
        except ProviderBusyError as e:
            raise HTTPException(
                status_code=429,
                detail={"message": str(e), "type": "rate_limit_error"},
                headers={"Retry-After": "5"},
            )
        except ChatGPTSessionError as e:
            raise HTTPException(status_code=502, detail={"message": str(e), "type": "upstream_error"})
        except Exception as e:
            raise HTTPException(status_code=500, detail={"message": str(e), "type": "internal_error"})
