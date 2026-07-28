import asyncio
import json

import pytest
from fastapi import HTTPException

from codex_auth.api.routes_openai import (
    ChatCompletionRequest,
    ChatMessage,
    _request_input,
    _trace_data,
    _trace_messages,
    openai_chat_completions,
)
from codex_auth.providers.openai.provider import OpenAIProvider


def test_request_input_builds_stateless_transcript_and_keeps_attachments():
    messages = [
        ChatMessage(role="system", content="Be concise."),
        ChatMessage(role="user", content="Remember alpha."),
        ChatMessage(role="assistant", content="I will."),
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "What did I ask you to remember?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,AAAA",
                        "name": "context.png",
                    },
                },
            ],
        ),
    ]

    prompt, files = _request_input(messages)

    assert "SYSTEM:\nBe concise." in prompt
    assert "ASSISTANT:\nI will." in prompt
    assert prompt.endswith("USER:\nWhat did I ask you to remember?")
    assert files == [
        {
            "url": "data:image/png;base64,AAAA",
            "name": "context.png",
            "mime_type": None,
        }
    ]


def test_trace_messages_never_retains_attachment_payload():
    secret_base64 = "PRIVATE_ATTACHMENT_BYTES"
    messages = [
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Inspect this"},
                {
                    "type": "file_url",
                    "file_url": {
                        "url": f"data:text/plain;base64,{secret_base64}",
                        "name": "proof.txt",
                    },
                },
            ],
        )
    ]

    trace = _trace_messages(messages)
    serialized = json.dumps(trace)

    assert secret_base64 not in serialized
    assert trace[0]["parts"][1]["source"] == "data_url"
    assert trace[0]["parts"][1]["name"] == "proof.txt"
    assert trace[0]["parts"][1]["mime_type"] == "text/plain"
    assert trace[0]["parts"][1]["content"] == "[BINARY CONTENT REDACTED]"


def test_trace_captures_image_file_and_tool_metadata_without_binary_content():
    png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZFlAAAAAASUVORK5CYII="
    )
    request = ChatCompletionRequest(
        model="gpt-test",
        stream=True,
        web_search=True,
        messages=[
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Inspect everything"},
                    {
                        "type": "image_url",
                        "image_url": {"url": png, "name": "pixel.png"},
                    },
                    {
                        "type": "file_url",
                        "file_url": {
                            "url": "https://files.example/report.pdf?secret=signed-value",
                            "name": "report.pdf",
                            "mime_type": "application/pdf",
                        },
                    },
                ],
            )
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a record",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
                },
            }
        ],
        tool_choice="auto",
    )

    trace = _trace_data(
        request,
        "gpt-test",
        "captured answer",
        120,
        1.5,
        20,
        4,
        chunk_count=3,
    )
    image = trace["messages"][0]["parts"][1]
    document = trace["messages"][0]["parts"][2]
    serialized = json.dumps(trace)

    assert png not in serialized
    assert "signed-value" not in serialized
    assert image["mime_type"] == "image/png"
    assert image["width"] == 1
    assert image["height"] == 1
    assert image["estimated_bytes"] > 0
    assert document["location"]["origin"] == "https://files.example"
    assert document["location"]["query"] == "[REDACTED]"
    assert trace["payload"]["attachment_count"] == 2
    assert trace["payload"]["tools"][0]["name"] == "lookup"
    assert trace["response_data"]["stream"]["chunk_count"] == 3
    assert trace["response_data"]["usage"]["total_tokens"] == 24


def test_trace_text_capture_has_aggregate_memory_bound():
    messages = [ChatMessage(role="user", content=str(index) * 5000) for index in range(10)]

    trace = _trace_messages(messages)

    assert sum(len(message["text"]) for message in trace) == 16000
    assert all(message["text_truncated"] for message in trace)


def test_function_tools_fail_explicitly_instead_of_being_ignored():
    request = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="Use the tool")],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(openai_chat_completions(request))

    assert exc_info.value.status_code == 501
    assert "not implemented" in exc_info.value.detail["message"]


def test_non_streaming_route_requests_canonical_buffered_generation(monkeypatch):
    observed = {}

    async def fake_generate_stream(prompt, **kwargs):
        observed["prompt"] = prompt
        observed.update(kwargs)
        yield "complete "
        yield "answer"

    monkeypatch.setattr(
        "codex_auth.api.routes_openai.provider.generate_stream",
        fake_generate_stream,
    )
    monkeypatch.setattr("codex_auth.api.routes_openai.record_usage", lambda *args, **kwargs: None)
    request = ChatCompletionRequest(
        model="gpt-test",
        messages=[ChatMessage(role="user", content="Hello")],
        stream=False,
    )

    response = asyncio.run(openai_chat_completions(request))

    assert response["choices"][0]["message"]["content"] == "complete answer"
    assert observed["model"] == "gpt-test"
    assert observed["realtime"] is False


def test_canonical_response_prefers_requested_assistant_message():
    conversation = {
        "mapping": {
            "old": {
                "message": {
                    "id": "assistant-old",
                    "author": {"role": "assistant"},
                    "create_time": 1,
                    "content": {"content_type": "text", "parts": ["old answer"]},
                }
            },
            "new": {
                "message": {
                    "id": "assistant-new",
                    "author": {"role": "assistant"},
                    "create_time": 2,
                    "content": {
                        "content_type": "text",
                        "parts": ["complete ", "multimodal answer"],
                    },
                }
            },
        }
    }

    assert (
        OpenAIProvider._canonical_assistant_text(conversation, "assistant-new")
        == "complete multimodal answer"
    )
