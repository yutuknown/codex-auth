"""Low-memory, secret-free telemetry for the local M365 beta.

Only operational metadata is persisted. Prompts, response text, credentials,
headers, URLs, account identity, conversation IDs, and request IDs are never
accepted by this module.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from beta.m365_bearer import default_beta_directory

MAX_LOG_BYTES = 512 * 1024
MAX_READ_EVENTS = 2_000
SAFE_FIELDS = {
    "attachment_count",
    "duration_ms",
    "error_phase",
    "event",
    "input_characters",
    "model",
    "output_characters",
    "provider",
    "status",
    "stream",
    "timestamp",
    "transport",
}


class BetaTelemetry:
    """Append and summarize bounded redacted JSONL telemetry."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (default_beta_directory() / "runtime-events.jsonl")
        self._lock = threading.Lock()

    @staticmethod
    def _safe_event(event: str, fields: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": int(time.time()),
            "event": str(event)[:64],
            "provider": "m365-copilot",
        }
        for key, value in fields.items():
            if key not in SAFE_FIELDS or key in {"event", "provider", "timestamp"}:
                continue
            if isinstance(value, bool):
                record[key] = value
            elif isinstance(value, int):
                record[key] = max(0, value)
            elif value is not None:
                record[key] = str(value)[:96]
        return record

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size <= MAX_LOG_BYTES:
            return
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        retained = lines[-MAX_READ_EVENTS // 2 :]
        temporary = self.path.with_suffix(".jsonl.tmp")
        temporary.write_text("\n".join(retained) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def record(self, event: str, **fields: Any) -> None:
        record = self._safe_event(event, fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        try:
            lines = self.path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines[-MAX_READ_EVENTS:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(
                    {key: value for key, value in item.items() if key in SAFE_FIELDS}
                )
        return events[-bounded_limit:]

    def summary(self) -> dict[str, Any]:
        events = self.recent(MAX_READ_EVENTS)
        statuses: Counter[str] = Counter()
        models: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        total_duration_ms = 0
        durations: list[int] = []
        completed = 0
        total_input_characters = 0
        total_output_characters = 0
        total_attachments = 0
        for item in events:
            status = str(item.get("status") or "")
            model = str(item.get("model") or "")
            if status:
                statuses[status] += 1
            if model:
                models[model] += 1
            error_phase = str(item.get("error_phase") or "")
            if error_phase:
                failures[error_phase] += 1
            total_input_characters += int(item.get("input_characters") or 0)
            total_output_characters += int(item.get("output_characters") or 0)
            total_attachments += int(item.get("attachment_count") or 0)
            if item.get("event") == "generation_completed":
                completed += 1
                duration = int(item.get("duration_ms") or 0)
                total_duration_ms += duration
                durations.append(duration)
        durations.sort()

        def percentile(fraction: float) -> int | None:
            if not durations:
                return None
            index = min(
                len(durations) - 1,
                max(0, round((len(durations) - 1) * fraction)),
            )
            return durations[index]

        terminal = completed + sum(
            count
            for status, count in statuses.items()
            if status in {"failed", "error"}
        )
        return {
            "source": "redacted_local_jsonl",
            "event_count": len(events),
            "completed_generations": completed,
            "success_rate": (
                round(completed / terminal, 4) if terminal else None
            ),
            "average_generation_ms": (
                round(total_duration_ms / completed) if completed else None
            ),
            "latency_ms": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "maximum": durations[-1] if durations else None,
            },
            "traffic": {
                "input_characters": total_input_characters,
                "output_characters": total_output_characters,
                "attachments": total_attachments,
            },
            "statuses": dict(sorted(statuses.items())),
            "failures_by_phase": dict(sorted(failures.items())),
            "models": dict(sorted(models.items())),
            "last_event_at": (
                max(int(item.get("timestamp") or 0) for item in events)
                if events
                else None
            ),
            "retention": {
                "max_bytes": MAX_LOG_BYTES,
                "max_api_events": 200,
            },
        }


telemetry = BetaTelemetry()
