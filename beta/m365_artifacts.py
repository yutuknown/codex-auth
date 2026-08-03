"""Ephemeral, secret-free artifacts emitted by the local M365 beta.

The upstream web client may mention generated images or cards using protected
URLs.  This module deliberately never stores those URLs.  It can only expose
base64 data when a caller has already obtained verified image bytes.
"""

from __future__ import annotations

import base64
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ITEMS = 12
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024
DEFAULT_TTL_SECONDS = 300


def _safe_metadata(value: Any) -> dict[str, Any]:
    """Keep a small descriptive subset; never retain URLs or auth-like keys."""

    if not isinstance(value, dict):
        return {}
    allowed = {"title", "name", "domain", "language", "count", "source", "phase"}
    result: dict[str, Any] = {}
    for key in allowed:
        candidate = value.get(key)
        if isinstance(candidate, str):
            result[key] = candidate[:160]
        elif isinstance(candidate, (int, float, bool)):
            result[key] = candidate
    return result


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    mime_type: str
    content: bytes = field(repr=False)
    expires_at: float
    metadata: dict[str, Any]

    def descriptor(self) -> dict[str, Any]:
        return {
            "id": self.artifact_id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "size": len(self.content),
            "availability": "embedded",
            "metadata": dict(self.metadata),
        }


class M365ArtifactStore:
    """Small process-local cache for verified response artifacts only."""

    def __init__(
        self,
        *,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.max_items = max_items
        self.max_total_bytes = max_total_bytes
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, ArtifactRecord] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for artifact_id in [
            key for key, item in self._items.items() if item.expires_at <= now
        ]:
            self._items.pop(artifact_id, None)

    def put_image(
        self,
        content: bytes,
        mime_type: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not mime_type.startswith("image/"):
            raise ValueError("artifact requires an image media type")
        if not content or len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact content must be between 1 byte and 2 MB")
        now = time.time()
        record = ArtifactRecord(
            artifact_id=f"m365_artifact_{uuid.uuid4().hex}",
            kind="image",
            mime_type=mime_type,
            content=bytes(content),
            expires_at=now + self.ttl_seconds,
            metadata=_safe_metadata(metadata or {}),
        )
        with self._lock:
            self._expire(now)
            self._items[record.artifact_id] = record
            while (
                len(self._items) > self.max_items
                or sum(len(item.content) for item in self._items.values())
                > self.max_total_bytes
            ):
                self._items.popitem(last=False)
        return record.descriptor()

    def image_block(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._expire()
            item = self._items.get(artifact_id)
            if item is None or item.kind != "image":
                return None
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": item.mime_type,
                    "data": base64.b64encode(item.content).decode("ascii"),
                },
            }

    @staticmethod
    def _image_urls(cards: Any) -> list[str]:
        """Extract only card image fields; URLs never leave this method."""

        urls: list[str] = []

        def visit(value: Any, image_context: bool = False) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item, image_context)
            elif isinstance(value, dict):
                for key, item in value.items():
                    normalized = str(key).lower()
                    is_image = image_context or normalized in {
                        "image",
                        "images",
                        "imageurl",
                        "image_url",
                        "imagereferenceurls",
                    }
                    if normalized == "imagereferenceurls" and isinstance(item, list):
                        for reference in item:
                            if isinstance(reference, str):
                                parsed = urlsplit(reference)
                                if parsed.scheme == "https" and parsed.hostname:
                                    urls.append(reference)
                        continue
                    if normalized == "url" and image_context and isinstance(item, str):
                        parsed = urlsplit(item)
                        if parsed.scheme == "https" and parsed.hostname:
                            urls.append(item)
                    else:
                        visit(item, is_image)

        visit(cards)
        return list(dict.fromkeys(urls))[:4]

    def resolve_generated_cards(
        self,
        cards: Any,
        *,
        fetcher: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve only verified public generated-image bytes.

        The remote fetcher enforces public DNS, HTTPS, redirect, cookie, and
        response-size limits.  Failures intentionally collapse into an
        unretrievable descriptor so protected card URLs cannot leak.
        """

        urls = self._image_urls(cards)
        if not urls:
            return []
        if fetcher is None:
            # Delayed import prevents the beta transport/remote helper cycle.
            from beta.m365_remote import RemoteAttachmentFetcher

            fetcher = RemoteAttachmentFetcher()
        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                remote = fetcher.fetch(url, name="m365-generated-image")
                if not str(remote.mime_type).startswith("image/"):
                    raise ValueError("generated artifact is not an image")
                results.append(
                    self.put_image(
                        remote.content,
                        remote.mime_type,
                        metadata={"name": remote.name, "source": "m365_generated_card"},
                    )
                )
            except Exception as exc:
                phase = str(exc)
                if not re.fullmatch(r"[A-Za-z0-9_:-]{1,96}", phase):
                    phase = type(exc).__name__
                results.append(
                    self.unretrievable(
                        "image",
                        {"source": "m365_generated_card", "phase": phase},
                    )
                )
        return results

    @staticmethod
    def unretrievable(
        kind: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "availability": "unretrievable",
            "metadata": _safe_metadata(metadata or {}),
        }


artifact_store = M365ArtifactStore()
