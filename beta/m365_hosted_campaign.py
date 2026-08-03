"""Redacted hosted acceptance runner for ``codex-auth-beta``.

It deliberately records only structural evidence.  Credentials are read from
the launching process and never written to the proof directory or stdout.
The database manifest is optional and is written only by an operator with the
separate durable-store configuration.
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


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Campaign:
    def __init__(self, base_url: str, api_key: str, admin_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.admin_key = admin_key
        self.checks: list[dict[str, Any]] = []
        self.commit = "unknown"

    def check(self, identifier: str, response: httpx.Response, allowed: set[int]) -> None:
        self.checks.append({
            "id": identifier,
            "outcome": "passed" if response.status_code in allowed else "failed",
            "http_status": response.status_code,
            "latency_ms": int(response.elapsed.total_seconds() * 1000),
            "bytes": len(response.content),
        })

    def intentional(self, identifier: str, response: httpx.Response, status: int) -> None:
        self.checks.append({
            "id": identifier,
            "outcome": "intentional" if response.status_code == status else "failed",
            "http_status": response.status_code,
            "latency_ms": int(response.elapsed.total_seconds() * 1000),
            "bytes": len(response.content),
        })

    def run(self) -> dict[str, Any]:
        with httpx.Client(base_url=self.base_url, timeout=90, follow_redirects=False) as client:
            for method, path, identifier in [
                ("GET", "/", "root_get"), ("HEAD", "/", "root_head"),
                ("GET", "/openapi.json", "openapi"), ("GET", "/admin/credentials", "admin_page"),
                ("GET", "/health", "health"), ("GET", "/account-limits", "account_limits"),
            ]:
                self.check(identifier, client.request(method, path), {200})
            health = client.get("/health")
            try:
                self.commit = str(health.json().get("build", {}).get("render_commit") or "unknown")
            except (TypeError, ValueError):
                pass
            headers = {"x-api-key": self.api_key}
            for path, identifier in [
                ("/v1/deployment-readiness", "deployment_readiness"),
                ("/v1/models", "models"), ("/v1/capabilities", "capabilities"),
                ("/v1/research", "research"), ("/v1/verification", "verification"),
                ("/v1/metrics", "metrics"), ("/v1/logs", "logs"),
            ]:
                self.check(identifier, client.get(path, headers=headers), {200})
            self.intentional(
                "unknown_model", client.get("/v1/models/not-a-model", headers=headers), 404
            )
            self.intentional(
                "count_tokens", client.post("/v1/messages/count_tokens", headers=headers, json={}), 501
            )
            self.intentional(
                "invalid_api_key", client.get("/v1/models", headers={"x-api-key": "invalid"}), 401
            )
            self.intentional(
                "refresh_invalid_admin", client.post("/refresh-token", headers={"x-admin-key": "invalid"}), 401
            )
            # This marker is not saved.  Only whether the marker was observed is retained.
            marker = "M365_HOSTED_MARKER_7F3C"
            for path, identifier, payload in [
                ("/v1/messages", "anthropic_text_stream", {"model": "auto", "messages": [{"role": "user", "content": f"Reply exactly {marker}"}]}),
                ("/v1/chat/completions", "chat_text_stream", {"model": "auto", "messages": [{"role": "user", "content": f"Reply exactly {marker}"}]}),
                ("/v1/responses", "responses_text_stream", {"model": "auto", "input": f"Reply exactly {marker}"}),
            ]:
                response = client.post(path, headers=headers, json=payload)
                self.check(identifier, response, {200})
                self.checks[-1]["marker_observed"] = marker.encode() in response.content
            unsupported = client.post("/v1/chat/completions", headers=headers, json={"model": "auto", "messages": [], "tools": [{"type": "function"}]})
            self.intentional("unsupported_controls", unsupported, 400)
        return {"tested_commit": self.commit, "completed_at": int(time.time()), "checks": self.checks}


def _write_bundle(directory: Path, manifest: dict[str, Any], digest: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    root = ET.Element("testsuite", name="m365-hosted-campaign", tests=str(len(manifest["checks"])))
    for check in manifest["checks"]:
        case = ET.SubElement(root, "testcase", name=check["id"])
        if check["outcome"] == "failed":
            ET.SubElement(case, "failure", message="safe campaign failure")
    ET.ElementTree(root).write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
    (directory / "sha256sums.txt").write_text(f"{digest}  manifest.json\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--store", action="store_true", help="write safe manifest to configured Postgres")
    args = parser.parse_args()
    api_key = os.environ.get(API_KEY_ENV, "")
    admin_key = os.environ.get(ADMIN_KEY_ENV, "")
    if not api_key or not admin_key:
        raise SystemExit("API and admin keys must be supplied by process environment")
    raw = Campaign(args.base_url, api_key, admin_key).run()
    manifest, digest = canonical_manifest(raw)
    _write_bundle(args.proof_dir, manifest, digest)
    if args.store:
        VerificationStore().save(manifest)
    failures = sum(item["outcome"] == "failed" for item in manifest["checks"])
    print(json.dumps({"tested_commit": manifest["tested_commit"], "digest": digest, "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
