import base64
import json
import os
import time
import urllib.parse

import pytest

from beta import m365_bearer
from beta.m365_bearer import (
    M365_GRAPH_REFRESH_SCOPE,
    BetaConfigurationError,
    DurabilityRequiredError,
    BetaCredential,
    BetaRoute,
    BetaUpstreamError,
    M365Attachment,
    M365BearerBeta,
    M365StreamAssembler,
    apply_captured_refresh_form,
)
from codex_auth.providers.microsoft365 import RECORD_SEPARATOR


def credential_raw(**overrides):
    return {
        "scope": "https://substrate.office.com/sydney/v2/.default",
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_in": 3600,
        **overrides,
    }


def route_raw(**overrides):
    return {
        "identity": "opaque-identity",
        "oauth": {
            "capture_complete": True,
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "query": {"client-request-id": "request"},
            "form": {"client_id": "client", "scope": "https://substrate.office.com/sydney/v2/.default"},
        },
        **overrides,
    }


def configured_raw(**overrides):
    return credential_raw(
        captured_at=time.time(),
        id_token=encoded_id_token(oid="object"),
        route=route_raw(),
        **overrides,
    )


def test_environment_credential_source_reports_volatile_rotation_without_state_file(monkeypatch):
    monkeypatch.setenv(
        m365_bearer.M365_AUTH_JSON_ENV,
        json.dumps(configured_raw()),
    )
    monkeypatch.delenv(m365_bearer.M365_STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(m365_bearer, "_RUNTIME_CREDENTIAL", None)

    beta = M365BearerBeta.from_directory()
    status = beta.status()

    assert status["credential_persistence"] == {
        "source": "environment",
        "rotation_persistence": "process_memory",
        "restart_durable": False,
    }
    assert "access-secret" not in json.dumps(status)
    assert "refresh-secret" not in json.dumps(status)


def test_environment_credential_rotation_uses_explicit_state_file(monkeypatch, tmp_path):
    state_file = tmp_path / "m365-state.json"
    monkeypatch.setenv(
        m365_bearer.M365_AUTH_JSON_ENV,
        json.dumps(configured_raw()),
    )
    monkeypatch.setenv(m365_bearer.M365_STATE_FILE_ENV, str(state_file))
    monkeypatch.setattr(m365_bearer, "_RUNTIME_CREDENTIAL", None)

    beta = M365BearerBeta.from_directory()
    rotated = {**beta.credential.raw, "access_token": "rotated-secret"}
    beta._persist_credential(rotated)

    assert json.loads(state_file.read_text(encoding="utf-8"))["access_token"] == (
        "rotated-secret"
    )
    assert beta.status()["credential_persistence"] == {
        "source": "environment",
        "rotation_persistence": "state_file",
        "restart_durable": True,
    }


def test_hosted_required_mode_fails_closed_without_database(monkeypatch):
    monkeypatch.setenv(m365_bearer.M365_AUTH_JSON_ENV, json.dumps(configured_raw()))
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_REQUIRE_DURABLE", "1")
    monkeypatch.delenv("CODEX_AUTH_M365_BETA_DATABASE_URL", raising=False)
    monkeypatch.delenv("CODEX_AUTH_M365_BETA_CREDENTIAL_KEY", raising=False)
    monkeypatch.setattr(m365_bearer, "_RUNTIME_CREDENTIAL", None)

    with pytest.raises(DurabilityRequiredError, match="durable_credential_store_required"):
        M365BearerBeta.from_directory()


def test_durable_bootstrap_is_one_time_and_does_not_fallback_to_environment(monkeypatch):
    class FakeStore:
        record = None
        version = None

        def __init__(self):
            pass

        def load(self):
            return (dict(self.record), self.version) if self.record is not None else (None, None)

        def save(self, value, expected_version=None):
            if expected_version is not None and expected_version != self.version:
                raise RuntimeError("credential_version_conflict")
            type(self).record = dict(value)
            type(self).version = (self.version or 0) + 1
            return self.version

        def backup_current(self, reason):
            return self.version

        @staticmethod
        def safe_status():
            return {"source": "encrypted_external_postgres", "restart_durable": True}

    import beta.m365_durable as durable
    monkeypatch.setattr(durable, "PostgresCredentialStore", FakeStore)
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_DATABASE_URL", "postgres://test")
    monkeypatch.setenv("CODEX_AUTH_M365_BETA_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv(m365_bearer.M365_AUTH_JSON_ENV, json.dumps(configured_raw(access_token="seed")))
    monkeypatch.setattr(m365_bearer, "_RUNTIME_CREDENTIAL", None)

    first = M365BearerBeta.from_directory()
    assert first.credential.access_token == "seed"
    monkeypatch.setenv(m365_bearer.M365_AUTH_JSON_ENV, json.dumps(configured_raw(access_token="stale")))
    monkeypatch.setattr(m365_bearer, "_RUNTIME_CREDENTIAL", None)
    second = M365BearerBeta.from_directory()
    assert second.credential.access_token == "seed"
    assert second.status()["credential_persistence"]["credential_version"] == 1


def encoded_id_token(**overrides):
    claims = {
        "tid": "tenant",
        "aud": "client",
        "preferred_username": "user@example.com",
        **overrides,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class FakeWebSocket:
    def __init__(self):
        update = {"type": 1, "target": "update", "arguments": [{"messages": [{"author": "bot", "text": "M365_BETA_OK"}]}]}
        self.received = [
            (b"{}\x1e", 1),
            ((json.dumps(update) + RECORD_SEPARATOR).encode(), 1),
            (b'{"type":3}\x1e', 1),
        ]
        self.sent = []
        self.closed = False

    def send(self, payload, flags):
        self.sent.append((payload, flags))

    def recv(self):
        return self.received.pop(0)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.cookies = []
        self.websocket = FakeWebSocket()
        self.endpoint = None
        self.headers = None
        self.closed = False

    def ws_connect(self, endpoint, headers, timeout):
        self.endpoint, self.headers = endpoint, headers
        return self.websocket

    def close(self):
        self.closed = True


class FailedConnectSession(FakeSession):
    def ws_connect(self, endpoint, headers, timeout):
        self.endpoint, self.headers = endpoint, headers
        raise RuntimeError("connect failed")


class UnauthorizedConnectError(RuntimeError):
    status_code = 401


class UnauthorizedConnectSession(FakeSession):
    def ws_connect(self, endpoint, headers, timeout):
        self.endpoint, self.headers = endpoint, headers
        raise UnauthorizedConnectError("unauthorized")


class GenerationFailWebSocket(FakeWebSocket):
    def __init__(self):
        self.received = [(b"{}\x1e", 1)]
        self.sent = []
        self.closed = False

    def recv(self):
        if self.received:
            return self.received.pop(0)
        raise RuntimeError("stream dropped after submit")


class GenerationFailSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.websocket = GenerationFailWebSocket()


class FakeRefreshResponse:
    status_code = 200

    def json(self):
        return {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 14400,
        }

    def close(self):
        pass


class FakeRefreshSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.request = None

    def post(self, endpoint, params, data, headers, timeout):
        self.request = {"endpoint": endpoint, "params": params, "data": data, "headers": headers, "timeout": timeout}
        return FakeRefreshResponse()


class RejectedRefreshResponse:
    status_code = 400

    def json(self):
        return {
            "error": "invalid_grant",
            "error_description": "AADSTS70000: The provided grant is invalid.",
            "error_codes": [70000],
        }

    def close(self):
        pass


class RejectedRefreshSession(FakeSession):
    def post(self, endpoint, params, data, headers, timeout):
        return RejectedRefreshResponse()


def test_raw_credential_derives_expiry_and_hides_secrets():
    credential = BetaCredential.from_raw(credential_raw(), captured_at=1_000)

    assert credential.expires_at == 4_600
    assert "access-secret" not in repr(credential)
    assert "refresh-secret" not in repr(credential)


def test_raw_credential_uses_id_token_issuance_when_captured_at_is_missing():
    raw = {
        **credential_raw(),
        "id_token": encoded_id_token(iat=2_000),
    }

    credential = BetaCredential.from_raw(raw)

    assert credential.expires_at == 5_600


@pytest.mark.parametrize("raw", [{}, {"identity": "identity"}, {"identity": "identity", "oauth": {}}])
def test_route_rejects_missing_capture_metadata(raw):
    with pytest.raises(BetaConfigurationError):
        BetaRoute.from_raw(raw)


def test_directory_derives_route_from_beta_credential_only(tmp_path):
    beta_directory = tmp_path / "beta"
    beta_directory.mkdir()
    (beta_directory / "ms365-auth.json").write_text(
        json.dumps(
            {
                **credential_raw(captured_at=time.time()),
                "id_token": encoded_id_token(),
            }
        ),
        encoding="utf-8",
    )

    beta = M365BearerBeta.from_directory(beta_directory, session_factory=FakeSession)

    assert beta.route.identity == "user@example.com"
    assert beta.route.token_endpoint == "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    assert beta.route.token_form["client_id"] == "client"
    assert (
        beta.route.token_form["redirect_uri"]
        == "brk-multihub://Outlook.office.com"
    )
    assert beta.route.token_form["x-client-VER"] == "5.9.0"
    assert beta.route.token_form["brk_client_id"] == m365_bearer.M365_BROKER_CLIENT_ID
    assert beta.route.token_form["brk_redirect_uri"] == m365_bearer.M365_REDIRECT_URI
    assert "x-client-current-telemetry" in beta.route.token_form
    assert "refresh_token" not in beta.route.token_form


def test_captured_refresh_form_import_writes_only_non_secret_metadata(tmp_path):
    beta_directory = tmp_path / "beta"
    beta_directory.mkdir()
    credential_path = beta_directory / "ms365-auth.json"
    credential_path.write_text(
        json.dumps(
            {
                **credential_raw(captured_at=time.time()),
                "id_token": encoded_id_token(oid="object"),
            }
        ),
        encoding="utf-8",
    )

    result = apply_captured_refresh_form(beta_directory)

    saved = json.loads(credential_path.read_text(encoding="utf-8"))
    oauth = saved["route"]["oauth"]
    assert result["capture_complete"] is True
    assert result["secrets_written"] is False
    assert oauth["capture_complete"] is True
    assert oauth["form"]["redirect_uri"] == "brk-multihub://Outlook.office.com"
    assert oauth["form"]["scope"] == (
        "https://substrate.office.com/sydney/v2/.default openid profile offline_access"
    )
    assert oauth["form"]["x-client-VER"] == "5.9.0"
    assert oauth["form"]["brk_client_id"] == m365_bearer.M365_BROKER_CLIENT_ID
    assert oauth["form"]["brk_redirect_uri"] == m365_bearer.M365_REDIRECT_URI
    assert oauth["form"]["X-AnchorMailbox"] == "Oid:object@tenant"
    assert "refresh_token" not in oauth["form"]
    assert saved["refresh_token"] == "refresh-secret"


def test_broker_refresh_response_root_substrate_scope_is_accepted():
    credential = BetaCredential.from_raw(
        credential_raw(scope="https://substrate.office.com/.default")
    )

    assert credential.expires_at > time.time()


def test_cookie_free_generation_uses_signalr_without_cookie_header(tmp_path):
    session = FakeSession()
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    route = BetaRoute.from_raw(route_raw())
    beta = M365BearerBeta(credential, route, tmp_path / "ms365-auth.json", session_factory=lambda: session)

    answer = beta.generate("Reply exactly with: M365_BETA_OK")

    assert answer == "M365_BETA_OK"
    assert session.cookies == []
    assert "Cookie" not in session.headers
    assert "access-secret" in session.endpoint
    assert "access-secret" not in str(beta.status())
    assert session.websocket.closed is True


def test_connection_retry_is_bounded_and_only_before_prompt_submission(tmp_path):
    sessions = [FailedConnectSession(), FakeSession()]

    def factory():
        return sessions.pop(0)

    beta = M365BearerBeta(
        BetaCredential.from_raw(credential_raw(captured_at=time.time())),
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=factory,
    )

    answer = beta.generate("Reply exactly with: M365_BETA_OK")

    assert answer == "M365_BETA_OK"
    assert beta.status()["last_connect_attempts"] == 2
    assert beta.status()["generation_replay_policy"] == "never_after_submit"


def test_connect_401_refreshes_once_before_prompt_submission(tmp_path):
    sessions = [UnauthorizedConnectSession(), FakeSession()]

    def factory():
        return sessions.pop(0)

    beta = M365BearerBeta(
        BetaCredential.from_raw(credential_raw(captured_at=time.time())),
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=factory,
    )
    refresh_calls = 0

    def refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return beta.status()

    beta.refresh = refresh

    answer = beta.generate("Reply exactly with: M365_BETA_OK")

    assert answer == "M365_BETA_OK"
    assert refresh_calls == 1
    assert beta.status()["pre_submit_refreshes"] == 1
    assert beta.status()["last_connect_failure"] == "signalr_connect_http_401"


def test_generation_is_not_replayed_after_invocation_is_submitted(tmp_path):
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return GenerationFailSession()

    beta = M365BearerBeta(
        BetaCredential.from_raw(credential_raw(captured_at=time.time())),
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=factory,
    )

    with pytest.raises(BetaUpstreamError, match="signalr_generation_failed"):
        beta.generate("Do not replay this")

    assert factory_calls == 1


def test_custom_catalog_tone_is_sent_in_signalr_invocation(tmp_path):
    session = FakeSession()
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=lambda: session,
    )

    beta.generate_stream(
        "test",
        lambda event: None,
        "gpt-5.6-think-deeper",
        "Gpt_5_6_Reasoning",
    )

    invocation = json.loads(session.websocket.sent[2][0].rstrip(RECORD_SEPARATOR))
    assert invocation["arguments"][0]["tone"] == "Gpt_5_6_Reasoning"


def test_attachment_binding_matches_captured_m365_message_annotation(tmp_path):
    session = FakeSession()
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=lambda: session,
    )
    attachment = M365Attachment(
        annotation_id="SPO_drive_item",
        url="https://my.microsoftpersonalcontent.com/file",
        name="proof.txt",
        mime_type="text/plain",
    )

    beta.generate_stream(
        "Read the attachment",
        lambda event: None,
        attachments=[attachment],
    )

    invocation = json.loads(session.websocket.sent[2][0].rstrip(RECORD_SEPARATOR))
    annotations = invocation["arguments"][0]["message"]["messageAnnotations"]
    assert annotations == [
        {
            "id": "SPO_drive_item",
            "messageAnnotationType": "File",
            "text": "proof.txt",
            "url": "https://my.microsoftpersonalcontent.com/file",
            "messageAnnotationMetadata": {"@type": "File", "fileType": "txt"},
        }
    ]


def test_image_attachment_reuses_upload_conversation_id(tmp_path):
    session = FakeSession()
    beta = M365BearerBeta(
        BetaCredential.from_raw(
            credential_raw(captured_at=time.time())
        ),
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=lambda: session,
    )
    attachment = M365Attachment(
        annotation_id="image-doc",
        name="proof.jpeg",
        mime_type="image/jpeg",
        annotation_type="ImageFile",
        conversation_id="image-conversation",
    )

    beta.generate_stream(
        "Describe the image",
        lambda event: None,
        attachments=[attachment],
    )

    assert "ConversationId=image-conversation" in session.endpoint
    endpoint_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(session.endpoint).query
    )
    variants = set(endpoint_query["variants"][0].split(","))
    assert "feature.EnableBase64DataInMessageAnnotations" in variants
    assert (
        "feature.EnableClientFileURLSupportForOfficeWebPaidCopilot"
        in variants
    )
    invocation = json.loads(
        session.websocket.sent[2][0].rstrip(RECORD_SEPARATOR)
    )
    annotation = invocation["arguments"][0]["message"][
        "messageAnnotations"
    ][0]
    assert annotation["messageAnnotationType"] == "ImageFile"
    assert annotation["messageAnnotationMetadata"] == {
        "@type": "File",
        "annotationType": "File",
        "fileName": "proof.jpeg",
        "fileType": "jpeg",
    }
    assert "url" not in annotation
    assert invocation["arguments"][0]["message"][
        "connectedFederatedConnections"
    ] == ["dummyId"]


