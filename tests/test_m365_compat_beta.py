import asyncio
import base64

import pytest

import beta.m365_compat as compat
from beta.m365_artifacts import artifact_store
from beta.m365_bearer import M365Attachment
from beta.m365_compat import (
    AnthropicStreamEncoder,
    OpenAIStreamEncoder,
    _collect_content,
    _estimate_tokens,
    _preflight_stream,
    _prepare_messages,
    _prompt_from_messages,
    _unsupported_request_feature,
    account_limits,
    capabilities,
    count_tokens,
    model,
    normalize_public_event,
    root_head,
)
from beta.m365_models import M365ModelCatalog


def test_root_head_is_a_side_effect_free_hosting_probe():
    response = root_head()

    assert response.status_code == 200
    assert response.body == b""


def test_public_contract_names_reasoning_as_summary_and_preserves_lane():
    event = normalize_public_event(
        {
            "type": "reasoning_progress",
            "delta": "Checking arithmetic",
            "lane": "reasoning:abc",
            "operation": "append",
            "elapsed_ms": 20,
        }
    )

    assert event == {
        "type": "reasoning_summary_delta",
        "delta": "Checking arithmetic",
        "lane": "reasoning:abc",
        "operation": "append",
        "elapsed_ms": 20,
    }
    assert "signature" not in event


def test_anthropic_encoder_matches_thinking_then_text_lifecycle_without_signature():
    encoder = AnthropicStreamEncoder("gpt-5.5-think-deeper", message_id="msg_test")

    thinking = encoder.feed(
        {"type": "reasoning_summary_delta", "delta": "Checking"}
    )
    text = encoder.feed({"type": "text_delta", "delta": "Answer"})
    finished = encoder.finish()
    output = thinking + text + finished

    assert [event["type"] for event in output] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert thinking[-1]["delta"] == {
        "type": "thinking_delta",
        "thinking": "Checking",
    }
    assert text[-1]["delta"] == {"type": "text_delta", "text": "Answer"}
    assert "signature_delta" not in str(output)
    assert "thoughtSignature" not in str(output)


def test_openai_encoder_uses_reasoning_content_and_regular_content():
    encoder = OpenAIStreamEncoder(
        "gpt-5.5-think-deeper",
        completion_id="chatcmpl_test",
        created=1,
    )

    reasoning = encoder.feed(
        {"type": "reasoning_summary_delta", "delta": "Checking"}
    )[0]
    text = encoder.feed({"type": "text_delta", "delta": "Answer"})[0]
    finish = encoder.finish()[0]

    assert reasoning["choices"][0]["delta"] == {
        "reasoning_content": "Checking"
    }
    assert text["choices"][0]["delta"] == {"content": "Answer"}
    assert finish["choices"][0]["finish_reason"] == "stop"
    assert finish["usage"]["completion_tokens"] > 0
    assert finish["usage_details"]["upstream_reported"] is False


def test_encoders_keep_safe_citations_in_terminal_m365_metadata():
    anthropic = AnthropicStreamEncoder("auto", message_id="msg_artifacts")
    anthropic.feed(
        {
            "type": "citation",
            "count": 1,
            "citations": [{"title": "Safe title", "domain": "example.com"}],
        }
    )
    terminal = next(item for item in anthropic.finish() if item["type"] == "message_delta")
    assert terminal["provider_metadata"]["m365"]["artifacts"][0]["citations"] == [
        {"title": "Safe title", "domain": "example.com"}
    ]
    assert "http" not in str(terminal)

    openai = OpenAIStreamEncoder("auto", completion_id="chatcmpl_artifacts", created=1)
    openai.feed({"type": "image_progress", "artifact": {"availability": "unretrievable"}})
    finished = openai.finish()[0]
    assert finished["provider_metadata"]["m365"]["artifacts"][0]["artifact"]["availability"] == "unretrievable"


def test_anthropic_encoder_emits_a_base64_image_only_for_cached_verified_bytes():
    descriptor = artifact_store.put_image(b"verified", "image/png")
    encoder = AnthropicStreamEncoder("auto", message_id="msg_image")
    output = encoder.feed({"type": "image", "artifact_id": descriptor["id"]})
    block = next(item for item in output if item["type"] == "content_block_start")
    assert block["content_block"] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "dmVyaWZpZWQ="},
    }


def test_prompt_conversion_accepts_anthropic_text_blocks_and_rejects_no_text():
    prompt = _prompt_from_messages(
        [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "assistant", "content": "Hi"},
        ],
        "Be concise",
    )

    assert prompt == (
        "Response preferences supplied by the API caller:\n\n"
        "Be concise\n\n"
        "Conversation context:\n\n"
        "Current user request: Hello\n\n"
        "Earlier assistant message: Hi"
    )


