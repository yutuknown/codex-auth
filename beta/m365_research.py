"""Research-gated M365 protocol probes.

No endpoint is guessed here.  A capability moves only when a redacted captured
contract and an end-to-end round trip are recorded by a local test.
"""

from __future__ import annotations

from typing import Any

PROBES: dict[str, str] = {
    "model_catalog": "account-scoped bearer request and validated response",
    "quota": "account-scoped bearer request and validated response",
    "client_tools": "tool-call and tool-result round trip",
    "generated_image_retrieval": "public or byte-safe generated-image retrieval",
    "native_history": "native system and structured-history fields accepted upstream",
    "custom_instructions": "explicit opt-in, local-only schema read of private personalization data",
    "memories": "explicit opt-in, local-only schema read of private personalization data",
}

VERIFIED_PRIVATE_READS = {"custom_instructions", "memories"}


def research_report() -> dict[str, Any]:
    return {
        "scope": "local beta evidence probes",
        "activation_rule": "captured contract plus successful round trip",
        "probes": [
            {
                "name": name,
                "state": (
                    "verified_private_read"
                    if name in VERIFIED_PRIVATE_READS
                    else "blocked_by_upstream"
                ),
                "required_evidence": evidence,
                "safe_capture_only": True,
            }
            for name, evidence in PROBES.items()
        ],
    }