def test_inspect_reports_only_safe_frame_schema(tmp_path):
    session = FakeSession()
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=lambda: session,
    )

    report = beta.inspect("Inspect this response")

    assert report["result"] == "passed"
    assert report["cookie_count"] == 0
    assert report["bot_text_update_count"] == 1
    assert report["response_characters"] == len("M365_BETA_OK")
    assert "M365_BETA_OK" not in json.dumps(report)
    assert "access-secret" not in json.dumps(report)


def test_normalized_event_mapping_covers_observed_m365_fields():
    frame = {
        "type": 1,
        "target": "update",
        "arguments": [
            {
                "messages": [
                    {
                        "messageId": "reasoning",
                        "author": "bot",
                        "messageType": "Progress",
                        "addToChainOfThought": True,
                        "text": "private progress",
                    },
                    {
                        "messageId": "result",
                        "author": "bot",
                        "text": "answer",
                        "searchQueries": ["query"],
                        "references": [{"title": "source"}],
                        "adaptiveCards": [{"body": [{"images": [{"url": "https://example.test/image"}]}]}],
                        "contentGenerationProgressList": [{"status": 1}],
                        "pluginInfo": {"id": "plugin"},
                        "suggestedResponses": [{"text": "next"}],
                    },
                    {
                        "messageId": "code",
                        "author": "bot",
                        "messageType": "GeneratedCode",
                        "hiddenText": "print(1)",
                    },
                ]
            }
        ],
    }

    events = M365BearerBeta._normalized_event_types(frame)

    assert {
        "reasoning_progress",
        "search_query",
        "citation",
        "generated_code",
        "image_progress",
        "adaptive_card",
        "image",
        "plugin",
        "suggestions",
        "text_snapshot",
    }.issubset(events)


