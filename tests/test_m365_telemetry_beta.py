import json

from beta.m365_telemetry import BetaTelemetry


def test_telemetry_persists_only_whitelisted_redacted_metadata(tmp_path):
    telemetry = BetaTelemetry(tmp_path / "events.jsonl")

    telemetry.record(
        "generation_completed",
        status="succeeded",
        model="auto",
        duration_ms=125,
        input_characters=20,
        output_characters=40,
        prompt="must not persist",
        access_token="must not persist",
        protected_url="must not persist",
    )

    raw = telemetry.path.read_text(encoding="utf-8")
    assert "must not persist" not in raw
    event = json.loads(raw)
    assert event["event"] == "generation_completed"
    assert event["duration_ms"] == 125
    assert telemetry.summary()["completed_generations"] == 1


def test_telemetry_api_limits_recent_events(tmp_path):
    telemetry = BetaTelemetry(tmp_path / "events.jsonl")
    for index in range(5):
        telemetry.record("generation_failed", status="failed", duration_ms=index)

    recent = telemetry.recent(2)

    assert len(recent) == 2
    assert all(item["status"] == "failed" for item in recent)


def test_telemetry_summary_includes_latency_traffic_and_failure_health(tmp_path):
    telemetry = BetaTelemetry(tmp_path / "events.jsonl")
    telemetry.record(
        "generation_completed",
        status="completed",
        duration_ms=100,
        input_characters=20,
        output_characters=40,
        attachment_count=1,
    )
    telemetry.record(
        "generation_completed",
        status="completed",
        duration_ms=300,
        input_characters=10,
        output_characters=30,
    )
    telemetry.record(
        "generation_failed",
        status="failed",
        error_phase="signalr_connect_http_429",
    )

    summary = telemetry.summary()

    assert summary["success_rate"] == 0.6667
    assert summary["latency_ms"] == {"p50": 100, "p95": 300, "maximum": 300}
    assert summary["traffic"] == {
        "input_characters": 30,
        "output_characters": 70,
        "attachments": 1,
    }
    assert summary["failures_by_phase"] == {"signalr_connect_http_429": 1}
