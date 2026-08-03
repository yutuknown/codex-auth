from beta.m365_equivalence import equivalence_report


def test_equivalence_report_has_unique_features_and_no_secrets():
    report = equivalence_report()
    names = [item["feature"] for item in report["features"]]

    assert len(names) == len(set(names))
    assert report["feature_count"] == len(names)
    assert set(report["state_values"]) == {
        "implemented_unverified", "verified_live", "verified_mock_only",
        "blocked_by_upstream", "unsupported", "out_of_scope",
    }
    assert all(item["state"] in report["state_values"] for item in report["features"])
    assert "access_token" not in str(report)
    assert "refresh_token" not in str(report)


def test_equivalence_report_does_not_claim_false_native_parity():
    mapped = {item["feature"]: item for item in equivalence_report()["features"]}

    assert mapped["caller_tools_and_results"]["m365_beta"] == "unavailable"
    assert mapped["usage_and_cache_accounting"]["m365_beta"] == "local_estimate"
    assert mapped["model_quota"]["m365_beta"] == "unavailable"
    assert mapped["image_input"]["m365_beta"] == "implemented"
    assert mapped["file_input"]["m365_beta"] == "implemented"
    assert mapped["oauth_refresh_and_durability"]["m365_beta"] == "implemented"
    assert mapped["conversation_continuity"]["m365_beta"] == "implemented"
