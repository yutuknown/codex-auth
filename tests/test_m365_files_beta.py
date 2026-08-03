import json
import time

import pytest

from beta.m365_bearer import BetaConfigurationError, BetaUpstreamError
from beta.m365_files import GraphCredential, M365GraphUploader


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload

    def close(self):
        pass


class FakeGraphSession:
    def __init__(self):
        self.cookies = []
        self.closed = False
        self.requests = []

    def get(self, url, headers, timeout):
        self.requests.append(("GET", url, headers))
        if "$select=id,name,webUrl,webDavUrl,sharepointIds" in url:
            return FakeResponse(
                200,
                {
                    "sharepointIds": {
                        "siteId": "site",
                        "webId": "web",
                        "listId": "list",
                    }
                },
            )
        return FakeResponse(200, {"extracted": True})

    def post(self, url, headers, json, timeout):
        self.requests.append(("POST", url, headers))
        return FakeResponse(
            200,
            {"uploadUrl": "https://upload.example.sharepoint.com/session"},
        )

    def put(self, url, data, headers, timeout):
        self.requests.append(("PUT", url, headers))
        return FakeResponse(
            201,
            {
                "id": "item",
                "parentReference": {"driveId": "drive"},
                "webUrl": "https://my.microsoftpersonalcontent.com/file",
            },
        )

    def close(self):
        self.closed = True


def test_graph_credential_is_loaded_only_from_nested_beta_record():
    credential = GraphCredential.from_beta_record(
        {
            "resources": {
                "graph": {
                    "access_token": "graph-secret",
                    "captured_at": time.time(),
                    "expires_in": 3600,
                }
            }
        }
    )

    assert credential.access_token == "graph-secret"
    assert "graph-secret" not in repr(credential)


def test_graph_credential_requires_nested_resource():
    with pytest.raises(BetaConfigurationError):
        GraphCredential.from_beta_record({})


def test_graph_upload_accepts_captured_onedrive_web_url_shape():
    assert M365GraphUploader._trusted_upload_url(
        "https://onedrive.live.com/?id=opaque"
    )


def test_graph_upload_pipeline_uses_zero_cookie_sessions(tmp_path):
    source = tmp_path / "example.txt"
    source.write_text("hello", encoding="utf-8")
    sessions = []

    def factory():
        session = FakeGraphSession()
        sessions.append(session)
        return session

    uploader = M365GraphUploader(
        GraphCredential("graph-secret", time.time() + 3600),
        session_factory=factory,
    )

    uploaded = uploader.upload(source)

    assert uploaded.annotation_id == "SPO_c2l0ZSx3ZWIsbGlzdA_item"
    assert uploaded.site_id == "site"
    assert uploaded.web_url == "https://my.microsoftpersonalcontent.com/file"
    assert uploaded.extraction_ready is True
    assert uploaded.safe_status()["chat_binding_ready"] is False
    assert uploaded.safe_status()["chat_binding_state"] == "unverified_upstream"
    assert uploaded.safe_status()["cookie_count"] == 0
    assert len(sessions) == 2
    assert all(session.cookies == [] for session in sessions)
    create_request = next(
        request
        for request in sessions[0].requests
        if request[0] == "POST"
    )
    assert (
        "/me/drive/special/copilotuploads:/example.txt:/createUploadSession"
        in create_request[1]
    )
    assert create_request[2]["KnownConsumerLocation"] == "true"
    assert create_request[2]["Origin"] == "https://m365.cloud.microsoft"
    assert create_request[2]["Referer"] == "https://m365.cloud.microsoft/"
    assert create_request[2]["SdkVersion"] == "graph-js/3.0.7 (featureUsage=7)"
    assert create_request[2]["Client-Request-Id"]
    upload_request = sessions[1].requests[0]
    assert upload_request[0] == "PUT"
    assert upload_request[2]["Content-Type"] == "application/octet-stream"
    assert upload_request[2]["Prefer"] == "ExtractTextOnCommit, pacToken=N"
    assert upload_request[2]["KnownConsumerLocation"] == "true"
    assert upload_request[2]["Origin"] == "https://m365.cloud.microsoft"
    assert upload_request[2]["Referer"] == "https://m365.cloud.microsoft/"
    assert "graph-secret" not in json.dumps(uploaded.safe_status())


def test_stage_attachment_returns_signalr_binding_without_exposing_url():
    sessions = []

    def factory():
        session = FakeGraphSession()
        sessions.append(session)
        return session

    uploader = M365GraphUploader(
        GraphCredential("graph-secret", time.time() + 3600),
        session_factory=factory,
    )

    attachment = uploader.stage_attachment(
        name="proof.txt",
        content=b"hello",
        mime_type="text/plain",
    )

    assert attachment.annotation_id == "SPO_c2l0ZSx3ZWIsbGlzdA_item"
    assert attachment.message_annotation()["messageAnnotationMetadata"] == {
        "@type": "File",
        "fileType": "txt",
    }
    assert "microsoftpersonalcontent.com" not in repr(attachment)


def test_graph_annotation_id_matches_captured_m365_client_algorithm():
    assert M365GraphUploader._spo_annotation_id(
        "item",
        {"siteId": "{site}", "webId": "web", "listId": "list"},
    ) == "SPO_c2l0ZSx3ZWIsbGlzdA_item"


def test_graph_annotation_requires_m365_sharepoint_identity():
    with pytest.raises(BetaUpstreamError, match="graph_item_missing_sharepoint_ids"):
        M365GraphUploader._spo_annotation_id("item", {"siteId": "site"})


def test_graph_upload_rejects_untrusted_upload_destination(tmp_path):
    source = tmp_path / "example.txt"
    source.write_text("hello", encoding="utf-8")

    class UntrustedSession(FakeGraphSession):
        def post(self, url, headers, json, timeout):
            return FakeResponse(200, {"uploadUrl": "https://attacker.example/upload"})

    uploader = M365GraphUploader(
        GraphCredential("graph-secret", time.time() + 3600),
        session_factory=UntrustedSession,
    )

    with pytest.raises(BetaUpstreamError, match="graph_upload_url_rejected"):
        uploader.upload(source)


def test_graph_upload_reports_only_http_phase_on_create_rejection(tmp_path):
    source = tmp_path / "example.txt"
    source.write_text("hello", encoding="utf-8")

    class RejectedSession(FakeGraphSession):
        def post(self, url, headers, json, timeout):
            return FakeResponse(401, {"error": {"message": "do not leak"}})

    uploader = M365GraphUploader(
        GraphCredential("graph-secret", time.time() + 3600),
        session_factory=RejectedSession,
    )
    with pytest.raises(BetaUpstreamError, match="graph_upload_session_http_401") as error:
        uploader.upload(source)
    assert "do not leak" not in str(error.value)
