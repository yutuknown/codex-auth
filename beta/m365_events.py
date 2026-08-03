"""Safe M365 event metadata extraction for the local beta."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def _text(value: Any, limit: int = 160) -> str:
    return str(value or "").replace("\n", " ").strip()[:limit]


def _domain(value: Any) -> str:
    try:
        return (urlsplit(str(value)).hostname or "").lower()[:120]
    except ValueError:
        return ""


def citation_metadata(references: Any) -> list[dict[str, str]]:
    if not isinstance(references, list):
        return []
    result: list[dict[str, str]] = []
    for reference in references[:12]:
        if not isinstance(reference, dict):
            continue
        title = _text(reference.get("title") or reference.get("providerDisplayName"))
        domain = _domain(reference.get("url") or reference.get("sourceUrl"))
        item = {key: value for key, value in {"title": title, "domain": domain}.items() if value}
        if item:
            result.append(item)
    return result


def suggestions_metadata(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:8]:
        candidate = value.get("text") if isinstance(value, dict) else value
        normalized = _text(candidate)
        if normalized:
            result.append(normalized)
    return result


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    """Drop all upstream URLs, headers, IDs, and opaque provider objects."""

    event_type = str(event.get("type") or "")
    result = {
        key: event[key]
        for key in ("type", "count", "elapsed_ms", "lane", "operation")
        if key in event and event[key] is not None
    }
    if event_type == "citation" and isinstance(event.get("citations"), list):
        result["citations"] = event["citations"][:12]
    if event_type in {"suggestions", "suggestions_detail"} and isinstance(event.get("suggestions"), list):
        result["suggestions"] = event["suggestions"][:8]
    if event_type in {"generated_code", "image", "image_progress", "adaptive_card", "plugin"}:
        if isinstance(event.get("artifact"), dict):
            result["artifact"] = event["artifact"]
        if isinstance(event.get("metadata"), dict):
            result["metadata"] = event["metadata"]
    return result
