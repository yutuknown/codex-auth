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
    usage_file = Path(os.path.expanduser("~")) / ".codex" / "usage.json"
    if usage_file.exists():
        try:
            with open(usage_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"history": []}