def test_prompt_conversion_accepts_system_arrays_and_historical_thinking_summaries():
    prompt = _prompt_from_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Checked the inputs"},
                    {"type": "text", "text": "First answer"},
                ],
            },
            {"role": "user", "content": "Continue"},
        ],
        [{"type": "text", "text": "Be precise"}],
    )
    assert prompt == (
        "Response preferences supplied by the API caller:\n\n"
        "Be precise\n\n"
        "Conversation context:\n\n"
        "Earlier assistant message: Checked the inputs\nFirst answer\n\n"
        "Current user request: Continue"
    )


def test_prepare_messages_stages_base64_image_with_proven_annotation_binding():
    class FakeUploader:
        def upload_bytes(
            self, *, name, content, mime_type, conversation_id=None
        ):
            assert name == "pixel.png"
            assert content == b"png-bytes"
            assert mime_type == "image/png"
            assert conversation_id is None
            return M365Attachment(
                annotation_id="image-doc",
                name=name,
                mime_type=mime_type,
                annotation_type="Image",
                conversation_id="conversation",
            )

    prompt, attachments = _prepare_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "name": "pixel.png",
                            "data": base64.b64encode(b"png-bytes").decode(),
                        },
                    },
                    {"type": "text", "text": "Describe it"},
                ],
            }
        ],
        image_uploader=FakeUploader(),
    )

    assert prompt == (
        "Conversation context:\n\n"
        "Current user request: [Attached file: pixel.png]\nDescribe it"
    )
    assert attachments[0].message_annotation()["id"] == "image-doc"


def test_file_input_uses_graph_uploader_when_live_proof_is_available():
    class FakeUploader:
        def stage_attachment(self, *, name, content, mime_type):
            assert name.endswith(".txt")
            assert content == b"proof"
            return M365Attachment(annotation_id="file-doc", name=name, mime_type=mime_type)

    prepared = _prepare_messages(
        [{"role": "user", "content": [{"type": "file", "source": {"type": "base64", "media_type": "text/plain", "data": base64.b64encode(b"proof").decode()}}]}],
        uploader=FakeUploader(),
    )
    assert prepared.attachments[0].annotation_id == "file-doc"


def test_prepared_conversation_preserves_safe_structured_ir_before_compilation():
    prepared = _prepare_messages(
        [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow up"},
        ],
        [{"type": "text", "text": "Be exact"}],
    )

    assert prepared.system_text == "Be exact"
    assert [turn.role for turn in prepared.turns] == [
        "user",
        "assistant",
        "user",
    ]
    status = prepared.safe_status()
    assert status == {
        "transport_mode": "compiled_structured_transcript",
        "native_structured_history": False,
        "system_instruction_mode": "response_preferences",
        "turn_count": 3,
        "attachment_count": 0,
        "roles": ["user", "assistant", "user"],
        "history_limits": {
            "max_turns": 64,
            "max_compiled_characters": 200000,
        },
        "reasoning": {
            "mode": "provider_summary_unsigned",
            "raw_chain_of_thought": False,
            "signature_available": False,
        },
        "caller_tools": {
            "invocation": "unavailable",
            "historical_tool_results": "compiled_as_context",
        },
    }
    prompt, attachments = prepared
    assert "Response preferences supplied by the API caller:" in prompt
    assert "Current user request: Follow up" in prompt
    assert attachments == []


def test_system_roles_and_historical_tool_results_are_preserved_as_context():
    prepared = _prepare_messages(
        [
            {"role": "system", "content": "Be concise"},
            {"role": "developer", "content": "Return a marker"},
            {"role": "assistant", "content": "Calling a tool"},
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "call-1",
                "content": "marker=42",
            },
            {"role": "user", "content": "Use the result"},
        ]
    )

    assert "System instruction:\nBe concise" in prepared.system_text
    assert "Developer instruction:\nReturn a marker" in prepared.system_text
    assert "Tool result (lookup):\nmarker=42" in prepared.prompt
    assert prepared.safe_status()["caller_tools"] == {
        "invocation": "unavailable",
        "historical_tool_results": "compiled_as_context",
    }


def test_anthropic_tool_result_block_is_compiled_but_tool_invocation_stays_rejected():
    prepared = _prepare_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "safe result"}],
                    }
                ],
            }
        ]
    )

    assert "Tool result (toolu_1):\nsafe result" in prepared.prompt
    assert _unsupported_request_feature({"tools": [{"name": "lookup"}]}) == (
        "client function tools"
    )


def test_deployment_history_limits_reject_unbounded_requests():
    messages = [{"role": "user", "content": str(index)} for index in range(65)]
    with pytest.raises(compat.BetaConfigurationError, match="64-turn"):
        _prepare_messages(messages)


