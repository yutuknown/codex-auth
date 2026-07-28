import pytest

from codex_auth.providers.openai.provider import _message_delta, parse_netscape_cookies


def test_parse_netscape_cookie_and_http_only_prefix():
    text = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "#HttpOnly_.chatgpt.com\tTRUE\t/\tTRUE\t0\tsession\tsecret",
        ]
    )

    assert parse_netscape_cookies(text) == [
        {
            "name": "session",
            "value": "secret",
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
        }
    ]


def test_parse_netscape_cookie_rejects_invalid_record():
    with pytest.raises(ValueError, match="line 1"):
        parse_netscape_cookies("not-a-cookie")


def test_message_delta_reconstructs_initial_and_patch_events():
    state = {}
    initial = {
        "v": {
            "conversation_id": "conversation",
            "message": {
                "id": "assistant",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["Hello"]},
            },
        }
    }
    patch = {
        "v": [
            {"o": "append", "p": "/message/content/parts/0", "v": " world"},
        ]
    }

    assert _message_delta(initial, state) == "Hello"
    assert _message_delta(patch, state) == " world"
    assert state["text"] == "Hello world"
    assert state["conversation_id"] == "conversation"


def test_message_delta_handles_top_level_v1_patch():
    state = {"role": "assistant", "content_type": "text", "text": ""}

    assert (
        _message_delta(
            {"p": "/message/content/parts/0", "o": "append", "v": "prefix"},
            state,
        )
        == "prefix"
    )
