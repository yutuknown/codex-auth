"""Truthful Antigravity-to-M365 beta capability mapping.

The report is intentionally static and secret-free. Runtime endpoints add
credential and model-catalog health separately.
"""

from __future__ import annotations

from typing import Any


def equivalence_report() -> dict[str, Any]:
    """Return the machine-readable feature map used by tests and the beta API."""

    features = [
        {
            "feature": "oauth_bearer",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": "zero-cookie SignalR generation with renewable bearer credentials",
        },
        {
            "feature": "anthropic_messages",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": "POST /v1/messages, streaming and buffered",
        },
        {
            "feature": "openai_chat_completions",
            "antigravity": "unavailable",
            "m365_beta": "supported",
            "evidence": "POST /v1/chat/completions, streaming and buffered",
        },
        {
            "feature": "streaming",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": "SignalR replacement snapshots normalized to append deltas",
        },
        {
            "feature": "reasoning",
            "antigravity": "signed_thinking",
            "m365_beta": "summary_only",
            "evidence": "provider-authored reasoning progress; no thought signature",
        },
        {
            "feature": "model_aliases",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": "aliases resolve before catalog validation",
        },
        {
            "feature": "dynamic_model_catalog",
            "antigravity": "supported",
            "m365_beta": "captured_or_fallback",
            "evidence": "M365 has no confirmed bearer model-list endpoint",
        },
        {
            "feature": "model_quota",
            "antigravity": "supported",
            "m365_beta": "unavailable",
            "evidence": "no confirmed M365 bearer quota endpoint",
        },
        {
            "feature": "oauth_refresh",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": (
                "live-proven Microsoft broker refresh with serialized atomic token "
                "rotation and successful zero-cookie generation using the new bearer"
            ),
        },
        {
            "feature": "credential_restart_persistence",
            "antigravity": "supported",
            "m365_beta": "supported_with_constraints",
            "evidence": (
                "environment seeds rotate in process memory unless an explicit "
                "persistent state file is mounted; readiness exposes the distinction"
            ),
        },
        {
            "feature": "structured_multi_turn",
            "antigravity": "supported",
            "m365_beta": "structured_transcript",
            "evidence": (
                "roles and blocks are preserved in a request IR and compiled into "
                "a live-proven response-preferences plus conversation transcript"
            ),
        },
        {
            "feature": "native_system_instruction",
            "antigravity": "supported",
            "m365_beta": "response_preferences",
            "evidence": (
                "live-proven preference fidelity without the rejected System label; "
                "no native upstream system field is claimed"
            ),
        },
        {
            "feature": "client_function_tools",
            "antigravity": "supported",
            "m365_beta": "unavailable",
            "evidence": "M365 web plugins are not an external function-calling protocol",
        },
        {
            "feature": "historical_tool_results",
            "antigravity": "supported",
            "m365_beta": "structured_transcript",
            "evidence": (
                "OpenAI tool messages and Anthropic tool_result blocks are retained "
                "as bounded labelled context without claiming tool invocation"
            ),
        },
        {
            "feature": "image_input",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": (
                "live-proven base64 image upload and ImageFile binding with "
                "zero cookies; public HTTPS image URLs are bounded, fetched, and "
                "staged through the same proven upload"
            ),
        },
        {
            "feature": "generated_image_output",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": (
                "live zero-cookie generated-image references resolved to verified "
                "image bytes and bounded in-memory base64 artifacts"
            ),
        },
        {
            "feature": "file_input",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": (
                "zero-cookie Graph create-upload-session, extraction, File annotation, "
                "and exact marker readback passed; the separate Graph resource bearer "
                "is acquired from the same broker refresh session"
            ),
        },
        {
            "feature": "remote_attachment_urls",
            "antigravity": "supported",
            "m365_beta": "supported_with_constraints",
            "evidence": (
                "live-proven public HTTPS image retrieval with redirect, DNS, "
                "cookie, and size guards; non-image URLs still require Graph"
            ),
        },
        {
            "feature": "tool_result_images",
            "antigravity": "supported",
            "m365_beta": "unavailable",
            "evidence": (
                "Antigravity converts image blocks inside tool results; the "
                "M365 beta has no client function-tool loop"
            ),
        },
        {
            "feature": "web_search",
            "antigravity": "model_dependent",
            "m365_beta": "supported",
            "evidence": "BingWebSearch plugin and observed search/citation frames",
        },
        {
            "feature": "usage_accounting",
            "antigravity": "supported",
            "m365_beta": "local_estimate",
            "evidence": (
                "responses provide labelled lexical estimates; SignalR has no "
                "stable upstream token-usage contract"
            ),
        },
        {
            "feature": "sampling_controls",
            "antigravity": "supported",
            "m365_beta": "unavailable",
            "evidence": (
                "temperature, top_p, top_k, max_tokens, thinking, and stop "
                "sequences are rejected until an upstream mapping is proven"
            ),
        },
        {
            "feature": "bounded_reliability",
            "antigravity": "advanced",
            "m365_beta": "pre_submit_adaptive",
            "evidence": (
                "a pre-submit 401 refreshes once, transient connects back off, "
                "and a submitted generation is never replayed"
            ),
        },
        {
            "feature": "persistent_telemetry",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": (
                "bounded secret-free JSONL, health metrics, latency percentiles, "
                "failure phases, and live log SSE"
            ),
        },
        {
            "feature": "multi_account_pool",
            "antigravity": "supported",
            "m365_beta": "out_of_scope",
            "evidence": "local beta intentionally uses one personal account",
        },
        {
            "feature": "api_key_guard",
            "antigravity": "supported",
            "m365_beta": "supported",
            "evidence": "optional CODEX_AUTH_M365_BETA_API_KEY",
        },
    ]
    fully_equivalent = sum(
        1
        for item in features
        if item["antigravity"] == "supported" and item["m365_beta"] == "supported"
    )
    states = {
        "supported": "supported",
        "supported_with_constraints": "supported",
        "summary_only": "verified_but_not_public",
        "structured_transcript": "verified_but_not_public",
        "response_preferences": "verified_but_not_public",
        "pre_submit_adaptive": "verified_but_not_public",
        "local_estimate": "verified_but_not_public",
        "captured_or_fallback": "experimental",
        "unavailable": "blocked_by_upstream",
        "out_of_scope": "unavailable",
    }
    for item in features:
        item["state"] = states.get(str(item["m365_beta"]), "experimental")
    return {
        "comparison": "antigravity-claude-proxy_to_m365_bearer_beta",
        "scope": "single-account local or explicitly configured hosted beta",
        "fully_equivalent_supported_features": fully_equivalent,
        "feature_count": len(features),
        "state_values": [
            "supported",
            "verified_but_not_public",
            "experimental",
            "unavailable",
            "blocked_by_upstream",
        ],
        "features": features,
        "truthfulness_rule": (
            "Unsupported upstream behavior is rejected or labelled; it is never simulated."
        ),
    }
