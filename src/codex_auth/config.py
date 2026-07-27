import json
import os
from pathlib import Path
from typing import Any, Dict


def get_auth_file() -> Path:
    """Return the configured Codex authentication file."""
    configured_path = os.environ.get("CODEX_AUTH_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "auth.json"


def auth_is_configured() -> bool:
    return bool(os.environ.get("CODEX_AUTH_JSON")) or get_auth_file().exists()


def load_auth_data() -> Dict[str, Any]:
    inline_auth = os.environ.get("CODEX_AUTH_JSON")
    if inline_auth:
        return json.loads(inline_auth)

    auth_file = get_auth_file()
    if not auth_file.exists():
        raise FileNotFoundError(f"Could not find auth.json at {auth_file}")
    with auth_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)
