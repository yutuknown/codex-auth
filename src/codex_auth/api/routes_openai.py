import time
import json
import logging
import asyncio
from typing import List, Optional, Union, Dict, Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import tiktoken

from ..providers.openai.provider import provider
from ..core.browser import (
    CaptchaDetectedError,
    AccountBlockedError,
    StealthTimeoutError
)
from ..usage import record_usage

router = APIRouter()
logger = logging.getLogger("codex_auth")

# --- Pydantic Schemas for Validation ---
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]

class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: List[ChatMessage] = Field(..., min_items=1)
    stream: bool = False
    web_search: bool = False

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
        context = await provider.get_context()
        if request.method == "GET":
            response = await context.request.get(url)
        elif request.method == "POST":
            try:
                body = await request.json()
                response = await context.request.post(url, data=body)
            except:
                response = await context.request.post(url)
        elif request.method == "OPTIONS":
            return {}
            
        if response.ok:
            return await response.json()
        else:
            raise HTTPException(status_code=response.status, detail=await response.text())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/v1/chat/completions")
async def openai_chat_completions(req: ChatCompletionRequest):
    requested_model = req.model
    if requested_model.endswith("-vision"):
        requested_model = requested_model[:-7]
    
    # Smart Session Management
    if len(req.messages) <= 2:
        logger.info(f"[API] Starting a new chat context with model {requested_model}...")
        await provider.reset_session(requested_model)
        
    prompt = ""
    files = []
    
    last_msg = req.messages[-1]
    content = last_msg.content
    
    if isinstance(content, str):
        prompt = content
    elif isinstance(content, list):
        for item in content:
            if item.get("type") == "text":
                prompt += item.get("text", "") + "\n"
            elif item.get("type") in ["image_url", "file_url"]:
                key = item.get("type")
                file_url = item.get(key, {}).get("url", "")
                files.append(file_url)
    
    prompt = prompt.strip()
    
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
                async for chunk in provider.generate_stream(prompt, files=files, web_search=req.web_search):
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
                    logger.info(f"[API] Stream completed • TTFT: {ttft_s*1000:.0f}ms • {completion_tokens} tok", extra={
                        "trace_data": {
                            "method": "POST",
                            "status": 200,
                            "path": "/v1/chat/completions",
                            "model": requested_model,
                            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
                            "response": full_response,
                            "ttft_ms": round(ttft_s * 1000),
                            "generation_s": round(generation_s, 2)
                        }
                    })
                except Exception as e:
                    logger.error(f"[API] Failed to record usage: {e}")
                    
            except CaptchaDetectedError:
                err = {"error": {"message": "Cloudflare CAPTCHA detected. Proxy blocked.", "type": "captcha_error", "code": 403}}
                yield f"data: {json.dumps(err)}\n\n"
            except AccountBlockedError:
                err = {"error": {"message": "OpenAI account deactivated or blocked.", "type": "account_blocked", "code": 403}}
                yield f"data: {json.dumps(err)}\n\n"
            except StealthTimeoutError as e:
                err = {"error": {"message": str(e), "type": "timeout", "code": 504}}
                yield f"data: {json.dumps(err)}\n\n"
            except Exception as e:
                err = {"error": {"message": str(e), "type": "internal_error", "code": 500}}
                yield f"data: {json.dumps(err)}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    else:
        # Non-streaming fallback
        full_response = ""
        start_time = time.time()
        try:
            async for chunk in provider.generate_stream(prompt, files=files, web_search=req.web_search):
                full_response += chunk
                
            generation_s = time.time() - start_time
            completion_tokens = get_token_count(full_response)
            try:
                # TTFT is same as full generation time for non-streaming
                record_usage(requested_model, prompt_tokens, completion_tokens, generation_s, generation_s)
                logger.info(f"[API] Request completed • {completion_tokens} tok", extra={
                    "trace_data": {
                        "method": "POST",
                        "status": 200,
                        "path": "/v1/chat/completions",
                        "model": requested_model,
                        "messages": [m.model_dump(exclude_none=True) for m in req.messages],
                        "response": full_response,
                        "ttft_ms": round(generation_s * 1000),
                        "generation_s": round(generation_s, 2)
                    }
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
        except CaptchaDetectedError as e:
            raise HTTPException(status_code=403, detail={"message": str(e), "type": "captcha_error"})
        except AccountBlockedError as e:
            raise HTTPException(status_code=403, detail={"message": str(e), "type": "account_blocked"})
        except StealthTimeoutError as e:
            raise HTTPException(status_code=504, detail={"message": str(e), "type": "timeout"})
        except Exception as e:
            raise HTTPException(status_code=500, detail={"message": str(e), "type": "internal_error"})