def test_inspector_labels_cdn_reasoning_ui_taxonomy_without_promoting_text():
    frame = {
        "type": 1,
        "target": "update",
        "arguments": [
            {
                "messages": [
                    {
                        "messageId": "ui-thinking",
                        "author": "bot",
                        "copilotMessageType": "thinking",
                        "layout": "chain_of_thought_search",
                        "text": "UI-managed progress",
                    }
                ]
            }
        ],
    }

    events = M365BearerBeta._normalized_event_types(frame)

    assert "reasoning_ui_item" in events
    assert "reasoning_progress" not in events


def test_stream_assembler_tracks_text_and_reasoning_lanes_independently():
    assembler = M365StreamAssembler()
    first = [
        {
            "messageId": "reasoning",
            "author": "bot",
            "messageType": "Progress",
            "addToChainOfThought": True,
            "text": "Checking",
        },
        {
            "messageId": "answer",
            "author": "bot",
            "text": "The result",
        },
    ]
    second = [
        {
            "messageId": "reasoning",
            "author": "bot",
            "messageType": "Progress",
            "addToChainOfThought": True,
            "text": "Checking arithmetic",
        },
        {
            "messageId": "answer",
            "author": "bot",
            "text": "The result is 42.",
        },
    ]

    first_events = assembler.consume({"type": 1}, first, 100)
    second_events = assembler.consume({"type": 1}, second, 200)

    assert [event["delta"] for event in first_events] == ["Checking", "The result"]
    assert [event["delta"] for event in second_events] == [" arithmetic", " is 42."]
    assert all(event["operation"] == "append" for event in first_events + second_events)
    assert first_events[0]["lane"] != first_events[1]["lane"]


