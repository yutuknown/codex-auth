from beta.m365_bearer import M365BearerBeta


def test_inspector_reports_artifact_availability_without_urls(monkeypatch):
    beta = object.__new__(M365BearerBeta)
    monkeypatch.setattr(beta, "status", lambda: {"cookie_count": 0})
    monkeypatch.setattr(beta, "_schema_paths", lambda frame: set())
    monkeypatch.setattr(beta, "_normalized_event_types", lambda frame: [])
    monkeypatch.setattr(beta, "_frame_messages", lambda frame: [])

    def exchange(prompt, model, observer):
        observer({"type": 1}, 1)
        # The real assembler is deliberately not invoked in this small status test;
        # simulate its inspected event shape through a lightweight replacement.
        return "answer"

    monkeypatch.setattr(beta, "_exchange", exchange)
    # This verifies the new public key exists even when an upstream card is absent.
    report = beta.inspect("safe", "auto")
    assert report["artifact_availability"] == {}
    assert "url" not in str(report).lower()
