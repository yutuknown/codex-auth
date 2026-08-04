"""Redacted, commit-bound acceptance campaign for the hosted M365 beta.

The runner intentionally retains only structural evidence: HTTP status, elapsed
time, response size, event names, and fixed-marker booleans.  It neither writes
credentials nor captures generated text, headers, URLs, identities, or upstream
conversation identifiers.  Credential-dependent checks are skipped after the
single permitted refresh attempt fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from beta.m365_verification import VerificationStore, canonical_manifest

API_KEY_ENV = "CODEX_AUTH_M365_BETA_API_KEY"
ADMIN_KEY_ENV = "CODEX_AUTH_M365_BETA_ADMIN_KEY"
EXPECTED_CONTRACT = "2026-08-03.1"
LIVE_OUTCOMES = {"passed", "intentional", "blocked_by_auth", "blocked_by_upstream"}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Campaign:
    def __init__(self, base_url: str, api_key: str, admin_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.admin_key = admin_key
        self.checks: list[dict[str, Any]] = []
        self.commit = "unknown"
        self.contract = "unknown"
        self.live_ready = False

    def _record(
        self,
        identifier: str,
        response: httpx.Response | None,
        outcome: str,
        *,
        phase: str | None = None,
        marker_observed: bool | None = None,
        event_types: set[str] | None = None,
    ) -> None:
        item: dict[str, Any] = {"id": identifier, "outcome": outcome}
        if response is not None:
            item.update({
                "http_status": response.status_code,
                "latency_ms": int(response.elapsed.total_seconds() * 1000),
                "bytes": len(response.content),
            })
        if phase:
            item["phase"] = phase
        if marker_observed is not None:
            item["marker_observed"] = marker_observed
        # Event names are reduced to a bounded count/digest-safe phase. They
        # are not persisted in the verification manifest.
        if event_types:
            item["phase"] = "sse_" + "_".join(sorted(event_types)[:4])
        self.checks.append(item)

    def _record_values(
        self,
        identifier: str,
        status: int,
        latency_ms: int,
        size: int,
        outcome: str,
        *,
        phase: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "id": identifier,
            "outcome": outcome,
            "http_status": status,
            "latency_ms": latency_ms,
            "bytes": size,
        }
        if phase:
            item["phase"] = phase
        self.checks.append(item)

    def check(self, identifier: str, response: httpx.Response, allowed: set[int]) -> None:
        self._record(identifier, response, "passed" if response.status_code in allowed else "failed")

    def intentional(self, identifier: str, response: httpx.Response, status: int) -> None:
        self._record(identifier, response, "intentional" if response.status_code == status else "failed")

    def blocked_auth(self, identifier: str) -> None:
        self._record(identifier, None, "blocked_by_auth", phase="credential_unusable")

    @property
    def api_headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key}

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"x-admin-key": self.admin_key}

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _sse_shape(payload: bytes, marker: bytes) -> tuple[bool, set[str]]:
        """Return only event labels and marker presence from an SSE response."""

        events: set[str] = set()
        for line in payload.decode("utf-8", errors="replace").splitlines():
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                if event and len(event) <= 64 and event.replace(".", "").replace("_", "").isalnum():
                    events.add(event)
        return marker in payload, events

    def _preflight(self, client: httpx.Client) -> dict[str, Any]:
        for method, path, identifier in [
            ("GET", "/", "root_get"), ("HEAD", "/", "root_head"),
            ("GET", "/openapi.json", "openapi"),
            ("GET", "/admin/credentials", "admin_page"),
            ("GET", "/health", "health"), ("GET", "/account-limits", "account_limits"),
        ]:
            response = client.request(method, path)
            self.check(identifier, response, {200})
        health_response = client.get("/health")
        health = self._safe_json(health_response)
        build = health.get("build") if isinstance(health.get("build"), dict) else {}
        self.commit = str(build.get("render_commit") or "unknown")
        self.contract = str(build.get("verification_contract") or "unknown")
        self._record(
            "build_contract",
            health_response,
            "passed" if self.commit != "unknown" and self.contract == EXPECTED_CONTRACT else "failed",
            phase="commit_bound" if self.contract == EXPECTED_CONTRACT else "contract_mismatch",
        )
        models_response: httpx.Response | None = None
        for path, identifier in [
            ("/v1/deployment-readiness", "deployment_readiness"),
            ("/v1/models", "models"), ("/v1/capabilities", "capabilities"),
            ("/v1/research", "research"), ("/v1/verification", "verification"),
            ("/v1/metrics", "metrics"), ("/v1/logs", "logs"),
        ]:
            response = client.get(path, headers=self.api_headers)
            self.check(identifier, response, {200})
            if identifier == "models":
                models_response = response
        catalog = self._safe_json(models_response) if models_response is not None else {}
        entries = catalog.get("data") if isinstance(catalog.get("data"), list) else []
        model_id = next((str(item.get("id")) for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str)), "")
        if model_id:
            self.check("known_model", client.get(f"/v1/models/{model_id}", headers=self.api_headers), {200})
        else:
            self._record("known_model", None, "failed", phase="catalog_has_no_public_model")
        started = time.monotonic()
        try:
            with client.stream("GET", "/v1/logs/stream", headers=self.api_headers, timeout=10) as stream:
                first = next(stream.iter_bytes())
                observed = b"event: heartbeat" in first or b"event: telemetry" in first
                self._record_values(
                    "logs_stream",
                    stream.status_code,
                    int((time.monotonic() - started) * 1000),
                    len(first),
                    "passed" if stream.status_code == 200 and observed else "failed",
                    phase="sse_heartbeat" if observed else "sse_missing_event",
                )
        except (httpx.HTTPError, StopIteration):
            self._record("logs_stream", None, "failed", phase="sse_connection_failed")
        self.intentional("unknown_model", client.get("/v1/models/not-a-model", headers=self.api_headers), 404)
        self.intentional("count_tokens", client.post("/v1/messages/count_tokens", headers=self.api_headers, json={}), 501)
        self.intentional("invalid_api_key", client.get("/v1/models", headers={"x-api-key": "invalid"}), 401)
        self.intentional("refresh_invalid_admin", client.post("/refresh-token", headers={"x-admin-key": "invalid"}), 401)
        self.intentional(
            "credential_import_malformed",
            client.post("/admin/credentials/import", headers={**self.admin_headers, "content-type": "application/json"}, json={"credential": "invalid"}),
            400,
        )
        return health

    def _refresh_gate(self, client: httpx.Client, health: dict[str, Any]) -> None:
        persistence = health.get("credential_persistence") if isinstance(health.get("credential_persistence"), dict) else {}
        durable = bool(persistence.get("restart_durable")) and persistence.get("source") == "encrypted_external_postgres"
        generation_ready = bool(health.get("generation_ready"))
        if not durable:
            self._record("durable_credential_store", None, "failed", phase="durable_storage_not_configured")
            return
        self._record("durable_credential_store", None, "passed", phase="encrypted_external_postgres")
        if generation_ready:
            self._record("refresh_gate", None, "passed", phase="already_active")
            self.live_ready = True
            return
        # Exactly one production refresh attempt. Its result determines whether
        # any credentials-dependent request is permitted in this campaign.
        response = client.post("/refresh-token", headers=self.admin_headers)
        if response.status_code == 200:
            refreshed = self._safe_json(response)
            credential = refreshed.get("credential") if isinstance(refreshed.get("credential"), dict) else {}
            if credential.get("generation_ready"):
                self._record("refresh_gate", response, "passed", phase="single_refresh_succeeded")
                self.live_ready = True
                return
        self._record("refresh_gate", response, "blocked_by_auth", phase="single_refresh_rejected")

    def _negative_request_matrix(self, client: httpx.Client) -> None:
        cases = [
            ("unsupported_tools", {"model": "auto", "messages": [], "tools": [{"type": "function"}]}),
            ("unsupported_sampling", {"model": "auto", "messages": [], "temperature": 0.2}),
            ("unsupported_stop", {"model": "auto", "messages": [], "stop": ["x"]}),
            ("unsupported_max_tokens", {"model": "auto", "messages": [], "max_tokens": 1}),
        ]
        for identifier, payload in cases:
            self.intentional(identifier, client.post("/v1/chat/completions", headers=self.api_headers, json=payload), 400)
        self.intentional("malformed_chat", client.post("/v1/chat/completions", headers=self.api_headers, json={"model": "auto"}), 400)

    def _text_and_stream_matrix(self, client: httpx.Client) -> None:
        marker = "M365_HOSTED_MARKER_7F3C"
        marker_bytes = marker.encode()
        routes = [
            ("/v1/messages", "anthropic", {"model": "auto", "messages": [{"role": "user", "content": f"Reply exactly {marker}"}]}),
            ("/v1/chat/completions", "chat", {"model": "auto", "messages": [{"role": "user", "content": f"Reply exactly {marker}"}]}),
            ("/v1/responses", "responses", {"model": "auto", "input": f"Reply exactly {marker}"}),
        ]
        for path, name, payload in routes:
            buffered = client.post(path, headers=self.api_headers, json=payload)
            observed = marker_bytes in buffered.content
            self._record(f"{name}_text_buffered", buffered, "passed" if buffered.status_code == 200 and observed else "failed", marker_observed=observed)
            streamed = client.post(path, headers=self.api_headers, json={**payload, "stream": True})
            observed, event_types = self._sse_shape(streamed.content, marker_bytes)
            self._record(f"{name}_text_stream", streamed, "passed" if streamed.status_code == 200 and observed else "failed", marker_observed=observed, event_types=event_types)

    def _continuity_matrix(self, client: httpx.Client) -> None:
        conversation = "campaign-continuity-v1"
        first = client.post("/v1/responses", headers={**self.api_headers, "x-codex-conversation-id": conversation}, json={"model": "auto", "input": "Reply exactly CONTINUITY_ONE"})
        second = client.post("/v1/responses", headers={**self.api_headers, "x-codex-conversation-id": conversation}, json={"model": "auto", "input": "Reply exactly CONTINUITY_TWO"})
        first_id = first.headers.get("x-codex-conversation-id", "")
        second_id = second.headers.get("x-codex-conversation-id", "")
        self._record("conversation_continuity", second, "passed" if first.status_code == second.status_code == 200 and first_id and first_id == second_id else "failed", phase="continuation_header")

    def run(self) -> dict[str, Any]:
        with httpx.Client(base_url=self.base_url, timeout=120, follow_redirects=False) as client:
            health = self._preflight(client)
            self._negative_request_matrix(client)
            self._refresh_gate(client, health)
            if not self.live_ready:
                for identifier in ("text_generation", "streaming", "conversation_continuity", "model_matrix", "attachment_matrix", "artifact_matrix", "reliability_load"):
                    self.blocked_auth(identifier)
            else:
                self._text_and_stream_matrix(client)
                self._continuity_matrix(client)
                # Separate fixture-driven checks are intentionally not inferred
                # from a text response. They are only promoted by their own
                # upload/readback and byte-validation stages.
                self._record("model_matrix", None, "blocked_by_upstream", phase="requires_live_catalog_capture")
                self._record("attachment_matrix", None, "blocked_by_upstream", phase="requires_graph_fixture_campaign")
                self._record("artifact_matrix", None, "blocked_by_upstream", phase="requires_generated_artifact_capture")
                self._record("reliability_load", None, "blocked_by_upstream", phase="requires_isolated_load_window")
        return {
            "tested_commit": self.commit,
            "completed_at": int(time.time()),
            "checks": self.checks,
        }


def _write_bundle(directory: Path, manifest: dict[str, Any], digest: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    root = ET.Element("testsuite", name="m365-hosted-campaign", tests=str(len(manifest["checks"])))
    for check in manifest["checks"]:
        case = ET.SubElement(root, "testcase", name=check["id"])
        if check["outcome"] == "failed":
            ET.SubElement(case, "failure", message="safe campaign failure")
        elif check["outcome"].startswith("blocked_"):
            ET.SubElement(case, "skipped", message=check.get("phase", "blocked"))
    ET.ElementTree(root).write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
    (directory / "sha256sums.txt").write_text(f"{digest}  manifest.json\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--store", action="store_true", help="write safe manifest to configured Postgres")
    args = parser.parse_args()
    api_key = os.environ.get(API_KEY_ENV, "")
    # The service deliberately falls back to the API key for its isolated beta
    # admin interface. A distinct admin key is still preferred when provided.
    admin_key = os.environ.get(ADMIN_KEY_ENV) or api_key
    if not api_key:
        raise SystemExit("API key must be supplied by process environment")
    raw = Campaign(args.base_url, api_key, admin_key).run()
    manifest, digest = canonical_manifest(raw)
    _write_bundle(args.proof_dir, manifest, digest)
    if args.store:
        VerificationStore().save(manifest)
    failures = sum(item["outcome"] == "failed" for item in manifest["checks"])
    blocked_auth = sum(item["outcome"] == "blocked_by_auth" for item in manifest["checks"])
    print(json.dumps({"tested_commit": manifest["tested_commit"], "digest": digest, "failures": failures, "blocked_by_auth": blocked_auth}))
    return 1 if failures or blocked_auth else 0


if __name__ == "__main__":
    raise SystemExit(main())
