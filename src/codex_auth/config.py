import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

PROVIDER_COOKIE_ENV = {
    "openai-web": ("CODEX_AUTH_COOKIES", "CODEX_AUTH_COOKIE_FILE", "cookies.txt"),
    "m365-copilot": (
        "CODEX_AUTH_M365_COOKIES",
        "CODEX_AUTH_M365_COOKIE_FILE",
        "m365-cookies.txt",
    ),
}


def get_provider_cookie_file(provider_id: str) -> Path:
    try:
        _, file_env, default_name = PROVIDER_COOKIE_ENV[provider_id]
    except KeyError as exc:
        raise ValueError(f"Provider '{provider_id}' has no cookie configuration") from exc
    configured_path = os.environ.get(file_env)
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / default_name


def load_provider_cookie_text(provider_id: str) -> str:
    try:
        inline_env, _, _ = PROVIDER_COOKIE_ENV[provider_id]
    except KeyError as exc:
        raise ValueError(f"Provider '{provider_id}' has no cookie configuration") from exc
    inline_cookies = os.environ.get(inline_env)
    if inline_cookies:
        return inline_cookies
    cookie_file = get_provider_cookie_file(provider_id)
    if not cookie_file.exists():
        raise FileNotFoundError(
            f"Could not find {provider_id} cookies at {cookie_file}. Set {inline_env} or upload a Netscape cookie file."
        )
    return cookie_file.read_text(encoding="utf-8")


def provider_cookies_are_configured(provider_id: str) -> bool:
    inline_env, _, _ = PROVIDER_COOKIE_ENV[provider_id]
    return bool(os.environ.get(inline_env)) or get_provider_cookie_file(provider_id).exists()


def save_provider_cookie_text(provider_id: str, text: str) -> Path:
    return _atomic_save_text(get_provider_cookie_file(provider_id), text)


def get_m365_auth_file() -> Path:
    configured_path = os.environ.get("CODEX_AUTH_M365_AUTH_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "m365-auth.json"


def get_m365_oauth_file() -> Path:
    configured_path = os.environ.get("CODEX_AUTH_M365_OAUTH_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "m365-oauth.json"


def get_m365_graph_file() -> Path:
    configured_path = os.environ.get("CODEX_AUTH_M365_GRAPH_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "m365-graph.json"


def get_m365_graph_oauth_file() -> Path:
    configured_path = os.environ.get("CODEX_AUTH_M365_GRAPH_OAUTH_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".codex" / "m365-graph-oauth.json"


def load_m365_auth_data() -> Dict[str, Any]:
    inline_auth = os.environ.get("CODEX_AUTH_M365_AUTH_JSON")
    if inline_auth:
        return json.loads(inline_auth)
    auth_file = get_m365_auth_file()
    if not auth_file.exists():
        return {}
    with auth_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_m365_auth_data(data: Dict[str, Any]) -> Path:
    return _atomic_save_json(get_m365_auth_file(), data)


def load_m365_oauth_data() -> Dict[str, Any]:
    inline_oauth = os.environ.get("CODEX_AUTH_M365_OAUTH_JSON")
    if inline_oauth:
        return json.loads(inline_oauth)
    oauth_file = get_m365_oauth_file()
    if not oauth_file.exists():
        return {}
    with oauth_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_m365_oauth_data(data: Dict[str, Any]) -> Path:
    return _atomic_save_json(get_m365_oauth_file(), data)


def load_m365_graph_data() -> Dict[str, Any]:
    inline_graph = os.environ.get("CODEX_AUTH_M365_GRAPH_JSON")
    if inline_graph:
        return json.loads(inline_graph)
    graph_file = get_m365_graph_file()
    if not graph_file.exists():
        return {}
    with graph_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_m365_graph_data(data: Dict[str, Any]) -> Path:
    return _atomic_save_json(get_m365_graph_file(), data)


def load_m365_graph_oauth_data() -> Dict[str, Any]:
    inline_oauth = os.environ.get("CODEX_AUTH_M365_GRAPH_OAUTH_JSON")
    if inline_oauth:
        return json.loads(inline_oauth)
    oauth_file = get_m365_graph_oauth_file()
    if not oauth_file.exists():
        return {}
    with oauth_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_m365_graph_oauth_data(data: Dict[str, Any]) -> Path:
    return _atomic_save_json(get_m365_graph_oauth_file(), data)


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


def save_cookie_text(text: str) -> Path:
    """Atomically save Netscape cookies with owner-only file permissions where supported."""
    return _atomic_save_text(get_cookie_file(), text)


def _atomic_save_text(cookie_file: Path, text: str) -> Path:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=cookie_file.parent,
            prefix=f".{cookie_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        with contextlib.suppress(OSError):
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, cookie_file)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return cookie_file


def _atomic_save_json(destination: Path, data: Dict[str, Any]) -> Path:
    return _atomic_save_text(
        destination,
        json.dumps(data, indent=2, sort_keys=True),
    )


def load_auth_data() -> Dict[str, Any]:
    inline_auth = os.environ.get("CODEX_AUTH_JSON")
    if inline_auth:
        return json.loads(inline_auth)

    auth_file = get_auth_file()
    if not auth_file.exists():
        raise FileNotFoundError(f"Could not find auth.json at {auth_file}")
    with auth_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)
