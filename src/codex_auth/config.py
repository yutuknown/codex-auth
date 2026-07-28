import json
import os
from pathlib import Path
from typing import Any, Dict


def get_cookie_file() -> Path:
    """Return the configured Netscape-format ChatGPT cookie file."""
    configured_path = os.environ.get("CODEX_AUTH_COOKIE_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "cookies.txt"


def get_auth_file() -> Path:
    """Return the configured Codex authentication file."""
    configured_path = os.environ.get("CODEX_AUTH_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "auth.json"


def auth_is_configured() -> bool:
    return (
        bool(os.environ.get("CODEX_AUTH_COOKIES"))
        or get_cookie_file().exists()
        or bool(os.environ.get("CODEX_AUTH_JSON"))
        or get_auth_file().exists()
    )


def load_cookie_text() -> str:
    """Load Netscape cookies from an environment secret or local file."""
    inline_cookies = os.environ.get("CODEX_AUTH_COOKIES")
    if inline_cookies:
        return inline_cookies

    cookie_file = get_cookie_file()
    if not cookie_file.exists():
        raise FileNotFoundError(
            f"Could not find cookies.txt at {cookie_file}. "
            "Export ChatGPT cookies in Netscape format or set CODEX_AUTH_COOKIES."
        )
    return cookie_file.read_text(encoding="utf-8")


def load_auth_data() -> Dict[str, Any]:
    inline_auth = os.environ.get("CODEX_AUTH_JSON")
    if inline_auth:
        return json.loads(inline_auth)

    auth_file = get_auth_file()
    if not auth_file.exists():
        raise FileNotFoundError(f"Could not find auth.json at {auth_file}")
    with auth_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)
