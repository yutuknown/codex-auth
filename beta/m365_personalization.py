"""Explicitly opted-in local reads of private M365 personalization endpoints.

This module deliberately returns schema-only evidence. It is not imported by
the hosted compatibility API and never writes response bodies to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from typing import Any

from beta.m365_bearer import BETA_CONFIRM_ENV, USER_AGENT, BetaConfigurationError, BetaUpstreamError, M365BearerBeta

PERSONALIZATION_CONFIRM_ENV = "CODEX_AUTH_M365_BETA_PERSONALIZATION_CONFIRM"
MAX_BODY_BYTES = 512 * 1024
MAX_SCHEMA_DEPTH = 10
VARIANT = "feature.EnablePersonalizationForMSA"


def _schema(value: Any, prefix: str = "$", depth: int = 0) -> set[str]:
    if depth > MAX_SCHEMA_DEPTH:
        return {prefix + ":depth_limit"}
    if isinstance(value, dict):
        result = {prefix + ":object"}
        for key, child in value.items():
            # Field names are acceptable schema evidence; values never leave memory.
            result.update(_schema(child, f"{prefix}.{str(key)[:80]}", depth + 1))
        return result
    if isinstance(value, list):
        result = {prefix + ":array"}
        for child in value[:20]:
            result.update(_schema(child, prefix + "[]", depth + 1))
        return result
    return {prefix + ":" + ("null" if value is None else type(value).__name__)}


def _endpoint(name: str) -> tuple[str, dict[str, str]]:
    base = "https://substrate.office.com/m365Copilot/"
    if name == "custom_instructions":
        return base + "CustomInstructions", {"variants": VARIANT}
    if name == "memories":
        return base + "Memories", {
            "request": json.dumps({"source": "officeweb", "traceId": uuid.uuid4().hex}, separators=(",", ":")),
            "variants": VARIANT,
        }
    raise BetaConfigurationError("unknown personalization probe")


def probe(name: str, beta: M365BearerBeta | None = None) -> dict[str, Any]:
    if os.environ.get(BETA_CONFIRM_ENV) != "1" or os.environ.get(PERSONALIZATION_CONFIRM_ENV) != "1":
        raise BetaConfigurationError("personalization probes require both beta confirmation variables")
    active = beta or M365BearerBeta.from_directory()
    session = active._new_cookie_free_session()
    started = time.monotonic()
    try:
        endpoint, params = _endpoint(name)
        response = session.get(endpoint, params=params, headers={
            "Accept": "application/json", "Accept-Language": "en-gb",
            "Authorization": f"Bearer {active.credential.access_token}",
            "Origin": "https://m365.cloud.microsoft", "Referer": "https://m365.cloud.microsoft/",
            "User-Agent": USER_AGENT,
            "X-AnchorMailbox": f"MSA:{active.route.identity}",
            "X-Scenario": "OfficeWebPaidConsumerCopilot",
            "client-request-id": str(uuid.uuid4()),
        }, timeout=30)
        try:
            body = bytes(response.content)
            status = int(response.status_code)
        finally:
            response.close()
        result: dict[str, Any] = {"probe": name, "cookie_count": 0, "latency_ms": round((time.monotonic() - started) * 1000), "http_status": status}
        if len(body) > MAX_BODY_BYTES:
            return {**result, "state": "body_too_large", "body_bytes": len(body)}
        if status in {401, 403}:
            return {**result, "state": "authorization_blocked"}
        if status < 200 or status >= 300:
            return {**result, "state": "upstream_http_failure"}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {**result, "state": "protocol_drift", "json_valid": False}
        paths = sorted(_schema(value))
        digest = hashlib.sha256("\n".join(paths).encode()).hexdigest()
        return {
            **result,
            "state": "verified_private_read",
            "json_valid": True,
            "top_level_type": type(value).__name__,
            "schema_path_count": len(paths),
            "schema_paths": paths,
            "schema_digest": digest,
        }
    except BetaUpstreamError:
        raise
    except Exception as exc:
        raise BetaUpstreamError("personalization_probe_failed") from exc
    finally:
        session.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Local-only, schema-only M365 personalization probe")
    parser.add_argument("probe", choices=("custom_instructions", "memories", "all"))
    args = parser.parse_args()
    try:
        names = ("custom_instructions", "memories") if args.probe == "all" else (args.probe,)
        print(json.dumps({"results": [probe(name) for name in names]}, sort_keys=True))
        return 0
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        print(json.dumps({"state": "not_run", "phase": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