def test_deployment_readiness_refuses_to_claim_durable_refresh(monkeypatch):
    class FakeCredential:
        raw = {}

    class FakeBeta:
        credential = FakeCredential()

        def status(self):
            return {
                "state": "active",
                "generation_ready": True,
                "refresh_ready": True,
                "credential_persistence": {
                    "source": "environment",
                    "rotation_persistence": "process_memory",
                    "restart_durable": False,
                },
            }

    monkeypatch.setattr(
        compat.M365BearerBeta, "from_directory", lambda: FakeBeta()
    )

    status = compat.deployment_readiness()

    assert status["ready"] is False
    assert status["file_input"] == "reacquirable_from_generation_refresh"
    assert status["caller_tool_invocation"] == "unavailable"
    assert status["reasoning"]["signature_available"] is False
    assert "restart" in status["warnings"][0]


def test_local_usage_estimate_is_nonzero_and_explicitly_not_upstream():
    assert _estimate_tokens("Hello, world!") > 0
    encoder = AnthropicStreamEncoder(
        "auto",
        message_id="msg_usage",
        input_text="Count this prompt",
    )
    encoder.feed({"type": "text_delta", "delta": "Count this answer"})
    output = encoder.finish()

    message_delta = next(item for item in output if item["type"] == "message_delta")
    assert message_delta["usage"]["output_tokens"] > 0
    assert message_delta["usage"]["source"] == "local_lexical_estimate"
    assert message_delta["usage"]["upstream_reported"] is False


def test_prepare_messages_stages_openai_data_url_image():
    class FakeUploader:
        def upload_bytes(
            self, *, name, content, mime_type, conversation_id=None
        ):
            assert name == "diagram.png"
            assert content == b"png-bytes"
            assert mime_type == "image/png"
            assert conversation_id is None
            return M365Attachment(
                annotation_id="image-doc",
                name=name,
                mime_type=mime_type,
                annotation_type="Image",
                conversation_id="conversation",
            )

    data_url = "data:image/png;base64," + base64.b64encode(
        b"png-bytes"
    ).decode()
    prompt, attachments = _prepare_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url,
                            "name": "diagram.png",
                        },
                    },
                    {"type": "text", "text": "Describe it"},
                ],
            }
        ],
        image_uploader=FakeUploader(),
    )

    assert prompt == (
        "Conversation context:\n\n"
        "Current user request: [Attached file: diagram.png]\nDescribe it"
    )
    assert attachments[0].message_annotation()["messageAnnotationMetadata"] == {
        "@type": "Image",
        "fileType": "png",
    }


def test_prepare_messages_fetches_remote_image_then_uses_proven_upload():
    class FakeRemoteFetcher:
        def fetch(self, url, *, name=""):
            assert url == "https://example.test/pixel.png"
            return type(
                "Remote",
                (),
                {
                    "content": b"png-bytes",
                    "mime_type": "image/png",
                    "name": name or "pixel.png",
                },
            )()

    class FakeImageUploader:
        def upload_bytes(self, *, name, content, mime_type, conversation_id=None):
            assert (name, content, mime_type) == (
                "pixel.png",
                b"png-bytes",
                "image/png",
            )
            return M365Attachment(
                annotation_id="image-doc",
                name=name,
                mime_type=mime_type,
                annotation_type="Image",
                conversation_id="conversation",
            )

    prompt, attachments = _prepare_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": "https://example.test/pixel.png",
                            "name": "pixel.png",
                        },
                    }
                ],
            }
        ],
        remote_fetcher=FakeRemoteFetcher(),
        image_uploader=FakeImageUploader(),
    )

    assert "Current user request: [Attached file: pixel.png]" in prompt
    assert attachments[0].annotation_id == "image-doc"


def test_image_upstream_failure_is_returned_as_safe_http_error(monkeypatch):
    class FailedUploader:
        def upload_bytes(self, **kwargs):
            raise compat.BetaUpstreamError("substrate_image_upload_http_401")

    monkeypatch.setattr(
        compat.M365SubstrateImageUploader,
        "from_directory",
        lambda: FailedUploader(),
    )

    async def call():
        return await compat.anthropic_messages(
            {
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
                                    "data": base64.b64encode(b"png").decode(),
                                },
                            }
                        ],
                    }
                ],
            }
        )

    response = asyncio.run(call())
    assert response.status_code == 502
    assert b"substrate_image_upload_http_401" in response.body


