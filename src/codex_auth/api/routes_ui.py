import json
import os
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    dashboard_path = Path(__file__).resolve().parent.parent / "web" / "templates" / "dashboard.html"
    if dashboard_path.exists():
        return dashboard_path.read_text(encoding="utf-8")
    return "<h1>Dashboard UI Not Found</h1>"

@router.get("/api/logs")
async def get_logs():
    # Import log_stream inside the route to avoid circular imports
    from . import log_stream
    return {"logs": list(log_stream)}

@router.get("/api/usage")
async def get_usage():
    from ..usage import get_usage_file, load_usage, PRICING, DEFAULT_PRICING
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
    auth_file = Path(__file__).resolve().parent.parent.parent.parent / ".codex" / "auth.json"
    is_authenticated = auth_file.exists()
    return {
        "status": "Active" if is_authenticated else "Missing Authentication",
        "auth_file_path": str(auth_file.absolute()) if is_authenticated else None,
        "is_authenticated": is_authenticated
    }

@router.get("/api/models_list")
async def get_models_list():
    from .routes_openai import provider
    import time
    
    real_models = await provider.fetch_models()
    models_out = []
    
    for m in real_models:
        slug = m.get("slug", "auto")
        max_tokens = m.get("max_tokens", 32768)
        tags = m.get("tags", [])
        
        product_features = m.get("product_features", {})
        attachments = product_features.get("attachments", {})
        has_image_support = "image_mime_types" in attachments and len(attachments["image_mime_types"]) > 0
        
        is_vision = "vision" in tags or "multimodal" in tags or "gpt4" in tags or has_image_support
        is_tools = "tools" in tags or "functions" in tags or "gpt4" in tags or "o1" in slug or "gpt-4o" in slug
        is_search = "browsing" in tags or "search" in tags or "web" in tags
        is_data = "code" in tags or "data" in tags or "analysis" in tags or "code_interpreter" in tags
        
        # Simple heuristic for tiers based on model name
        tier = "Tier 2: Standard"
        if "mini" in slug or "haiku" in slug or "flash" in slug:
            tier = "Tier 1: Fast"
        elif "o1" in slug or "opus" in slug or "pro" in slug:
            tier = "Tier 3: Reasoning"
            
        models_out.append({
            "id": slug,
            "context_length": max_tokens,
            "vision": is_vision,
            "tools": is_tools,
            "search": is_search,
            "data": is_data,
            "tier": tier
        })
        
    return {"models": models_out}
