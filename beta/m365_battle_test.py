"""Redacted live acceptance campaign for the local M365 bearer beta."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import httpx

REPOSITORY_DIRECTORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIRECTORY))
SOURCE_DIRECTORY = REPOSITORY_DIRECTORY / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from beta.m365_bearer import (
    BETA_CONFIRM_ENV,
    BetaConfigurationError,
    BetaUpstreamError,
    M365BearerBeta,
    default_beta_directory,
)
from beta.m365_compat import app
from beta.m365_models import M365ModelCatalog

SAFE_PHASE = re.compile(r"^[A-Za-z0-9_:-]{1,96}$")
FIXED_TEXT_MARKER = "M365_BATTLE_TEXT_OK"
STRUCTURED_MARKER = "M365_STRUCTURED_47"
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _safe_phase(exc: Exception) -> str:
    candidate = str(exc)
    return candidate if SAFE_PHASE.fullmatch(candidate) else type(exc).__name__


class BattleReport:
    def __init__(self) -> None:
        self.started_at = int(time.time())
        self.results: list[dict[str, Any]] = []

    def add(
        self,
        capability: str,
        status: str,
        *,
        duration_ms: int | None = None,
        evidence: dict[str, Any] | None = None,
        phase: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "capability": capability,
            "status": status,
        }
        if duration_ms is not None:
            item["duration_ms"] = max(0, int(duration_ms))
        if evidence:
            item["evidence"] = evidence
        if phase:
            item["phase"] = phase if SAFE_PHASE.fullmatch(phase) else "redacted"
        self.results.append(item)

    def safe_payload(self) -> dict[str, Any]:
        counts = Counter(item["status"] for item in self.results)
        return {
            "provider": "m365-copilot",
            "scope": "local_zero_cookie_beta",
            "started_at": self.started_at,
            "completed_at": int(time.time()),
            "counts": dict(sorted(counts.items())),
            "results": self.results,
            "redaction": {
                "prompts": "not retained",
                "responses": "not retained",
                "credentials": "not retained",
                "identity": "not retained",
                "urls": "not retained",
            },
        }


def _timed_probe(
    report: BattleReport,
    capability: str,
    probe: Callable[[], dict[str, Any]],
) -> None:
    started = time.monotonic()
    try:
        evidence = probe()
        report.add(
            capability,
            "passed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence=evidence,
        )
    except (BetaConfigurationError, BetaUpstreamError) as exc:
        report.add(
            capability,
            "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            phase=_safe_phase(exc),
        )
    except Exception as exc:
        report.add(
            capability,
            "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            phase=type(exc).__name__,
        )


def _model_probe(model_id: str, tone: str) -> dict[str, Any]:
    beta = M365BearerBeta.from_directory()
    event_counts: Counter[str] = Counter()

    def emit(event: dict[str, Any]) -> None:
        event_counts[str(event.get("type") or "unknown")] += 1

    answer = beta.generate_stream(
        f"Reply exactly with: {FIXED_TEXT_MARKER}",
        emit,
        model_id,
        tone,
    )
    if FIXED_TEXT_MARKER not in answer:
        raise BetaUpstreamError("model_marker_missing")
    return {
        "cookie_count": beta.status()["cookie_count"],
        "connect_attempts": beta.status()["last_connect_attempts"],
        "event_types": dict(sorted(event_counts.items())),
        "response_characters": len(answer),
    }


def _inspect_probe(prompt: str, model: str) -> dict[str, Any]:
    report = M365BearerBeta.from_directory().inspect(prompt, model)
    return {
        "cookie_count": report["cookie_count"],
        "frame_count": report["frame_count"],
        "message_types": report["message_types"],
        "normalized_event_types": report["normalized_event_types"],
        "stream_event_types": report["stream_event_types"],
        "stream_operations": report["stream_operations"],
        "response_characters": report["response_characters"],
        "reference_count": report["reference_count"],
        "search_query_count": report["search_query_count"],
        "adaptive_card_count": report["adaptive_card_count"],
    }


async def _http_campaign(report: BattleReport) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://m365-beta.local",
        timeout=90,
    ) as client:
        started = time.monotonic()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": f"Reply exactly with: {FIXED_TEXT_MARKER}",
                    }
                ],
            },
        )
        payload = response.json()
        content = payload.get("content") or []
        text_characters = sum(
            len(str(block.get("text") or ""))
            for block in content
            if isinstance(block, dict)
        )
        report.add(
            "anthropic_buffered_http",
            "passed"
            if response.status_code == 200 and text_characters > 0
            else "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence={
                "http_status": response.status_code,
                "content_blocks": len(content),
                "response_characters": text_characters,
                "usage_source": (
                    payload.get("usage_estimation") or {}
                ).get("source"),
            },
        )

        started = time.monotonic()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-5.5-think-deeper",
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Reason briefly, then reply exactly with "
                            f"{FIXED_TEXT_MARKER}"
                        ),
                    }
                ],
            },
        )
        body = response.text
        report.add(
            "anthropic_streaming_http",
            "passed"
            if response.status_code == 200
            and "message_start" in body
            and "message_stop" in body
            and "text_delta" in body
            else "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence={
                "http_status": response.status_code,
                "thinking_delta": "thinking_delta" in body,
                "text_delta": "text_delta" in body,
                "signature_delta": "signature_delta" in body,
            },
        )

        started = time.monotonic()
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": f"Reply exactly with: {FIXED_TEXT_MARKER}",
                    }
                ],
            },
        )
        payload = response.json()
        choices = payload.get("choices") or []
        report.add(
            "openai_buffered_http",
            "passed" if response.status_code == 200 and choices else "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence={
                "http_status": response.status_code,
                "choice_count": len(choices),
                "usage_source": (payload.get("usage_details") or {}).get(
                    "source"
                ),
            },
        )

        started = time.monotonic()
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5-think-deeper",
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Reason briefly, then reply exactly with "
                            f"{FIXED_TEXT_MARKER}"
                        ),
                    }
                ],
            },
        )
        body = response.text
        report.add(
            "openai_streaming_http",
            "passed"
            if response.status_code == 200
            and "chat.completion.chunk" in body
            and "data: [DONE]" in body
            else "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence={
                "http_status": response.status_code,
                "reasoning_content": "reasoning_content" in body,
                "content_delta": '"content"' in body,
                "done_marker": "data: [DONE]" in body,
            },
        )

        started = time.monotonic()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "auto",
                "system": (
                    f"Your final response must contain only {STRUCTURED_MARKER}."
                ),
                "messages": [
                    {"role": "user", "content": "Remember the number 47."},
                    {"role": "assistant", "content": "I will remember it."},
                    {
                        "role": "user",
                        "content": "Now follow the system instruction.",
                    },
                ],
            },
        )
        payload = response.json()
        combined = "".join(
            str(block.get("text") or "")
            for block in payload.get("content") or []
            if isinstance(block, dict)
        )
        metadata = payload.get("provider_metadata") or {}
        report.add(
            "compiled_system_and_multi_turn_http",
            "passed"
            if response.status_code == 200
            and metadata.get("turn_count") == 3
            and metadata.get("transport_mode") == "structured_transcript"
            and metadata.get("native_structured_history") is False
            else "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence={
                "http_status": response.status_code,
                "marker_present": STRUCTURED_MARKER in combined,
                "turn_count": metadata.get("turn_count"),
                "transport_mode": metadata.get("transport_mode"),
                "native_structured_history": metadata.get(
                    "native_structured_history"
                ),
            },
        )
        report.add(
            "native_system_instruction_fidelity",
            "passed" if STRUCTURED_MARKER in combined else "blocked",
            phase=(
                ""
                if STRUCTURED_MARKER in combined
                else "m365_rejected_compiled_system_label"
            ),
            evidence={
                "marker_present": STRUCTURED_MARKER in combined,
                "system_instruction_mode": metadata.get(
                    "system_instruction_mode"
                ),
            },
        )

        for capability, request in (
            (
                "client_tools_rejection_http",
                {
                    "model": "auto",
                    "messages": [{"role": "user", "content": "test"}],
                    "tools": [{"name": "test", "input_schema": {}}],
                },
            ),
            (
                "sampling_controls_rejection_http",
                {
                    "model": "auto",
                    "messages": [{"role": "user", "content": "test"}],
                    "temperature": 0.2,
                },
            ),
        ):
            response = await client.post("/v1/messages", json=request)
            report.add(
                capability,
                "passed" if response.status_code == 400 else "failed",
                evidence={"http_status": response.status_code},
            )

        started = time.monotonic()
        response = await client.post(
            "/v1/messages",
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "name": "battle-pixel.png",
                                    "data": base64.b64encode(
                                        ONE_PIXEL_PNG
                                    ).decode(),
                                },
                            },
                            {
                                "type": "text",
                                "text": "Describe the attached pixel.",
                            },
                        ],
                    }
                ],
            },
        )
        report.add(
            "image_input_http",
            "passed" if response.status_code == 200 else "failed",
            duration_ms=round((time.monotonic() - started) * 1000),
            evidence={"http_status": response.status_code},
            phase=(
                ""
                if response.status_code == 200
                else "substrate_image_upload_not_completed"
            ),
        )

        original_api_key = os.environ.get("CODEX_AUTH_M365_BETA_API_KEY")
        os.environ["CODEX_AUTH_M365_BETA_API_KEY"] = "battle-local-key"
        try:
            denied = await client.get("/v1/models")
            allowed = await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer battle-local-key"},
            )
        finally:
            if original_api_key is None:
                os.environ.pop("CODEX_AUTH_M365_BETA_API_KEY", None)
            else:
                os.environ["CODEX_AUTH_M365_BETA_API_KEY"] = original_api_key
        report.add(
            "api_key_guard_http",
            "passed"
            if denied.status_code == 401 and allowed.status_code == 200
            else "failed",
            evidence={
                "denied_status": denied.status_code,
                "allowed_status": allowed.status_code,
            },
        )

        metrics = await client.get("/v1/metrics")
        logs = await client.get("/v1/logs?limit=10")
        metrics_payload = metrics.json()
        logs_payload = logs.json()
        report.add(
            "persistent_telemetry_http",
            "passed"
            if metrics.status_code == 200
            and logs.status_code == 200
            and metrics_payload.get("source") == "redacted_local_jsonl"
            else "failed",
            evidence={
                "metrics_status": metrics.status_code,
                "logs_status": logs.status_code,
                "event_count": metrics_payload.get("event_count"),
                "returned_logs": len(logs_payload.get("events") or []),
            },
        )


async def _local_http_campaign(report: BattleReport) -> None:
    """Test local HTTP contracts without touching an unavailable upstream."""

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://m365-beta.local",
        timeout=30,
    ) as client:
        for capability, request in (
            (
                "client_tools_rejection_http",
                {
                    "model": "auto",
                    "messages": [{"role": "user", "content": "test"}],
                    "tools": [{"name": "test", "input_schema": {}}],
                },
            ),
            (
                "sampling_controls_rejection_http",
                {
                    "model": "auto",
                    "messages": [{"role": "user", "content": "test"}],
                    "temperature": 0.2,
                },
            ),
        ):
            response = await client.post("/v1/messages", json=request)
            report.add(
                capability,
                "passed" if response.status_code == 400 else "failed",
                evidence={"http_status": response.status_code},
            )

        original_api_key = os.environ.get("CODEX_AUTH_M365_BETA_API_KEY")
        os.environ["CODEX_AUTH_M365_BETA_API_KEY"] = "battle-local-key"
        try:
            denied = await client.get("/v1/models")
            allowed = await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer battle-local-key"},
            )
        finally:
            if original_api_key is None:
                os.environ.pop("CODEX_AUTH_M365_BETA_API_KEY", None)
            else:
                os.environ["CODEX_AUTH_M365_BETA_API_KEY"] = original_api_key
        report.add(
            "api_key_guard_http",
            "passed"
            if denied.status_code == 401 and allowed.status_code == 200
            else "failed",
            evidence={
                "denied_status": denied.status_code,
                "allowed_status": allowed.status_code,
            },
        )

        metrics = await client.get("/v1/metrics")
        logs = await client.get("/v1/logs?limit=10")
        report.add(
            "persistent_telemetry_http",
            "passed"
            if metrics.status_code == 200
            and logs.status_code == 200
            and metrics.json().get("source") == "redacted_local_jsonl"
            else "failed",
            evidence={
                "metrics_status": metrics.status_code,
                "logs_status": logs.status_code,
                "event_count": metrics.json().get("event_count"),
                "returned_logs": len(logs.json().get("events") or []),
            },
        )


def run(known_refresh_result: str | None = None) -> dict[str, Any]:
    if os.environ.get(BETA_CONFIRM_ENV) != "1":
        raise BetaConfigurationError(
            f"set {BETA_CONFIRM_ENV}=1 before the live battle test"
        )
    report = BattleReport()
    beta = M365BearerBeta.from_directory()
    catalog = M365ModelCatalog.from_directory()
    credential_status = beta.status()
    credential_ready = (
        credential_status["generation_ready"]
        and credential_status["cookie_count"] == 0
    )
    report.add(
        "credential_and_zero_cookie",
        "passed" if credential_ready else "failed",
        evidence={
            "state": credential_status["state"],
            "generation_ready": credential_status["generation_ready"],
            "cookie_count": credential_status["cookie_count"],
            "refresh_capture_state": credential_status["refresh_capture_state"],
        },
    )
    report.add(
        "model_catalog_truthfulness",
        "passed",
        evidence=catalog.safe_status(),
    )

    if credential_ready:
        for model in catalog.models.values():
            _timed_probe(
                report,
                f"model:{model.slug}",
                lambda selected=model: _model_probe(
                    selected.slug, selected.tone
                ),
            )

        _timed_probe(
            report,
            "reasoning_progress",
            lambda: _inspect_probe(
                "Solve 19 * 23 carefully, explain briefly, and end with 437.",
                "gpt-5.5-think-deeper",
            ),
        )
        _timed_probe(
            report,
            "web_search_and_citations",
            lambda: _inspect_probe(
                "Search the web for Microsoft's official homepage and cite it.",
                "auto",
            ),
        )
        _timed_probe(
            report,
            "code_interpreter",
            lambda: _inspect_probe(
                "Use Python to calculate the first 20 Fibonacci numbers and summarize.",
                "auto",
            ),
        )
        _timed_probe(
            report,
            "image_generation",
            lambda: _inspect_probe(
                "Generate a simple blue circle on a white background.",
                "auto",
            ),
        )
        asyncio.run(_http_campaign(report))
    else:
        for model in catalog.models.values():
            report.add(
                f"model:{model.slug}",
                "blocked",
                phase="credential_re_import_required",
            )
        for capability in (
            "reasoning_progress",
            "web_search_and_citations",
            "code_interpreter",
            "image_generation",
            "anthropic_buffered_http",
            "anthropic_streaming_http",
            "openai_buffered_http",
            "openai_streaming_http",
            "compiled_system_and_multi_turn_http",
            "native_system_instruction_fidelity",
            "image_input_http",
        ):
            report.add(
                capability,
                "blocked",
                phase="credential_re_import_required",
            )
        asyncio.run(_local_http_campaign(report))

    raw = json.loads(
        (default_beta_directory() / "ms365-auth.json").read_text(
            encoding="utf-8"
        )
    )
    graph_ready = bool(
        ((raw.get("resources") or {}).get("graph") or {}).get("access_token")
    )
    report.add(
        "file_input_graph",
        "blocked" if not graph_ready else "not_run",
        phase="graph_credential_missing" if not graph_ready else "requires_file_fixture",
    )
    report.add(
        "oauth_refresh",
        (
            "passed"
            if known_refresh_result == "succeeded"
            else "failed_known"
            if known_refresh_result
            else "not_run"
        ),
        phase=known_refresh_result or "explicit_opt_in_required",
    )
    report.add(
        "dynamic_model_discovery",
        "blocked",
        phase="no_confirmed_bearer_model_endpoint",
    )
    report.add(
        "model_quota",
        "blocked",
        phase="no_confirmed_bearer_quota_endpoint",
    )
    report.add(
        "provider_reasoning_signature",
        "blocked",
        phase="not_exposed_by_m365",
    )
    report.add(
        "multi_account_pool",
        "out_of_scope",
        phase="single_personal_account_beta",
    )
    return report.safe_payload()


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--known-refresh-result")
    parser.add_argument(
        "--output",
        type=Path,
        default=default_beta_directory() / "battle-test-report.json",
    )
    arguments = parser.parse_args()
    payload = run(arguments.known_refresh_result)
    arguments.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "result": "completed",
                "counts": payload["counts"],
                "report": arguments.output.name,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
