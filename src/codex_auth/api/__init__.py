import sys
import logging
from collections import deque
import webbrowser
import threading
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
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
            log_stream.append({
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "message": msg_clean
            })
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

app = FastAPI(title="ChatGPT Stealth API", lifespan=app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Import and include routers below to avoid circular imports
from .routes_openai import router as openai_router
from .routes_ollama import router as ollama_router
from .routes_ui import router as ui_router

app.include_router(openai_router)
app.include_router(ollama_router)
app.include_router(ui_router)
