import json

import pytest

import beta.m365_compat as compat
from beta.m365_conversations import ConversationConflict, ConversationCoordinator
from beta.m365_durable import CREDENTIAL_KEY_ENV, PostgresCredentialStore
from beta.m365_personalization import (
    BETA_CONFIRM_ENV,
    PERSONALIZATION_CONFIRM_ENV,
    _endpoint,
    probe,
)


def test_conversation_coordinator_reuses_upstream_id_and_blocks_inflight_duplicates():
    coordinator = ConversationCoordinator(secret="test", now=lambda: 100)
    first = coordinator.acquire(explicit_id="thread", first_user_text="first", request_text="turn one")
    coordinator.complete(first, result={"state": "completed"})
    second = coordinator.acquire(explicit_id="thread", first_user_text="other", request_text="turn two")

    assert first["upstream_id"] == second["upstream_id"]
    assert first["proxy_id"] == second["proxy_id"]
    assert "first" not in str(first)
    with pytest.raises(ConversationConflict):
        coordinator.acquire(explicit_id="thread", first_user_text="other", request_text="turn two")


def test_conversation_coordinator_returns_bounded_completed_response_copy():
    coordinator = ConversationCoordinator(secret="cache", now=lambda: 100)
    first = coordinator.acquire(
        explicit_id="thread",
        first_user_text="first",
        request_text="chat_completions\0same request",
    )
    response = {"object": "chat.completion", "choices": [{"message": {"content": "answer"}}]}
    coordinator.complete(first, result={"response": response})

    duplicate = coordinator.acquire(
        explicit_id="thread",
        first_user_text="first",
        request_text="chat_completions\0same request",
    )
    duplicate["cached_response"]["choices"][0]["message"]["content"] = "changed"
    repeated = coordinator.acquire(
        explicit_id="thread",
        first_user_text="first",
        request_text="chat_completions\0same request",
    )

    assert duplicate["continuity"] == "cached"
    assert repeated["cached_response"]["choices"][0]["message"]["content"] == "answer"


def test_first_user_fallback_is_hmaced_not_plaintext():
    coordinator = ConversationCoordinator(secret="test")
    token = coordinator.acquire(explicit_id=None, first_user_text="private prompt", request_text="private prompt")

    assert "private" not in str(token)
    assert token["proxy_id"].startswith("m365c_")


def test_continuation_sends_only_appended_turns():
    prepared = compat._prepare_messages(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "two"},
        ]
    )
    prompt = compat._continuation_prompt(prepared, {"continuity": "continued", "delta_start": 2})

    assert "two" in prompt
    assert "one" not in prompt
    assert "answer" not in prompt


def test_conversation_rolls_over_after_configured_turn_limit(monkeypatch):
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_UPSTREAM_MAX_TURNS", "6")
    local = ConversationCoordinator(secret="rollover", now=lambda: 100)
    first_hashes = tuple(f"turn-{index}" for index in range(6))
    first = local.acquire(
        explicit_id="session",
        first_user_text="first",
        request_text="initial",
        turn_hashes=first_hashes,
    )
    local.complete(first)
    second = local.acquire(
        explicit_id="session",
        first_user_text="first",
        request_text="continued",
        turn_hashes=first_hashes + ("turn-6",),
    )

    assert second["continuity"] == "rolled_over"
    assert second["upstream_id"] != first["upstream_id"]
    assert local.public_metadata(second)["rollover"] is True


def test_model_switch_keeps_proxy_chat_but_forks_upstream_conversation():
    local = ConversationCoordinator(secret="model-switch", now=lambda: 100)
    first_hashes = ("user-one", "assistant-one")
    first = local.acquire(
        explicit_id="studio-chat",
        first_user_text="first",
        request_text="first request",
        turn_hashes=first_hashes,
        model_id="gpt-5-5-quick-response",
    )
    local.complete(first)

    second = local.acquire(
        explicit_id="studio-chat",
        first_user_text="first",
        request_text="second request",
        turn_hashes=first_hashes + ("user-two",),
        model_id="gpt-5-5-think-deeper",
    )

    assert second["proxy_id"] == first["proxy_id"]
    assert second["upstream_id"] != first["upstream_id"]
    assert second["continuity"] == "model_switched"
    assert local.public_metadata(second)["model_switch"] is True


def test_model_case_stability_does_not_fork_upstream():
    local = ConversationCoordinator(secret="model-stable", now=lambda: 100)
    first = local.acquire(
        explicit_id="studio-chat",
        first_user_text="first",
        request_text="first request",
        turn_hashes=("user-one",),
        model_id="GPT-5-5-QUICK-RESPONSE",
    )
    local.complete(first)
    second = local.acquire(
        explicit_id="studio-chat",
        first_user_text="first",
        request_text="second request",
        turn_hashes=("user-one", "assistant-one", "user-two"),
        model_id="gpt-5-5-quick-response",
    )

    assert second["upstream_id"] == first["upstream_id"]
    assert second["continuity"] == "continued"


def test_responses_endpoint_emits_reasoning_and_text(monkeypatch):
    async def provider_events(prompt, model, attachments=None):
        yield {"type": "reasoning_summary_delta", "delta": "Checking"}
        yield {"type": "text_delta", "delta": "Answer"}

    monkeypatch.setattr(compat, "_provider_events", provider_events)

    async def call():
        return await compat.openai_responses({"model": "auto", "input": "unique responses prompt"})

    import asyncio

    response = asyncio.run(call())
    assert response["object"] == "response"
    assert response["output"][0]["type"] == "reasoning"
    assert response["output"][1]["content"][0]["text"] == "Answer"
    assert response["provider_metadata"]["m365"]["reasoning"]["raw_chain_of_thought"] is False


class _Response:
    status_code = 200
    content = b'{"items":[{"kind":"memory"}]}'

    def close(self):
        pass


class _Session:
    cookies = []

    def __init__(self):
        self.request = None

    def get(self, endpoint, *, params, headers, timeout):
        self.request = {"endpoint": endpoint, "params": params, "headers": headers, "timeout": timeout}
        return _Response()

    def close(self):
        pass


class _Beta:
    class credential:
        access_token = "must-not-leak"

    class route:
        identity = "private@example.invalid"

    def __init__(self):
        self.session = _Session()

    def _new_cookie_free_session(self):
        return self.session


def test_personalization_probe_reports_schema_only(monkeypatch):
    monkeypatch.setenv(BETA_CONFIRM_ENV, "1")
    monkeypatch.setenv(PERSONALIZATION_CONFIRM_ENV, "1")
    beta = _Beta()

    result = probe("memories", beta)

    assert result["state"] == "verified_private_read"
    assert result["cookie_count"] == 0
    assert "must-not-leak" not in str(result)
    assert "'kind': 'memory'" not in str(result)
    assert json.loads(beta.session.request["params"]["request"])["source"] == "officeweb"
    assert "Cookie" not in beta.session.request["headers"]


def test_personalization_endpoints_are_read_only_and_variant_scoped():
    endpoint, params = _endpoint("custom_instructions")

    assert endpoint.endswith("/CustomInstructions")
    assert params["variants"] == "feature.EnablePersonalizationForMSA"


def test_durable_record_is_encrypted_and_safe_status_has_no_credential(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_KEY_ENV, "local-test-key")
    store = PostgresCredentialStore("postgresql://not-used-by-this-unit-test")

    encrypted = store._encrypt({"access_token": "must-not-leak"})

    assert b"must-not-leak" not in encrypted
    assert store._decrypt(encrypted) == {"access_token": "must-not-leak"}
    assert "access_token" not in str(store.safe_status())
