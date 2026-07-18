import json
import os
import threading
from pathlib import Path
from typing import Dict, Any

_usage_lock = threading.Lock()

# Map model slug to (input_price_per_1M, output_price_per_1M)
PRICING = {
    "gpt-5-5": (5.00, 30.00),
    "gpt-5-3": (5.00, 30.00),
    "gpt-5-5-mini": (0.15, 0.60),
    "gpt-5-3-mini": (0.15, 0.60),
    "gpt-5-mini": (0.15, 0.60),
    "auto": (2.50, 10.00), # Fallback estimate
}

# Fallback default pricing if model is not specifically listed above
DEFAULT_PRICING = (2.50, 10.00)

def get_usage_file() -> Path:
    usage_path = Path(__file__).resolve().parent.parent.parent / ".codex" / "usage.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    return usage_path

def load_usage() -> Dict[str, Any]:
    usage_file = get_usage_file()
    if not usage_file.exists():
        return {
            "total_requests": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_savings_usd": 0.0,
            "models": {}
        }
    try:
        with open(usage_file, "r") as f:
            return json.load(f)
    except Exception:
        # If file is corrupted or empty
        return {
            "total_requests": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_savings_usd": 0.0,
            "models": {}
        }

def save_usage(data: Dict[str, Any]) -> None:
    usage_file = get_usage_file()
    with open(usage_file, "w") as f:
        json.dump(data, f, indent=2)

def record_usage(model_slug: str, input_tokens: int, output_tokens: int) -> None:
    with _usage_lock:
        data = load_usage()
        
        # Pricing calculation
        in_price_1m, out_price_1m = PRICING.get(model_slug, DEFAULT_PRICING)
        cost = (input_tokens / 1_000_000) * in_price_1m + (output_tokens / 1_000_000) * out_price_1m
        
        # Update totals
        data["total_requests"] = data.get("total_requests", 0) + 1
        data["total_input_tokens"] = data.get("total_input_tokens", 0) + input_tokens
        data["total_output_tokens"] = data.get("total_output_tokens", 0) + output_tokens
        data["total_savings_usd"] = data.get("total_savings_usd", 0.0) + cost
        
        # Update model specifics
        if "models" not in data:
            data["models"] = {}
        
        if model_slug not in data["models"]:
            data["models"][model_slug] = {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0
            }
            
        data["models"][model_slug]["requests"] += 1
        data["models"][model_slug]["input_tokens"] += input_tokens
        data["models"][model_slug]["output_tokens"] += output_tokens
        
        save_usage(data)

def format_tokens(num: int) -> str:
    """Format token count in K or M for sleek display."""
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)
