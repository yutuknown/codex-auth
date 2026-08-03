import beta.m365_bearer as bearer


def test_inspect_cli_can_write_the_same_redacted_report(monkeypatch, tmp_path, capsys):
    class FakeBeta:
        def inspect(self, prompt, model):
            assert prompt == "safe prompt"
            assert model == "auto"
            return {"result": "passed", "artifact_phases": {"remote_attachment_http_403": 1}}

    monkeypatch.setenv(bearer.BETA_CONFIRM_ENV, "1")
    monkeypatch.setattr(bearer.M365BearerBeta, "from_directory", lambda: FakeBeta())
    report = tmp_path / "safe.json"
    monkeypatch.setattr(
        "sys.argv",
        ["m365_bearer.py", "inspect", "--prompt", "safe prompt", "--report", str(report)],
    )
    assert bearer._main() == 0
    assert report.exists()
    assert "safe prompt" not in report.read_text()
    assert "remote_attachment_http_403" in capsys.readouterr().out