def test_stream_assembler_exposes_builtin_plugin_lifecycle_without_payload():
    assembler = M365StreamAssembler()
    message = {
        "messageType": "TriggerPlugin",
        "pluginInfo": {"secret": "must-not-leak"},
        "suggestedResponses": [{"text": "one"}, {"text": "two"}],
    }

    events = assembler.consume({}, [message], 25)

    assert {"type": "plugin", "count": 1, "elapsed_ms": 25} in events
    assert {"type": "suggestions", "count": 2, "elapsed_ms": 25} in events
    assert "must-not-leak" not in str(events)


def test_status_reports_expiring_soon_without_secret(tmp_path):
    credential = BetaCredential.from_raw(credential_raw(expires_in=1), captured_at=time.time())
    beta = M365BearerBeta(credential, BetaRoute.from_raw(route_raw()), tmp_path / "ms365-auth.json", session_factory=FakeSession)

    status = beta.status()

    assert status["state"] == "expiring_soon"
    assert "access-secret" not in str(status)
    assert "refresh-secret" not in str(status)


def test_refresh_rotates_the_local_pair_atomically(tmp_path):
    session = FakeRefreshSession()
    credential_path = tmp_path / "ms365-auth.json"
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(credential, BetaRoute.from_raw(route_raw()), credential_path, session_factory=lambda: session)

    status = beta.refresh()

    saved = json.loads(credential_path.read_text(encoding="utf-8"))
    assert session.cookies == []
    assert session.request["data"]["refresh_token"] == "refresh-secret"
    assert saved["access_token"] == "rotated-access"
    assert saved["refresh_token"] == "rotated-refresh"
    assert status["last_refresh_outcome"] == "succeeded"
    assert "rotated-access" not in str(status)
    assert "rotated-refresh" not in str(status)


