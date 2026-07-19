import logging
import threading
import time
import webbrowser
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from rich.logging import RichHandler

from ..core.browser import engine
from ..providers.openai.provider import provider

# Store the last 500 logs in memory for the UI dashboard
log_stream = deque(maxlen=500)

class StreamHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # Remove rich markup tags for clean HTML rendering
            import re
            msg_clean = re.sub(r'\[/?(?:dim|cyan|red|green|yellow)\]', '', msg)
            log_entry = {
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "message": msg_clean
            }
            if hasattr(record, 'trace_data'):
                log_entry['trace_data'] = record.trace_data
            if hasattr(record, 'is_http'):
                log_entry['is_http'] = True
                log_entry['method'] = getattr(record, 'method', '')
                log_entry['path'] = getattr(record, 'path', '')
                log_entry['status'] = getattr(record, 'status', 0)
                log_entry['latency_ms'] = getattr(record, 'latency_ms', 0)
            log_stream.append(log_entry)
        except Exception:
            pass

# Setup professional minimalist logging for both console and dashboard
logging.basicConfig(
    level=logging.INFO,
    format="  [dim]●[/dim] %(message)s",
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
    # Start the browser auto-open in a background thread so it doesn't block startup
    threading.Thread(target=auto_open_dashboard, daemon=True).start()
    
    # Delegate the heavy Playwright initialization to the generic engine
    async with engine.lifespan():
        # Initialize the configured provider
        await provider.initialize(engine)
        yield

from fastapi import Request

app = FastAPI(title="ChatGPT Stealth API", lifespan=app_lifespan)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    path = request.url.path
    if not path.startswith(("/dashboard", "/api/logs", "/api/usage", "/api/status", "/api/models_list")):
        logger.info(f"{request.method} {path} {response.status_code}", extra={
            "is_http": True,
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "latency_ms": round(process_time * 1000, 2)
        })
    
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Import and include routers below to avoid circular imports
import os

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
