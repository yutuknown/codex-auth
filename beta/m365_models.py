"""Account-scoped model catalog for the local M365 bearer beta."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_DIRECTORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIRECTORY))
SOURCE_DIRECTORY = REPOSITORY_DIRECTORY / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from beta.m365_bearer import BetaConfigurationError, M365BearerBeta
from codex_auth.providers.microsoft365 import MODEL_TITLES, MODEL_TONES, _slug_for_tone

PROVIDER_ID = "m365-copilot"
CATALOG_SOURCES = {
    "authenticated_chat_shell",
    "captured_chat_shell",
    "live_probe",
    "fallback",
}


@dataclass(frozen=True)
class M365Model:
    slug: str
    tone: str
    title: str
    description: str
    reasoning: bool
    source: str
    verified_at: int | None = None

    @property
    def public_id(self) -> str:
        return f"{PROVIDER_ID}:{self.slug}"

    def api_model(self) -> dict[str, Any]:
        return {
            "id": self.slug,
            "object": "model",
            "created": self.verified_at or 0,
            "owned_by": "microsoft",
            "description": self.description or self.title,
            "provider": PROVIDER_ID,
            "upstream_id": self.tone,
            "reasoning": self.reasoning,
            "catalog_source": self.source,
            "namespaced_id": self.public_id,
        }


@dataclass(frozen=True)
class ResolvedM365Model:
    requested_id: str
    canonical_id: str
    model: M365Model
    alias_applied: bool


class M365ModelCatalog:
    """Resolve public IDs and aliases against an account-visible catalog."""

    def __init__(
        self,
        models: list[M365Model],
        *,
        source: str,
        default_slug: str,
        captured_at: int | None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        if not models:
            raise BetaConfigurationError("M365 model catalog must not be empty")
        self.models = {model.slug: model for model in models}
        self.source = source
        self.default_slug = default_slug if default_slug in self.models else next(iter(self.models))
        self.captured_at = captured_at
        self.aliases = self._validate_aliases(aliases or {})

    @classmethod
    def from_beta_record(cls, raw: dict[str, Any]) -> "M365ModelCatalog":
        record = raw.get("model_catalog")
        aliases = raw.get("model_aliases") or {}
        if not isinstance(aliases, dict):
            raise BetaConfigurationError("model_aliases must be an object")
        if not isinstance(record, dict):
            return cls._fallback(aliases)

        source = str(record.get("source") or "captured_chat_shell")
        if source not in CATALOG_SOURCES or source == "fallback":
            raise BetaConfigurationError("model_catalog source is invalid")
        raw_models = record.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise BetaConfigurationError("model_catalog.models must be a non-empty array")
        captured_at = record.get("captured_at")
        if captured_at is not None:
            try:
                captured_at = int(captured_at)
            except (TypeError, ValueError) as exc:
                raise BetaConfigurationError("model_catalog.captured_at must be an integer") from exc

        models: list[M365Model] = []
        seen_tones: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict):
                raise BetaConfigurationError("each model_catalog model must be an object")
            tone = str(item.get("tone") or "").strip()
            if not tone or tone in seen_tones:
                if not tone:
                    raise BetaConfigurationError("each model_catalog model requires tone")
                continue
            seen_tones.add(tone)
            slug = str(item.get("slug") or _slug_for_tone(tone)).strip()
            title = str(item.get("title") or tone).strip()
            verified_at = item.get("verified_at")
            if verified_at is not None:
                try:
                    verified_at = int(verified_at)
                except (TypeError, ValueError) as exc:
                    raise BetaConfigurationError("model verified_at must be an integer") from exc
            models.append(
                M365Model(
                    slug=slug,
                    tone=tone,
                    title=title,
                    description=str(item.get("description") or ""),
                    reasoning=bool(
                        item.get("reasoning")
                        if "reasoning" in item
                        else "reasoning" in tone.lower()
                    ),
                    source=source,
                    verified_at=verified_at,
                )
            )
        default_tone = str(record.get("default_tone") or "")
        default_slug = _slug_for_tone(default_tone) if default_tone else models[0].slug
        return cls(
            models,
            source=source,
            default_slug=default_slug,
            captured_at=captured_at,
            aliases={str(key): str(value) for key, value in aliases.items()},
        )

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> "M365ModelCatalog":
        raw = M365BearerBeta.from_directory(directory).credential.raw
        return cls.from_beta_record(raw)

    @classmethod
    def _fallback(cls, aliases: dict[str, Any]) -> "M365ModelCatalog":
        models = [
            M365Model(
                slug=slug,
                tone=tone,
                title=MODEL_TITLES[slug],
                description=f"Microsoft 365 Copilot mode using upstream tone {tone}",
                reasoning="reasoning" in tone.lower(),
                source="fallback",
            )
            for slug, tone in MODEL_TONES.items()
        ]
        return cls(
            models,
            source="fallback",
            default_slug="auto",
            captured_at=None,
            aliases={str(key): str(value) for key, value in aliases.items()},
        )

    def _normalize_public_id(self, value: str) -> str:
        normalized = value.strip()
        prefix = f"{PROVIDER_ID}:"
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
        return normalized

    def _validate_aliases(self, aliases: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for alias, target in aliases.items():
            key = self._normalize_public_id(str(alias))
            value = self._normalize_public_id(str(target))
            if not key or not value:
                raise BetaConfigurationError("model aliases require non-empty names")
            if key in self.models:
                raise BetaConfigurationError(f"model alias '{key}' shadows a canonical model")
            normalized[key] = value
        for alias in normalized:
            visited = {alias}
            target = normalized[alias]
            while target in normalized:
                if target in visited:
                    raise BetaConfigurationError(f"model alias cycle includes '{target}'")
                visited.add(target)
                target = normalized[target]
            if target not in self.models:
                raise BetaConfigurationError(
                    f"model alias '{alias}' targets unknown model '{target}'"
                )
        return normalized

    def resolve(self, requested_id: str | None) -> ResolvedM365Model:
        requested = self._normalize_public_id(requested_id or self.default_slug)
        canonical = requested
        visited: set[str] = set()
        while canonical in self.aliases:
            if canonical in visited:
                raise BetaConfigurationError("model alias cycle detected")
            visited.add(canonical)
            canonical = self.aliases[canonical]
        model = self.models.get(canonical)
        if model is None:
            available = ", ".join(sorted(self.models))
            raise BetaConfigurationError(
                f"unknown M365 model '{requested}'. Available models: {available}"
            )
        return ResolvedM365Model(
            requested_id=requested,
            canonical_id=canonical,
            model=model,
            alias_applied=requested != canonical,
        )

    def safe_status(self) -> dict[str, Any]:
        age = max(0, int(time.time()) - self.captured_at) if self.captured_at else None
        return {
            "source": self.source,
            "model_count": len(self.models),
            "default_model": self.default_slug,
            "captured_at": self.captured_at,
            "age_seconds": age,
            "alias_count": len(self.aliases),
            "account_scoped": self.source != "fallback",
            "dynamic": self.source == "authenticated_chat_shell",
        }

    def api_list(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [model.api_model() for model in self.models.values()],
            "catalog": self.safe_status(),
        }

    def api_get(self, requested_id: str) -> dict[str, Any]:
        resolved = self.resolve(requested_id)
        return {
            **resolved.model.api_model(),
            "requested_id": resolved.requested_id,
            "canonical_id": resolved.canonical_id,
            "alias_applied": resolved.alias_applied,
        }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the safe M365 beta model catalog")
    parser.add_argument("command", choices=("status", "list"))
    arguments = parser.parse_args()
    try:
        catalog = M365ModelCatalog.from_directory()
        output = catalog.safe_status() if arguments.command == "status" else catalog.api_list()
        print(json.dumps(output, sort_keys=True))
        return 0
    except BetaConfigurationError as exc:
        print(json.dumps({"state": "not_configured", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