def test_graph_bearer_is_acquired_from_same_refresh_bundle_without_replacing_sydney_access(tmp_path):
    session = FakeRefreshSession()
    credential_path = tmp_path / "ms365-auth.json"
    credential = BetaCredential.from_raw(
        credential_raw(captured_at=time.time())
    )
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        credential_path,
        session_factory=lambda: session,
    )

    status = beta.acquire_graph_credential()

    saved = json.loads(credential_path.read_text(encoding="utf-8"))
    assert session.request["data"]["scope"] == M365_GRAPH_REFRESH_SCOPE
    assert saved["access_token"] == "access-secret"
    assert saved["refresh_token"] == "rotated-refresh"
    assert saved["resources"]["graph"]["access_token"] == "rotated-access"
    assert saved["resources"]["graph"]["source"] == "broker_refresh"
    assert status["state"] == "active"
    assert status["cookie_count"] == 0
    assert "rotated-access" not in str(status)


def test_rejected_refresh_persists_safe_recovery_state(tmp_path):
    credential_path = tmp_path / "ms365-auth.json"
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        credential_path,
        session_factory=RejectedRefreshSession,
    )

    with pytest.raises(
        BetaUpstreamError,
        match="oauth_refresh_rejected:invalid_grant:AADSTS70000",
    ):
        beta.refresh()

    saved = json.loads(credential_path.read_text(encoding="utf-8"))
    status = beta.status()
    assert saved["refresh_last_outcome"] == "failed"
    assert saved["refresh_last_error_code"] == "invalid_grant:AADSTS70000"
    assert status["state"] == "refresh_failed"
    assert status["generation_ready"] is True
    assert status["refresh_available"] is True
    assert status["refresh_ready"] is False
    assert status["recovery_action"] == "capture_fresh_oauth_response"
    assert "access-secret" not in str(status)
    assert "refresh-secret" not in str(status)


