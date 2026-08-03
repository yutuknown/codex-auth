import pytest

from beta.m365_bearer import BetaConfigurationError
from beta.m365_models import M365ModelCatalog


def captured_record():
    return {
        "model_catalog": {
            "source": "captured_chat_shell",
            "captured_at": 100,
            "default_tone": "Magic",
            "models": [
                {"tone": "Magic", "title": "Auto"},
                {
                    "tone": "Gpt_5_6_Reasoning",
                    "title": "GPT 5.6 Think Deeper",
                    "verified_at": 101,
                },
            ],
        },
        "model_aliases": {
            "deep": "gpt-5.6-think-deeper",
            "m365-copilot:latest-reasoning": "deep",
        },
    }


def test_captured_catalog_maps_future_tone_and_default():
    catalog = M365ModelCatalog.from_beta_record(captured_record())

    assert catalog.source == "captured_chat_shell"
    assert catalog.default_slug == "auto"
    assert list(catalog.models) == ["auto", "gpt-5.6-think-deeper"]
    assert catalog.models["gpt-5.6-think-deeper"].reasoning is True


def test_aliases_resolve_before_availability_validation():
    catalog = M365ModelCatalog.from_beta_record(captured_record())

    resolved = catalog.resolve("m365-copilot:latest-reasoning")

    assert resolved.alias_applied is True
    assert resolved.canonical_id == "gpt-5.6-think-deeper"
    assert resolved.model.tone == "Gpt_5_6_Reasoning"


def test_alias_cannot_target_unavailable_model():
    raw = captured_record()
    raw["model_aliases"] = {"future": "gpt-9.0-reasoning"}

    with pytest.raises(BetaConfigurationError, match="targets unknown model"):
        M365ModelCatalog.from_beta_record(raw)


def test_alias_cycle_is_rejected():
    raw = captured_record()
    raw["model_aliases"] = {"one": "two", "two": "one"}

    with pytest.raises(BetaConfigurationError, match="cycle"):
        M365ModelCatalog.from_beta_record(raw)


def test_fallback_is_truthfully_not_account_scoped_or_dynamic():
    catalog = M365ModelCatalog.from_beta_record({})

    status = catalog.safe_status()

    assert status["source"] == "fallback"
    assert status["account_scoped"] is False
    assert status["dynamic"] is False
    assert catalog.resolve("m365-copilot:gpt-5.5-think-deeper").model.tone == "Gpt_5_5_Reasoning"


def test_openai_model_list_exposes_source_and_namespaced_id():
    catalog = M365ModelCatalog.from_beta_record(captured_record())

    response = catalog.api_list()

    assert response["object"] == "list"
    assert response["catalog"]["source"] == "captured_chat_shell"
    assert response["data"][1]["id"] == "gpt-5.6-think-deeper"
    assert response["data"][1]["namespaced_id"] == "m365-copilot:gpt-5.6-think-deeper"
    assert response["data"][1]["upstream_id"] == "Gpt_5_6_Reasoning"
