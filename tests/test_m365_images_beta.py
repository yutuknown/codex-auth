import json
import time

import pytest

from beta.m365_bearer import (
    BetaConfigurationError,
    BetaCredential,
    BetaRoute,
    BetaUpstreamError,
)
from beta.m365_images import M365SubstrateImageUploader


class FakeResponse:
    status_code = 200

    def __init__(self, conversation_id="conversation"):
        self.conversation_id = conversation_id

    def json(self):
        return {
            "fileName": "proof.jpeg",
            "fileSize": 4,
            "fileType": ".jpeg",
            "docId": "opaque-image-document",
            "conversationId": self.conversation_id,
            "result": {"value": "Success", "message": "Success"},
        }

    def close(self):
        pass


class FakeSession:
    def __init__(self, response=None):
        self.cookies = []
        self.response = response or FakeResponse()
        self.request = None
        self.closed = False

    def post(self, endpoint, headers, multipart, timeout):
        self.request = {
            "endpoint": endpoint,
            "headers": headers,
            "multipart": multipart,
            "timeout": timeout,
        }
        conversation = next(
            part
            for part in multipart.parts
            if part["name"] == "conversationId"
        )
        self.response.conversation_id = conversation["data"].decode()
        return self.response

    def close(self):
        self.closed = True


def credential():
    return BetaCredential.from_raw(
        {
            "scope": "https://substrate.office.com/sydney/v2/.default",
            "access_token": "image-access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
        },
        captured_at=time.time(),
    )


def route():
    return BetaRoute.from_raw(
        {
            "identity": "user@example.test",
            "oauth": {
                "token_endpoint": (
                    "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
                ),
                "query": {},
                "form": {"client_id": "client"},
            },
        }
    )


class FakeMultipart:
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    def close(self):
        self.closed = True


def test_substrate_image_upload_uses_bearer_without_cookies():
    session = FakeSession()
    multipart = None

    def multipart_factory(parts):
        nonlocal multipart
        multipart = FakeMultipart(parts)
        return multipart

    uploader = M365SubstrateImageUploader(
        credential(),
        route(),
        session_factory=lambda: session,
        multipart_factory=multipart_factory,
    )

    attachment = uploader.upload_bytes(
        name="proof.jpeg",
        content=b"jpeg",
        mime_type="image/jpeg",
        conversation_id="conversation",
    )

    assert session.cookies == []
    assert session.closed is True
    assert multipart.closed is True
    assert multipart.parts == [
        {"name": "scenario", "data": b"UploadImage"},
        {"name": "conversationId", "data": b"conversation"},
        {
            "name": "FileBase64",
            "data": b"data:image/jpeg;base64,anBlZw==",
        },
        {"name": "optionsSets", "data": b"cwcgptvsan"},
        {
            "name": "optionsSets",
            "data": b"flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
        },
        {"name": "optionsSets", "data": b"gptvnorm2048"},
    ]
    assert session.request["headers"]["X-Scenario"] == (
        "OfficeWebPaidConsumerCopilot"
    )
    assert attachment.conversation_id == "conversation"
    assert attachment.message_annotation() == {
        "id": "opaque-image-document",
        "messageAnnotationType": "ImageFile",
        "text": "proof.jpeg",
        "messageAnnotationMetadata": {
            "@type": "File",
            "annotationType": "File",
            "fileName": "proof.jpeg",
            "fileType": "jpeg",
        },
    }
    assert "image-access-secret" not in repr(attachment)
    assert "image-access-secret" not in json.dumps(
        attachment.message_annotation()
    )


def test_substrate_image_upload_rejects_non_image_mime_type():
    uploader = M365SubstrateImageUploader(
        credential(),
        route(),
        session_factory=FakeSession,
    )

    with pytest.raises(BetaConfigurationError, match="unsupported"):
        uploader.upload_bytes(
            name="proof.txt",
            content=b"text",
            mime_type="text/plain",
        )


def test_substrate_image_upload_requires_matching_conversation():
    response = FakeResponse("wrong")

    class MismatchSession(FakeSession):
        def post(self, endpoint, headers, multipart, timeout):
            self.request = {
                "endpoint": endpoint,
                "headers": headers,
                "multipart": multipart,
                "timeout": timeout,
            }
            return response

    uploader = M365SubstrateImageUploader(
        credential(),
        route(),
        session_factory=MismatchSession,
        multipart_factory=FakeMultipart,
    )

    with pytest.raises(
        BetaUpstreamError,
        match="substrate_image_upload_missing_identity",
    ):
        uploader.upload_bytes(
            name="proof.png",
            content=b"png",
            mime_type="image/png",
            conversation_id="expected",
        )
