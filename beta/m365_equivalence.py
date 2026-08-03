"""Evidence-led comparison of M365 beta and Antigravity Claude Proxy.

Antigravity is pinned to 055699f.  This module deliberately starts each M365
feature as implemented-but-unverified: only a commit-bound hosted campaign can
promote it to verified_live.
"""

from __future__ import annotations

from typing import Any

from beta.m365_verification import safe_latest_verification

CAPABILITY_STATES = (
    "implemented_unverified", "verified_live", "verified_mock_only",
    "blocked_by_upstream", "unsupported", "out_of_scope",
)


def _features() -> list[dict[str, str]]:
    return [
        {"feature": "oauth_bearer", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "zero_cookie_generation"},
        {"feature": "oauth_refresh_and_durability", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "refresh_restart_durability"},
        {"feature": "anthropic_messages", "comparison": "equivalent", "m365_beta": "implemented", "evidence_id": "anthropic_text_stream"},
        {"feature": "openai_chat_completions", "comparison": "M365 extension", "m365_beta": "implemented", "evidence_id": "chat_text_stream"},
        {"feature": "openai_responses", "comparison": "M365 extension", "m365_beta": "implemented", "evidence_id": "responses_text_stream"},
        {"feature": "conversation_continuity", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "conversation_continuity"},
        {"feature": "reasoning", "comparison": "partial", "m365_beta": "unsigned_summary", "evidence_id": "reasoning_lanes"},
        {"feature": "dynamic_model_catalog", "comparison": "blocked by M365 upstream", "m365_beta": "captured_catalog", "evidence_id": "model_catalog_probe"},
        {"feature": "model_quota", "comparison": "blocked by M365 upstream", "m365_beta": "unavailable", "evidence_id": "model_quota_probe"},
        {"feature": "native_system_and_history", "comparison": "partial", "m365_beta": "compiled_transcript", "evidence_id": "structured_history_probe"},
        {"feature": "caller_tools_and_results", "comparison": "blocked by M365 upstream", "m365_beta": "unavailable", "evidence_id": "caller_tools_probe"},
        {"feature": "image_input", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "image_input_matrix"},
        {"feature": "file_input", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "graph_file_marker"},
        {"feature": "generated_image_output", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "generated_image_bytes"},
        {"feature": "web_search_and_citations", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "search_citation_events"},
        {"feature": "usage_and_cache_accounting", "comparison": "blocked by M365 upstream", "m365_beta": "local_estimate", "evidence_id": "usage_contract_probe"},
        {"feature": "sampling_controls", "comparison": "intentionally out of scope", "m365_beta": "rejected", "evidence_id": "unsupported_controls"},
        {"feature": "reliability_and_retries", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "reliability_fault_matrix"},
        {"feature": "telemetry_and_live_logs", "comparison": "partial", "m365_beta": "implemented", "evidence_id": "telemetry_stream"},
        {"feature": "multi_account_routing", "comparison": "intentionally out of scope", "m365_beta": "one_account", "evidence_id": "single_account_design"},
        {"feature": "custom_instructions_and_memories", "comparison": "M365 extension", "m365_beta": "local_read_probe", "evidence_id": "personalization_canary"},
    ]


def equivalence_report() -> dict[str, Any]:
    verification = safe_latest_verification()
    verified_ids: set[str] = set(verification.get("passed_evidence_ids") or [])
    mock_ids: set[str] = set()
    features: list[dict[str, Any]] = []
    for item in _features():
        feature = dict(item)
        if feature["comparison"] == "blocked by M365 upstream":
            state = "blocked_by_upstream"
        elif feature["comparison"] == "intentionally out of scope":
            state = "out_of_scope"
        elif feature["m365_beta"] in {"unavailable", "rejected"}:
            state = "unsupported"
        elif feature["evidence_id"] in verified_ids:
            state = "verified_live"
        elif feature["evidence_id"] in mock_ids:
            state = "verified_mock_only"
        else:
            state = "implemented_unverified"
        feature["state"] = state
        features.append(feature)
    return {
        "comparison": "antigravity-claude-proxy@055699f_to_m365_bearer_beta",
        "scope": "one-account M365 beta; no production promotion",
        "feature_count": len(features),
        "state_values": list(CAPABILITY_STATES),
        "verification": verification,
        "features": features,
        "truthfulness_rule": "Only a passing hosted manifest for the running commit can claim verified_live.",
    }
