"""Browser-assisted import of a newly acquired M365 OAuth response.

Microsoft SPA refresh tokens can require a new authorization-code cycle.  This
command accepts the resulting local response JSON, keeps route metadata, and
atomically replaces only active credential fields.  It never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from beta.m365_bearer import (
    BetaConfigurationError,
    _atomic_write_json,
    _read_json,
    default_beta_directory,
)


REQUIRED = ("token_type", "access_token", "refresh_token", "expires_in", "scope")


def _validate_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BetaConfigurationError("authorization response must be a JSON object")
    if str(value.get("token_type") or "").lower() != "bearer":
        raise BetaConfigurationError("authorization response requires bearer token_type")
    if any(not str(value.get(key) or "").strip() for key in REQUIRED):
        raise BetaConfigurationError("authorization response is missing required token fields")
    try:
        if int(value["expires_in"]) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise BetaConfigurationError("authorization response expires_in must be positive") from exc
    return value


def import_authorization_response(
    response: dict[str, Any], directory: Path | None = None
) -> dict[str, Any]:
    """Atomically import a browser-acquired response without returning tokens."""

    response = _validate_response(response)
    base = directory or default_beta_directory()
    destination = base / "ms365-auth.json"
    existing = _read_json(destination)
    preserved = {
        key: existing[key]
        for key in ("route", "resources", "model_catalog", "model_aliases", "variants")
        if key in existing
    }
    credential_fields = {
        key: response[key]
        for key in (
            "token_type", "scope", "expires_in", "ext_expires_in", "access_token",
            "refresh_token", "refresh_token_expires_in", "id_token", "client_info",
        )
        if key in response
    }
    merged = {**preserved, **credential_fields, "captured_at": int(time.time())}
    _atomic_write_json(destination, merged)
    return {
        "state": "active",
        "cookie_count": 0,
        "refresh_available": bool(merged.get("refresh_token")),
        "route_preserved": "route" in preserved,
        "secrets_returned": False,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Import a browser-acquired M365 OAuth response")
    parser.add_argument("response_file", type=Path)
    parser.add_argument("--directory", type=Path, default=None)
    args = parser.parse_args()
    try:
        response = json.loads(args.response_file.read_text(encoding="utf-8"))
        print(json.dumps(import_authorization_response(response, args.directory), sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, BetaConfigurationError) as exc:
        print(json.dumps({"state": "re_import_required", "phase": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
