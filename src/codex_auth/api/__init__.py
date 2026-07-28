import hashlib
import itertools
import logging
import os
import secrets
import threading
import time
import webbrowser
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from rich.logging import RichHandler

from ..providers.openai.provider import provider
from .trace_context import request_trace_id

# Store the last 500 logs in memory for the UI dashboard
log_stream = deque(maxlen=500)
log_sequence = itertools.count(1)
pending_request_traces = OrderedDict()
log_stream_lock = threading.Lock()
MAX_PENDING_REQUEST_TRACES = 100
MAX_REQUEST_BODY_BYTES = 30 * 1024 * 1024
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}


def sanitized_headers(headers) -> dict[str, str]:
    return {
        name.lower(): "[REDACTED]" if name.lower() in SENSITIVE_HEADER_NAMES else value[:512]
        for name, value in headers.items()
    }

class StreamHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # Remove rich markup tags for clean HTML rendering
            import re
            msg_clean = re.sub(r'\[/?(?:dim|cyan|red|green|yellow)\]', '', msg)
            log_entry = {
                "sequence": next(log_sequence),
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "message": msg_clean
            }
            request_id = getattr(record, "request_id", "")
            if request_id:
                log_entry["request_id"] = request_id
            trace_data = getattr(record, "trace_data", None)
            if trace_data:
                trace_data = dict(trace_data)
                if request_id:
                    trace_data.setdefault("request_id", request_id)
            if hasattr(record, 'is_http'):
                log_entry['is_http'] = True
                log_entry['method'] = getattr(record, 'method', '')
                log_entry['path'] = getattr(record, 'path', '')
                log_entry['status'] = getattr(record, 'status', 0)
                log_entry['latency_ms'] = getattr(record, 'latency_ms', 0)
                log_entry["request_headers"] = getattr(record, "request_headers", {})
                log_entry["response_headers"] = getattr(record, "response_headers", {})
            with log_stream_lock:
                if trace_data and request_id:
                    for existing in reversed(log_stream):
                        if existing.get("is_http") and existing.get("request_id") == request_id:
                            trace_data.setdefault("request_headers", existing.get("request_headers", {}))
                            trace_data.setdefault("response_headers", existing.get("response_headers", {}))
                            existing["trace_data"] = trace_data
                            existing["trace_message"] = msg_clean
                            return
                    pending_request_traces[request_id] = (trace_data, msg_clean)
                    pending_request_traces.move_to_end(request_id)
                    while len(pending_request_traces) > MAX_PENDING_REQUEST_TRACES:
                        pending_request_traces.popitem(last=False)
                    return
                if log_entry.get("is_http") and request_id:
                    pending = pending_request_traces.pop(request_id, None)
                    if pending:
                        log_entry["trace_data"], log_entry["trace_message"] = pending
                        log_entry["trace_data"].setdefault(
                            "request_headers", log_entry["request_headers"]
                        )
                        log_entry["trace_data"].setdefault(
                            "response_headers", log_entry["response_headers"]
                        )
                elif trace_data:
                    log_entry["trace_data"] = trace_data
                log_stream.append(log_entry)
        except Exception:
            pass

# Setup professional minimalist logging for both console and dashboard
logging.basicConfig(
    level=logging.INFO,
    format="  [dim]*[/dim] %(message)s",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True, show_time=False, show_path=False, show_level=False),
        StreamHandler()
    ]
)
logger = logging.getLogger("codex_auth")

def auto_open_dashboard():
    # Wait a tiny bit for the server to bind before opening
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:8000/dashboard")

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    # Open the dashboard for local use, but not in hosted/container environments.
    open_dashboard = os.environ.get("CODEX_AUTH_OPEN_DASHBOARD", "true").lower()
    if open_dashboard not in {"0", "false", "no"}:
        threading.Thread(target=auto_open_dashboard, daemon=True).start()
    
    await provider.initialize()
    try:
        yield
    finally:
        await provider.close()

from fastapi import Request

app = FastAPI(title="ChatGPT Stealth API", lifespan=app_lifespan)


def api_key_is_valid(request: Request, expected_key: str) -> bool:
    authorization = request.headers.get("Authorization", "")
    bearer_key = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    supplied_key = bearer_key or request.headers.get("X-API-Key", "")
    if supplied_key and secrets.compare_digest(supplied_key, expected_key):
        return True
    expected_session = dashboard_session_value(expected_key)
    supplied_session = request.cookies.get("codex_auth_dashboard", "")
    return bool(supplied_session) and secrets.compare_digest(supplied_session, expected_session)


def dashboard_session_value(api_key: str) -> str:
    """Derive a browser-session value without storing the raw API key in the cookie."""
    return hashlib.sha256(f"codex-auth-dashboard:{api_key}".encode()).hexdigest()


@app.get("/healthz", include_in_schema=False)
async def healthcheck():
    return {"status": "ok"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    expected_key = os.environ.get("CODEX_AUTH_API_KEY")
    public_paths = {"/", "/healthz", "/login"}
    is_public = request.url.path in public_paths or request.url.path.startswith("/assets/")
    if expected_key and not is_public and not api_key_is_valid(request, expected_key):
        if request.url.path == "/dashboard":
            from fastapi.responses import RedirectResponse

            return RedirectResponse("/login", status_code=303)
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Invalid or missing API key", "type": "authentication_error"}},
            headers={"WWW-Authenticate": "Bearer"},
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "message": "Request body exceeds the 30 MB limit",
                            "type": "request_too_large",
                        }
                    },
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Invalid Content-Length header", "type": "invalid_request"}},
            )

    request_id = secrets.token_hex(8)
    request.state.request_id = request_id
    trace_token = request_trace_id.set(request_id)
    start_time = time.time()
    try:
        response = await call_next(request)
        process_time = time.time() - start_time

        if request.url.path in {"/dashboard", "/login", "/healthz"} or request.url.path.startswith(
            ("/api/", "/v1/", "/backend-api/")
        ):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-ID"] = request_id

        path = request.url.path
        quiet_paths = {
            "/dashboard",
            "/api/logs",
            "/api/usage",
            "/api/status",
            "/api/account",
            "/api/models_list",
        }
        if path not in quiet_paths:
            logger.info(f"{request.method} {path} {response.status_code}", extra={
                "is_http": True,
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "latency_ms": round(process_time * 1000, 2),
                "request_headers": sanitized_headers(request.headers),
                "response_headers": sanitized_headers(response.headers),
            })

        return response
    finally:
        request_trace_id.reset(trace_token)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Import and include routers below to avoid circular imports
from fastapi.staticfiles import StaticFiles

from .routes_ollama import router as ollama_router
from .routes_openai import router as openai_router
from .routes_ui import router as ui_router

app.include_router(openai_router)
app.include_router(ollama_router)
app.include_router(ui_router)

# Mount assets directory for images like the logo
assets_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets")
if os.path.exists(assets_path):
    app.mount("/assets", StaticFiles(directory=assets_path), name="assets")