def test_unproven_controls_are_rejected_instead_of_silently_ignored():
    assert _unsupported_request_feature({"tools": [{"name": "read"}]}) == (
        "client function tools"
    )
    assert _unsupported_request_feature({"tool_choice": {"type": "auto"}}) == (
        "tool_choice"
    )
    assert _unsupported_request_feature({"temperature": 0.2, "top_p": 0.8}) == (
        "temperature, top_p"
    )
    assert _unsupported_request_feature({"max_tokens": 100}) == "max_tokens"
    assert _unsupported_request_feature({"thinking": {"type": "enabled"}}) == "thinking"


def test_collect_content_keeps_reasoning_and_answer_separate():
    reasoning, text = _collect_content(
        [
            {"type": "reasoning_summary_delta", "delta": "Check "},
            {"type": "text_delta", "delta": "Result"},
            {"type": "reasoning_summary_delta", "delta": "carefully"},
        ]
    )

    assert reasoning == "Check carefully"
    assert text == "Result"


def test_preflight_stream_replays_first_event():
    async def source():
        yield "first"
        yield "second"

    async def collect():
        stream = await _preflight_stream(source())
        return [item async for item in stream]

    assert asyncio.run(collect()) == ["first", "second"]


def test_anthropic_endpoint_streams_reasoning_and_text(monkeypatch):
    async def provider_events(prompt, model, attachments=None):
        assert "Hello" in prompt
        assert model == "gpt-5.5-think-deeper"
        assert attachments == []
        yield {"type": "reasoning_summary_delta", "delta": "Checking"}
        yield {"type": "text_delta", "delta": "Answer"}

    monkeypatch.setattr(compat, "_provider_events", provider_events)

    async def call():
        response = await compat.anthropic_messages(
            {
                "model": "gpt-5.5-think-deeper",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            }
        )
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(call())
    assert "event: message_start" in body
    assert '"type":"thinking_delta","thinking":"Checking"' in body
    assert '"type":"text_delta","text":"Answer"' in body
    assert "signature_delta" not in body


def test_openai_endpoint_streams_reasoning_content(monkeypatch):
    async def provider_events(prompt, model, attachments=None):
        assert attachments == []
        yield {"type": "reasoning_summary_delta", "delta": "Checking"}
        yield {"type": "text_delta", "delta": "Answer"}

    monkeypatch.setattr(compat, "_provider_events", provider_events)

    async def call():
        response = await compat.openai_chat_completions(
            {
                "model": "gpt-5.5-think-deeper",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            }
        )
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(call())
    assert '"reasoning_content":"Checking"' in body
    assert '"content":"Answer"' in body
    assert body.endswith("data: [DONE]\n\n")


def test_models_endpoint_returns_source_labelled_availability(monkeypatch):
    catalog = M365ModelCatalog.from_beta_record({})
    monkeypatch.setattr(
        compat.M365ModelCatalog,
        "from_directory",
        lambda: catalog,
    )

    response = compat.models()

    assert response["object"] == "list"
    assert response["catalog"]["source"] == "fallback"
    assert response["catalog"]["dynamic"] is False
    assert response["data"][0]["namespaced_id"] == "m365-copilot:auto"


def test_model_detail_resolves_alias_and_never_contains_credentials(monkeypatch):
    catalog = M365ModelCatalog.from_beta_record(
        {"model_aliases": {"reasoning": "gpt-5.5-think-deeper"}}
    )
    monkeypatch.setattr(
        compat.M365ModelCatalog,
        "from_directory",
        lambda: catalog,
    )

    response = model("reasoning")

    assert response["canonical_id"] == "gpt-5.5-think-deeper"
    assert response["alias_applied"] is True
    assert "token" not in str(response).lower()


def test_count_tokens_truthfully_matches_antigravity_not_implemented_contract():
    response = count_tokens()

    assert response.status_code == 501
    assert b'"type":"not_implemented"' in response.body


def test_equivalence_report_is_machine_readable_and_truthful():
    response = capabilities()
    mapped = {item["feature"]: item for item in response["features"]}

    assert mapped["oauth_bearer"]["m365_beta"] == "implemented"
    assert mapped["reasoning"]["m365_beta"] == "unsigned_summary"
    assert mapped["caller_tools_and_results"]["m365_beta"] == "unavailable"
    assert mapped["dynamic_model_catalog"]["m365_beta"] == "captured_catalog"
    assert mapped["oauth_refresh_and_durability"]["m365_beta"] == "implemented"
    assert mapped["usage_and_cache_accounting"]["m365_beta"] == "local_estimate"
    assert mapped["native_system_and_history"]["m365_beta"] == "compiled_transcript"


def test_account_limits_degrades_safely_when_not_configured(monkeypatch):
    def missing():
        raise compat.BetaConfigurationError("missing")

    monkeypatch.setattr(compat.M365BearerBeta, "from_directory", missing)

    response = account_limits()

    assert response["accounts"] == 0
    assert response["credential"]["state"] == "not_configured"
