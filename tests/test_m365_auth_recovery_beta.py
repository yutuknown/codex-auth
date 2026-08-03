import json

import pytest

from beta.m365_auth_recovery import import_authorization_response
from beta.m365_bearer import BetaConfigurationError


def test_recovery_import_preserves_route_and_never_returns_secrets(tmp_path):
    (tmp_path / "ms365-auth.json").write_text(json.dumps({"route": {"opaque": "kept"}, "access_token": "old"}))
    status = import_authorization_response(
        {"token_type": "Bearer", "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "scope": "https://substrate.office.com/sydney/v2/.default"},
        tmp_path,
    )
    saved = json.loads((tmp_path / "ms365-auth.json").read_text())
    assert saved["route"] == {"opaque": "kept"}
    assert saved["access_token"] == "new-access"
    assert "new-access" not in str(status)
    assert status["secrets_returned"] is False


def test_recovery_import_rejects_incomplete_response(tmp_path):
    (tmp_path / "ms365-auth.json").write_text("{}")
    with pytest.raises(BetaConfigurationError):
        import_authorization_response({"token_type": "Bearer"}, tmp_path)
