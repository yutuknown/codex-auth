"""Bounded, secret-free conversation coordination for the local M365 beta.

The coordinator maps a caller supplied proxy conversation key to an opaque
upstream ConversationId.  It intentionally holds no prompt or response text.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CONVERSATION_SECRET_ENV = "CODEX_AUTH_M365_BETA_CONVERSATION_HMAC_KEY"
UPSTREAM_MAX_TURNS_ENV = "CODEX_AUTH_M365_BETA_UPSTREAM_MAX_TURNS"
MAX_CONVERSATIONS = 1_000
CONVERSATION_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_UPSTREAM_MAX_TURNS = 24
ROLLOVER_CONTEXT_TURNS = 6


class ConversationConflict(RuntimeError):
    """A duplicate request is already executing for this conversation."""


@dataclass
class _Conversation:
    upstream_id: str
    proxy_id: str
    created_at: float
    updated_at: float
    last_request_hash: str | None = None
    in_flight: set[str] = field(default_factory=set)
    completed: dict[str, dict[str, Any]] = field(default_factory=dict)
    turn_hashes: tuple[str, ...] = ()
    upstream_turns: int = 0
    model_id: str | None = None


class ConversationCoordinator:
    """In-memory beta coordinator with safe, bounded idempotency metadata."""

    def __init__(self, secret: str | None = None, now: callable = time.time) -> None:
        self._secret = (secret or os.environ.get(CONVERSATION_SECRET_ENV) or "local-beta").encode()
        self._now = now
        self._items: dict[str, _Conversation] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _max_upstream_turns() -> int:
        try:
            configured = int(os.environ.get(UPSTREAM_MAX_TURNS_ENV, DEFAULT_UPSTREAM_MAX_TURNS))
        except ValueError:
            configured = DEFAULT_UPSTREAM_MAX_TURNS
        return max(6, min(configured, 128))

    def _caller_key(self, explicit: str | None, first_user_text: str) -> str:
        if explicit:
            return "caller:" + explicit.strip()[:256]
        digest = hmac.new(self._secret, first_user_text.encode("utf-8"), hashlib.sha256).hexdigest()
        return "first-user:" + digest

    @staticmethod
    def _safe_proxy_id(key: str) -> str:
        return "m365c_" + hashlib.sha256(key.encode()).hexdigest()[:24]

    @staticmethod
    def request_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        stale = [key for key, item in self._items.items() if now - item.updated_at > CONVERSATION_TTL_SECONDS]
        for key in stale:
            self._items.pop(key, None)
        if len(self._items) > MAX_CONVERSATIONS:
            oldest = sorted(self._items, key=lambda key: self._items[key].updated_at)[: len(self._items) - MAX_CONVERSATIONS]
            for key in oldest:
                self._items.pop(key, None)

    def acquire(
        self,
        *,
        explicit_id: str | None,
        first_user_text: str,
        request_text: str,
        turn_hashes: tuple[str, ...] = (),
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve a turn and return only a safe proxy id plus internal upstream id."""

        now = self._now()
        key = self._caller_key(explicit_id, first_user_text)
        request_id = self.request_hash(request_text)
        with self._lock:
            self._prune(now)
            item = self._items.get(key)
            is_new = item is None
            if item is None:
                item = _Conversation(str(uuid.uuid4()), self._safe_proxy_id(key), now, now)
                self._items[key] = item
            delta_start = 0
            continuity = "new"
            normalized_model = (model_id or "").strip().lower() or None
            model_changed = bool(
                not is_new
                and normalized_model
                and item.model_id
                and normalized_model != item.model_id
            )
            if model_changed:
                # M365 Web starts a new upstream chat when the selected model
                # changes. Keep the caller's proxy conversation stable, but
                # fork the provider conversation and replay only bounded recent
                # context so a local chat can continue naturally.
                item.upstream_id = str(uuid.uuid4())
                item.completed.clear()
                continuity = "model_switched"
                delta_start = max(0, len(turn_hashes) - ROLLOVER_CONTEXT_TURNS)
                item.upstream_turns = len(turn_hashes) - delta_start
            elif not is_new and turn_hashes and item.turn_hashes:
                if turn_hashes[: len(item.turn_hashes)] == item.turn_hashes:
                    delta_start = len(item.turn_hashes)
                    continuity = "continued"
                    appended_count = len(turn_hashes) - delta_start
                    if item.upstream_turns + appended_count > self._max_upstream_turns():
                        item.upstream_id = str(uuid.uuid4())
                        delta_start = max(0, len(turn_hashes) - ROLLOVER_CONTEXT_TURNS)
                        item.upstream_turns = len(turn_hashes) - delta_start
                        continuity = "rolled_over"
                    else:
                        item.upstream_turns += appended_count
                elif turn_hashes != item.turn_hashes:
                    # An edited/branched history must not corrupt the existing
                    # M365 thread. Fork internally without retaining the text.
                    item.upstream_id = str(uuid.uuid4())
                    item.proxy_id = self._safe_proxy_id(key + ":fork:" + self.request_hash(request_text))
                    item.completed.clear()
                    continuity = "forked"
                    delta_start = max(0, len(turn_hashes) - ROLLOVER_CONTEXT_TURNS)
                    item.upstream_turns = len(turn_hashes) - delta_start
            if request_id in item.in_flight:
                raise ConversationConflict("conversation_request_in_progress")
            item.in_flight.add(request_id)
            item.updated_at = now
            item.last_request_hash = request_id
            if turn_hashes:
                item.turn_hashes = turn_hashes
                if is_new:
                    item.upstream_turns = len(turn_hashes)
            if normalized_model:
                item.model_id = normalized_model
            return {
                "proxy_id": item.proxy_id,
                "upstream_id": item.upstream_id,
                "request_hash": request_id,
                "delta_start": delta_start,
                "continuity": continuity,
            }

    def complete(self, token: dict[str, Any], *, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            for item in self._items.values():
                if item.proxy_id == token["proxy_id"]:
                    item.in_flight.discard(token["request_hash"])
                    item.updated_at = self._now()
                    # A bounded safe result only; caller content never enters this cache.
                    if result is not None:
                        item.completed[token["request_hash"]] = dict(result)
                        while len(item.completed) > 8:
                            item.completed.pop(next(iter(item.completed)))
                    return

    def fail(self, token: dict[str, Any]) -> None:
        self.complete(token)

    @staticmethod
    def public_metadata(token: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": token["proxy_id"],
            "provider": "m365-copilot",
            "upstream_continuity": token.get("continuity", "continued"),
            "rollover": token.get("continuity") == "rolled_over",
            "model_switch": token.get("continuity") == "model_switched",
        }


coordinator = ConversationCoordinator()
