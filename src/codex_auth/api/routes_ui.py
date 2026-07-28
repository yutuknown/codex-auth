import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..config import auth_is_configured, get_auth_file, get_cookie_file, save_cookie_text

router = APIRouter()
MAX_COOKIE_UPDATE_CHARACTERS = 512 * 1024


class CookieUpdateRequest(BaseModel):
    cookies: str = Field(min_length=1, max_length=MAX_COOKIE_UPDATE_CHARACTERS)
    source_name: str | None = Field(default=None, max_length=128)


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Codex-Auth Dashboard Login</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center;
           background: #09090b; color: #fafafa; }
    main { width: min(390px, calc(100% - 40px)); padding: 32px; border: 1px solid #27272a;
           border-radius: 16px; background: #111113; box-shadow: 0 20px 60px #0008; }
    img { display: block; width: 180px; max-height: 70px; margin: 0 auto 24px; }
    h1 { margin: 0 0 8px; font-size: 22px; }
    p { margin: 0 0 22px; color: #a1a1aa; line-height: 1.5; }
    label { display: block; margin-bottom: 8px; font-size: 13px; color: #d4d4d8; }
    input { box-sizing: border-box; width: 100%; padding: 12px 14px; color: #fafafa;
            border: 1px solid #3f3f46; border-radius: 9px; background: #18181b; }
    button { width: 100%; margin-top: 14px; padding: 12px; border: 0; border-radius: 9px;
             background: #fafafa; color: #09090b; font-weight: 700; cursor: pointer; }
    .error { color: #f87171; margin-bottom: 14px; }
  </style>
</head>
<body><main>
  <img src="/assets/logo-dark.svg" alt="Codex-Auth">
  <h1>Dashboard login</h1>
  <p>Enter the private API key configured for this deployment.</p>
  {error}
  <form method="post" action="/login">
    <label for="api_key">API key</label>
    <input id="api_key" name="api_key" type="password" required autofocus autocomplete="current-password">
    <button type="submit">Open dashboard</button>
  </form>
</main></body></html>"""


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/login", status_code=303)


@router.head("/", include_in_schema=False)
async def root_head():
    return RedirectResponse("/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_login():
    return LOGIN_PAGE.replace("{error}", "")


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_login_submit(request: Request):
    expected_key = os.environ.get("CODEX_AUTH_API_KEY", "")
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    supplied_key = (form.get("api_key") or [""])[0]
    if not expected_key or not supplied_key or not secrets.compare_digest(supplied_key, expected_key):
        return HTMLResponse(
            LOGIN_PAGE.replace("{error}", '<p class="error">Invalid API key.</p>'),
            status_code=401,
        )

    from . import dashboard_session_value

    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        "codex_auth_dashboard",
        dashboard_session_value(expected_key),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https" or bool(os.environ.get("RENDER")),
        samesite="lax",
        path="/",
    )
    return response


@router.get("/logout", include_in_schema=False)
async def dashboard_logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("codex_auth_dashboard", path="/")
    return response


@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    dashboard_path = Path(__file__).resolve().parent.parent / "web" / "templates" / "dashboard.html"
    if dashboard_path.exists():
        return dashboard_path.read_text(encoding="utf-8")
    return "<h1>Dashboard UI Not Found</h1>"

@router.get("/api/logs")
async def get_logs():
    # Import log_stream inside the route to avoid circular imports
    from . import log_stream, log_stream_lock
    with log_stream_lock:
        return {"logs": [dict(entry) for entry in log_stream]}

@router.get("/api/usage")
async def get_usage():
    from ..usage import DEFAULT_PRICING, PRICING, get_usage_file, load_usage
    usage_file = get_usage_file()
    if usage_file.exists():
        try:
            data = load_usage()
            # Transform stored field names to match what the frontend expects
            models_out = {}
            for model, stats in data.get("models", {}).items():
                in_price, out_price = PRICING.get(model, DEFAULT_PRICING)
                input_tok = stats.get("input_tokens", 0)
                output_tok = stats.get("output_tokens", 0)
                estimated_cost = (input_tok / 1_000_000) * in_price + (output_tok / 1_000_000) * out_price
                total_ttft_s = stats.get("total_ttft_s", 0.0)
                total_generation_s = stats.get("total_generation_s", 0.0)
                requests_count = stats.get("requests", 0)
                
                avg_ttft_ms = (total_ttft_s / requests_count * 1000) if requests_count > 0 else 0
                tokens_per_sec = (output_tok / total_generation_s) if total_generation_s > 0 else 0
                
                models_out[model] = {
                    "prompt_tokens": input_tok,
                    "completion_tokens": output_tok,
                    "estimated_cost": estimated_cost,
                    "requests": requests_count,
                    "avg_ttft_ms": avg_ttft_ms,
                    "tokens_per_sec": tokens_per_sec,
                }
            return {
                "total_requests": data.get("total_requests", 0),
                "total_input_tokens": data.get("total_input_tokens", 0),
                "total_output_tokens": data.get("total_output_tokens", 0),
                "total_savings_usd": data.get("total_savings_usd", 0.0),
                "models": models_out,
            }
        except Exception:
            pass
    return {"models": {}, "total_requests": 0, "total_input_tokens": 0, "total_output_tokens": 0, "total_savings_usd": 0.0}

@router.get("/api/status")
async def get_status():
    from .. import __version__
    from ..providers.openai.provider import provider

    auth_file = get_auth_file()
    cookie_file = get_cookie_file()
    is_configured = auth_is_configured()
    runtime = provider.runtime_status()
    is_authenticated = bool(runtime["initialized"])
    if os.environ.get("CODEX_AUTH_COOKIES"):
        auth_source = "CODEX_AUTH_COOKIES"
    elif cookie_file.exists():
        auth_source = str(cookie_file.absolute())
    elif os.environ.get("CODEX_AUTH_JSON"):
        auth_source = "CODEX_AUTH_JSON"
    else:
        auth_source = str(auth_file.absolute())
    return {
        "status": (
            "Active"
            if is_authenticated
            else "Configured, not initialized"
            if is_configured
            else "Missing Authentication"
        ),
        "auth_file_path": auth_source if is_configured else None,
        "is_authenticated": is_authenticated,
        "is_configured": is_configured,
        "version": __version__,
        "runtime": runtime,
    }


@router.get("/api/account")
async def get_account():
    from ..providers.openai.provider import ChatGPTSessionError, provider

    try:
        return await provider.fetch_account_details()
    except ChatGPTSessionError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": str(exc), "type": "upstream_error"},
        ) from exc


@router.post("/api/auth/cookies")
async def update_session_cookies(payload: CookieUpdateRequest):
    from ..providers.openai.provider import ChatGPTSessionError, provider

    cookie_text = payload.cookies.strip()
    try:
        details = await provider.replace_cookies(cookie_text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "type": "invalid_cookie_format"},
        ) from exc
    except ChatGPTSessionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "type": "cookie_validation_error"},
        ) from exc

    file_saved = False
    persistence_warning = None
    try:
        save_cookie_text(cookie_text)
        file_saved = True
    except OSError:
        persistence_warning = (
            "The cookies are active in this process, but the local cookie file could not be updated"
        )
    os.environ["CODEX_AUTH_COOKIES"] = cookie_text

    runtime = provider.runtime_status()
    profile = details.get("profile") or {}
    account = details.get("account") or {}
    entitlement = details.get("entitlement") or {}
    hosted_on_render = bool(os.environ.get("RENDER"))
    if hosted_on_render:
        persistence_warning = (
            "Active now. To survive a Render restart or deploy, also update the "
            "CODEX_AUTH_COOKIES secret or attach a persistent disk"
        )
    return {
        "status": "activated",
        "cookie_count": len(provider.cookie_metadata),
        "auth_mode": runtime.get("auth_mode"),
        "session_cookie": runtime.get("session_cookie"),
        "profile": {
            "identity_level": profile.get("identity_level"),
            "id_present": bool(profile.get("id")),
            "email_present": bool(profile.get("email")),
        },
        "account": {
            "plan_type": account.get("plan_type"),
            "subscription_plan": entitlement.get("subscription_plan"),
            "session_access": bool(details.get("can_access_with_session")),
        },
        "persistence": {
            "runtime_active": True,
            "file_saved": file_saved,
            "restart_safe": file_saved and not hosted_on_render,
            "warning": persistence_warning,
        },
    }


@router.get("/api/models_list")
async def get_models_list():

    from ..providers.openai.provider import ChatGPTSessionError
    from .routes_openai import provider
    
    try:
        real_models = await provider.fetch_models()
    except ChatGPTSessionError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": str(exc), "type": "upstream_error"},
        ) from exc
    models_out = []
    proxy_capabilities = provider.runtime_status()["proxy_capabilities"]
    
    for m in real_models:
        slug = m.get("slug", "auto")
        max_tokens = m.get("max_tokens", 32768)
        product_features = m.get("product_features", {})
        attachments = product_features.get("attachments", {})
        enabled_tools = m.get("enabled_tools") or []
            
        models_out.append({
            "id": slug,
            "title": m.get("title") or slug,
            "description": m.get("description") or "",
            "context_length": max_tokens,
            "reasoning_type": m.get("reasoning_type") or "none",
            "configurable_thinking_effort": bool(m.get("configurable_thinking_effort")),
            "upstream_capabilities": {
                "attachments": bool(attachments),
                "image_input": bool(attachments.get("image_mime_types")),
                "tools": "tools" in enabled_tools or "tools2" in enabled_tools,
                "search": "search" in enabled_tools,
                "canvas": "canvas" in enabled_tools,
                "image_generation": "image_gen_tool_enabled" in enabled_tools,
            },
            "proxy_capabilities": {
                **proxy_capabilities,
            },
        })
        
    return {
        "models": models_out,
        "model_count": len(models_out),
        "default_model": "auto",
        "source": "/backend-api/models",
    }
