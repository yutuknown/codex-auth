from beta.m365_equivalence import equivalence_report


def test_equivalence_report_has_unique_features_and_no_secrets():
    report = equivalence_report()
    names = [item["feature"] for item in report["features"]]

    assert len(names) == len(set(names))
    assert report["feature_count"] == len(names)
    assert set(report["state_values"]) == {
        "supported", "verified_but_not_public", "experimental", "unavailable", "blocked_by_upstream"
    }
    assert all(item["state"] in report["state_values"] for item in report["features"])
    assert "access_token" not in str(report)
    assert "refresh_token" not in str(report)


def test_equivalence_report_does_not_claim_false_native_parity():
    mapped = {item["feature"]: item for item in equivalence_report()["features"]}

    assert mapped["client_function_tools"]["m365_beta"] == "unavailable"
    assert mapped["usage_accounting"]["m365_beta"] == "local_estimate"
    assert "upstream" in mapped["usage_accounting"]["evidence"]
    assert mapped["model_quota"]["m365_beta"] == "unavailable"
    assert mapped["image_input"]["m365_beta"] == "supported"
    assert mapped["file_input"]["m365_beta"] == "supported"
    assert mapped["remote_attachment_urls"]["m365_beta"] == "supported_with_constraints"
    assert mapped["tool_result_images"]["m365_beta"] == "unavailable"
    assert mapped["persistent_telemetry"]["m365_beta"] == "supported"
    assert mapped["oauth_refresh"]["m365_beta"] == "supported"
    assert mapped["credential_restart_persistence"]["m365_beta"] == (
        "supported_with_constraints"
    )
    assert mapped["historical_tool_results"]["m365_beta"] == (
        "structured_transcript"
    )
    assert "AADSTS" not in mapped["oauth_refresh"]["evidence"]