def test_status_restores_known_refresh_failure_from_local_record(tmp_path):
    credential = BetaCredential.from_raw(
        credential_raw(
            captured_at=time.time(),
            access_expiry_estimated=True,
            refresh_last_at=1_234,
            refresh_last_outcome="failed",
            refresh_last_error_code="invalid_grant:AADSTS70000",
        )
    )
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=FakeSession,
    )

    status = beta.status()

    assert status["state"] == "refresh_failed"
    assert status["access_expiry_estimated"] is True
    assert status["last_refresh_at"] == 1_234
    assert status["refresh_ready"] is False
    assert status["last_refresh_error_code"] == "invalid_grant:AADSTS70000"


def test_fresh_import_ignores_stale_refresh_failure_without_timestamp(tmp_path):
    credential = BetaCredential.from_raw(
        credential_raw(
            captured_at=time.time(),
            refresh_last_outcome="failed",
            refresh_last_error_code="invalid_grant:AADSTS70000",
        )
    )
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=FakeSession,
    )

    status = beta.status()

    assert status["state"] == "active"
    assert status["refresh_ready"] is True
    assert status["last_refresh_outcome"] is None
    assert status["last_refresh_error_code"] is None


def test_refresh_is_gated_when_exact_form_payload_was_not_captured(tmp_path):
    raw = route_raw()
    raw["oauth"]["capture_complete"] = False
    beta = M365BearerBeta(
        BetaCredential.from_raw(credential_raw(captured_at=time.time())),
        BetaRoute.from_raw(raw),
        tmp_path / "ms365-auth.json",
        session_factory=FakeRefreshSession,
    )

    with pytest.raises(
        BetaConfigurationError,
        match="exact successful DevTools form payload",
    ):
        beta.refresh()

    status = beta.status()
    assert status["refresh_ready"] is False
    assert status["refresh_capture_state"] == "missing_form_payload"


def test_refresh_write_failure_preserves_the_active_pair(tmp_path, monkeypatch):
    credential = BetaCredential.from_raw(credential_raw(captured_at=time.time()))
    beta = M365BearerBeta(
        credential,
        BetaRoute.from_raw(route_raw()),
        tmp_path / "ms365-auth.json",
        session_factory=FakeRefreshSession,
    )
    monkeypatch.setattr(m365_bearer, "_atomic_write_json", lambda *_: (_ for _ in ()).throw(OSError("disk unavailable")))

    with pytest.raises(BetaUpstreamError, match="oauth_refresh_failed"):
        beta.refresh()

    assert beta.credential.access_token == "access-secret"
    assert beta.credential.refresh_token == "refresh-secret"
    assert beta.status()["last_refresh_outcome"] == "failed"


@pytest.mark.skipif(
    os.environ.get("CODEX_AUTH_M365_BETA_CONFIRM") != "1",
    reason="requires an explicitly confirmed fresh local M365 beta credential",
)
def test_live_cookie_free_beta_probe():
    beta = M365BearerBeta.from_directory()

    response = beta.generate("Reply exactly with: M365_BETA_OK")

    assert response
    assert beta.status()["cookie_count"] == 0
