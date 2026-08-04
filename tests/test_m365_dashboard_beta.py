import asyncio

import httpx

import beta.m365_compat as compat
from beta.m365_dashboard import (
    DASHBOARD_COOKIE,
    dashboard_html,
    dashboard_session_valid,
    issue_dashboard_session,
)


def test_dashboard_session_is_signed_short_lived_and_contains_no_admin_key(monkeypatch):
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_DASHBOARD_SESSION_KEY", "dashboard-secret")

    token = issue_dashboard_session(now=1_000)

    assert "dashboard-secret" not in token
    assert dashboard_session_valid(token, now=1_001) is True
    assert dashboard_session_valid(token, now=1_000 + 8 * 60 * 60 + 1) is False
    assert dashboard_session_valid(token + "tampered", now=1_001) is False


def test_dashboard_shell_exposes_account_models_capabilities_and_logs_without_secrets():
    page = dashboard_html()

    assert "/assets/logo-dark.svg" in page
    assert "themeToggle" in page
    assert "sidebarToggle" in page
    assert "Codex /" in page
    assert page.count('class="nav-text"') == 6
    assert all(f'id="{view}" class="view' in page for view in ("overview", "account", "models", "capabilities", "verification", "logs"))
    assert page.count('class="provider-subtitle"') == 1
    assert "codex-auth-beta-sidebar-v2" in page
    assert "No runtime events yet" in page
    assert "side:before" not in page
    assert "top:after" not in page
    assert "Connect Microsoft 365" in page
    assert "Paste OAuth JSON" in page
    assert "Available Models" in page
    assert "Capability Evidence" in page
    assert "Commit-bound verification" in page
    assert "Network Inspector" in page
    assert "access_token" not in page
    assert "refresh_token" not in page


def test_dashboard_requires_login_and_exchanges_key_for_httponly_cookie(monkeypatch):
    monkeypatch.setenv(compat.ADMIN_KEY_ENV, "operator-key")
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_DASHBOARD_SESSION_KEY", "session-key")

    async def call():
        transport = httpx.ASGITransport(app=compat.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            page = await client.get("/dashboard")
            unauthorized = await client.get("/dashboard/api/overview")
            rejected = await client.post("/dashboard/login", json={"admin_key": "wrong"})
            accepted = await client.post("/dashboard/login", json={"admin_key": "operator-key"})
            overview = await client.get("/dashboard/api/overview")
            return page, unauthorized, rejected, accepted, overview

    page, unauthorized, rejected, accepted, overview = asyncio.run(call())

    assert page.status_code == 200
    assert unauthorized.status_code == 401
    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert DASHBOARD_COOKIE in accepted.headers["set-cookie"]
    assert "HttpOnly" in accepted.headers["set-cookie"]
    assert "operator-key" not in accepted.headers["set-cookie"]
    assert overview.status_code == 200
    assert "operator-key" not in overview.text


def test_dashboard_credential_import_returns_only_safe_lifecycle(monkeypatch):
    monkeypatch.setenv(compat.ADMIN_KEY_ENV, "operator-key")
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_DASHBOARD_SESSION_KEY", "session-key")

    class _Beta:
        def replace_credential(self, value):
            assert value["access_token"] == "sensitive"
            return {
                "state": "active",
                "generation_ready": True,
                "cookie_count": 0,
                "credential_persistence": {"source": "encrypted_external_postgres", "restart_durable": True},
            }

    monkeypatch.setattr(compat.M365BearerBeta, "from_directory", lambda: _Beta())

    async def call():
        transport = httpx.ASGITransport(app=compat.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            await client.post("/dashboard/login", json={"admin_key": "operator-key"})
            return await client.post(
                "/dashboard/api/credentials/import",
                json={"credential": {"access_token": "sensitive"}},
            )

    response = asyncio.run(call())

    assert response.status_code == 200
    assert response.json()["secrets_returned"] is False
    assert "sensitive" not in response.text
