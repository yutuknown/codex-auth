"""Commit-bound, secret-free verification evidence for the hosted beta.

The hosted API never accepts a raw test transcript.  A campaign produces a
small structural manifest (status codes, timings and safe phases only), which
is canonicalised, hashed and retained only when it names the running build.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from beta.m365_durable import DurableCredentialError, PostgresCredentialStore, configured

VERIFICATION_CONTRACT_VERSION = "2026-08-03.1"
RENDER_COMMIT_ENV = "RENDER_GIT_COMMIT"
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_FORBIDDEN_KEYS = {
    "access_token", "refresh_token", "authorization", "cookie", "headers",
    "prompt", "response", "body", "url", "identity", "conversation_id",
}


def running_commit() -> str:
    """Return only a safe build identity; local development remains explicit."""

    value = os.environ.get(RENDER_COMMIT_ENV, "local-unversioned").strip()
    return value if _SAFE_VALUE.fullmatch(value) else "invalid-build-id"


def canonical_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate a deliberately narrow safe verification-manifest schema."""

    if not isinstance(manifest, dict):
        raise ValueError("verification manifest must be an object")
    if set(manifest).intersection(_FORBIDDEN_KEYS):
        raise ValueError("verification manifest contains prohibited data")
    tested_commit = str(manifest.get("tested_commit") or "")
    if not _SAFE_VALUE.fullmatch(tested_commit):
        raise ValueError("verification manifest has an invalid tested_commit")
    checks = manifest.get("checks")
    if not isinstance(checks, list) or len(checks) > 500:
        raise ValueError("verification manifest checks are invalid")
    safe_checks: list[dict[str, Any]] = []
    for item in checks:
        if not isinstance(item, dict) or set(item).intersection(_FORBIDDEN_KEYS):
            raise ValueError("verification check contains prohibited data")
        name = str(item.get("id") or "")
        outcome = str(item.get("outcome") or "")
        if not _SAFE_VALUE.fullmatch(name) or outcome not in {
            "passed", "failed", "intentional", "blocked_by_upstream", "mock_only",
        }:
            raise ValueError("verification check is invalid")
        safe: dict[str, Any] = {"id": name, "outcome": outcome}
        for field in ("http_status", "latency_ms", "bytes", "marker_observed"):
            value = item.get(field)
            if isinstance(value, bool) or (isinstance(value, int) and value >= 0):
                safe[field] = value
        phase = item.get("phase")
        if phase is not None and _SAFE_VALUE.fullmatch(str(phase)):
            safe["phase"] = str(phase)
        safe_checks.append(safe)
    safe_manifest = {
        "contract_version": VERIFICATION_CONTRACT_VERSION,
        "tested_commit": tested_commit,
        "completed_at": int(manifest.get("completed_at") or time.time()),
        "checks": sorted(safe_checks, key=lambda item: item["id"]),
    }
    canonical = json.dumps(safe_manifest, sort_keys=True, separators=(",", ":")).encode()
    return safe_manifest, hashlib.sha256(canonical).hexdigest()


class VerificationStore:
    """Durable verification summaries, available only with the credential DB."""

    def __init__(self) -> None:
        self._credentials = PostgresCredentialStore()

    def _ensure(self, connection: Any) -> None:
        self._credentials._ensure(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS codex_auth_m365_verification "
            "(id bigserial PRIMARY KEY, tested_commit text NOT NULL, digest text NOT NULL, "
            "manifest jsonb NOT NULL, completed_at bigint NOT NULL)"
        )

    def save(self, manifest: dict[str, Any]) -> dict[str, Any]:
        safe, digest = canonical_manifest(manifest)
        if safe["tested_commit"] != running_commit():
            raise ValueError("tested_commit does not match running build")
        with self._credentials._connect() as connection:
            self._ensure(connection)
            connection.execute(
                "INSERT INTO codex_auth_m365_verification(tested_commit,digest,manifest,completed_at) "
                "VALUES (%s,%s,%s,%s)",
                (safe["tested_commit"], digest, json.dumps(safe), safe["completed_at"]),
            )
        return {"tested_commit": safe["tested_commit"], "digest": digest, "completed_at": safe["completed_at"]}

    def latest(self) -> dict[str, Any] | None:
        with self._credentials._connect() as connection:
            self._ensure(connection)
            row = connection.execute(
                "SELECT digest, manifest FROM codex_auth_m365_verification "
                "WHERE tested_commit=%s ORDER BY id DESC LIMIT 1", (running_commit(),)
            ).fetchone()
        if row is None:
            return None
        manifest = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        return {"tested_commit": running_commit(), "digest": str(row[0]), "manifest": manifest}


def safe_latest_verification() -> dict[str, Any]:
    if not configured():
        return {"state": "unavailable", "reason": "durable_storage_not_configured"}
    try:
        latest = VerificationStore().latest()
    except DurableCredentialError:
        return {"state": "unavailable", "reason": "durable_storage_unavailable"}
    if latest is None:
        return {"state": "not_verified", "tested_commit": running_commit()}
    outcomes = [item["outcome"] for item in latest["manifest"]["checks"]]
    return {
        "state": "verified" if "failed" not in outcomes else "verification_failed",
        "tested_commit": latest["tested_commit"],
        "digest": latest["digest"],
        "completed_at": latest["manifest"]["completed_at"],
        "check_counts": {outcome: outcomes.count(outcome) for outcome in sorted(set(outcomes))},
        "passed_evidence_ids": [
            item["id"] for item in latest["manifest"]["checks"]
            if item["outcome"] == "passed"
        ],
    }
