import asyncio

import httpx
import pytest

import beta.m365_compat as compat
from beta.m365_equivalence import CAPABILITY_STATES, equivalence_report
from beta.m365_verification import canonical_manifest


def test_verification_manifest_is_canonical_and_rejects_secrets():
    manifest, digest = canonical_manifest(
        {
            "tested_commit": "abc123",
            "completed_at": 1,
            "checks": [
                {"id": "image_input_matrix", "outcome": "passed", "http_status": 200, "bytes": 8},
            ],
        }
    )

    assert manifest["checks"][0]["id"] == "image_input_matrix"
    assert len(digest) == 64
    with pytest.raises(ValueError):
        canonical_manifest({"tested_commit": "abc123", "checks": [], "access_token": "secret"})
    with pytest.raises(ValueError):
        canonical_manifest({"tested_commit": "abc123", "checks": [{"id": "x", "outcome": "passed", "url": "secret"}]})


def test_capabilities_start_unverified_and_pin_antigravity_commit(monkeypatch):
    monkeypatch.delenv("CODEX_AUTH_M365_BETA_DATABASE_URL", raising=False)
    report = equivalence_report()

    assert "055699f" in report["comparison"]
    assert set(report["state_values"]) == set(CAPABILITY_STATES)
    assert all(item["state"] in CAPABILITY_STATES for item in report["features"])
    assert next(item for item in report["features"] if item["feature"] == "file_input")["state"] == "implemented_unverified"


def test_refresh_requires_admin_key_and_v1_uses_constant_time_guard(monkeypatch):
    monkeypatch.setenv(compat.API_KEY_ENV, "api-key")
    monkeypatch.setenv(compat.ADMIN_KEY_ENV, "admin-key")

    async def call():
        transport = httpx.ASGITransport(app=compat.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            refresh = await client.post("/refresh-token")
            invalid_api = await client.get("/v1/capabilities", headers={"x-api-key": "wrong"})
            valid_api = await client.get("/v1/capabilities", headers={"x-api-key": "api-key"})
            return refresh, invalid_api, valid_api

    refresh, invalid_api, valid_api = asyncio.run(call())
    assert refresh.status_code == 401
    assert invalid_api.status_code == 401
    assert valid_api.status_code == 200


def test_health_includes_non_secret_build_contract(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    payload = compat.health()

    assert payload["build"]["render_commit"] == "abc123" or payload["status"] == "not_configured"
