"""Zero-cookie Substrate image upload stage for the local M365 beta."""

from __future__ import annotations

import base64
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from curl_cffi import CurlMime
from curl_cffi.requests import Session

from beta.m365_bearer import (
    USER_AGENT,
    BetaConfigurationError,
    BetaCredential,
    BetaRoute,
    BetaUpstreamError,
    M365Attachment,
    M365BearerBeta,
    _id_token_claims,
    _route_from_credential,
)

UPLOAD_ENDPOINT = "https://substrate.office.com/m365Copilot/UploadFile"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


class M365SubstrateImageUploader:
    """Upload one image with the Sydney bearer and no browser cookies."""

    def __init__(
        self,
        credential: BetaCredential,
        route: BetaRoute,
        *,
        session_factory: Callable[[], Any] = lambda: Session(
            impersonate="chrome"
        ),
        multipart_factory: Callable[[list[dict[str, Any]]], Any] = (
            CurlMime.from_list
        ),
    ) -> None:
        self.credential = credential
        self.route = route
        self.session_factory = session_factory
        self.multipart_factory = multipart_factory

    @classmethod
    def from_directory(
        cls,
        directory: Path | None = None,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> "M365SubstrateImageUploader":
        raw = M365BearerBeta.from_directory(directory).credential.raw
        return cls(
            BetaCredential.from_raw(raw),
            BetaRoute.from_raw(_route_from_credential(raw)),
            session_factory=session_factory
            or (lambda: Session(impersonate="chrome")),
        )

    def _session(self) -> Any:
        session = self.session_factory()
        if list(session.cookies):
            session.close()
            raise BetaUpstreamError("image_cookie_free_session_failed")
        return session

    def upload_bytes(
        self,
        *,
        name: str,
        content: bytes,
        mime_type: str,
        conversation_id: str | None = None,
    ) -> M365Attachment:
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise BetaConfigurationError(
                f"unsupported M365 image MIME type: {mime_type}"
            )
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise BetaConfigurationError(
                "image source must be between 1 byte and 20 MB"
            )
        safe_name = Path(name).name[:128]
        if not safe_name:
            raise BetaConfigurationError("image source requires a filename")
        if self.credential.expires_at <= time.time():
            raise BetaConfigurationError("M365 image bearer is expired")

        active_conversation_id = conversation_id or str(uuid.uuid4())
        session = self._session()
        data_url = (
            f"data:{mime_type};base64,"
            f"{base64.b64encode(content).decode('ascii')}"
        )
        multipart = self.multipart_factory(
            [
                {"name": "scenario", "data": b"UploadImage"},
                {
                    "name": "conversationId",
                    "data": active_conversation_id.encode(),
                },
                {
                    "name": "FileBase64",
                    "data": data_url.encode("ascii"),
                },
                {
                    "name": "optionsSets",
                    "data": b"cwcgptvsan",
                },
                {
                    "name": "optionsSets",
                    "data": (
                        b"flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch"
                    ),
                },
                {
                    "name": "optionsSets",
                    "data": b"gptvnorm2048",
                },
            ]
        )
        try:
            claims = _id_token_claims(self.credential.raw)
        except BetaConfigurationError:
            claims = {}
        oid = str(claims.get("oid") or "")
        tid = str(claims.get("tid") or "")
        anchor_mailbox = (
            f"Oid:{oid}@{tid}"
            if oid and tid
            else f"MSA:{self.route.identity}"
        )
        try:
            response = session.post(
                UPLOAD_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.credential.access_token}",
                    "Accept": "application/json",
                    "Origin": "https://m365.cloud.microsoft",
                    "Referer": "https://m365.cloud.microsoft/",
                    "User-Agent": USER_AGENT,
                    "X-AnchorMailbox": anchor_mailbox,
                    "X-Scenario": "OfficeWebPaidConsumerCopilot",
                    "X-Variants": "feature.EnableImageSupportInUploadFile",
                },
                multipart=multipart,
                timeout=60,
            )
            try:
                if response.status_code != 200:
                    error_code = ""
                    try:
                        error_payload = response.json()
                    except (TypeError, ValueError):
                        error_payload = {}
                    if isinstance(error_payload, dict):
                        error = error_payload.get("error")
                        raw_code = (
                            error.get("code")
                            if isinstance(error, dict)
                            else error_payload.get("code")
                        )
                        error_code = re.sub(
                            r"[^a-z0-9_-]+",
                            "_",
                            str(raw_code or "").lower(),
                        ).strip("_")[:64]
                    suffix = f"_{error_code}" if error_code else ""
                    raise BetaUpstreamError(
                        f"substrate_image_upload_http_{response.status_code}"
                        f"{suffix}"
                    )
                try:
                    uploaded = response.json()
                except (TypeError, ValueError) as exc:
                    raise BetaUpstreamError(
                        "substrate_image_upload_invalid_json"
                    ) from exc
            finally:
                response.close()
        finally:
            multipart.close()
            session.close()

        if not isinstance(uploaded, dict):
            raise BetaUpstreamError("substrate_image_upload_invalid_json")
        result = uploaded.get("result")
        if (
            not isinstance(result, dict)
            or str(result.get("value") or "").lower() != "success"
        ):
            raise BetaUpstreamError("substrate_image_upload_failed")
        doc_id = str(uploaded.get("docId") or "")
        returned_conversation_id = str(uploaded.get("conversationId") or "")
        if not doc_id or returned_conversation_id != active_conversation_id:
            raise BetaUpstreamError("substrate_image_upload_missing_identity")

        return M365Attachment(
            annotation_id=doc_id,
            name=str(uploaded.get("fileName") or safe_name),
            mime_type=mime_type,
            annotation_type="ImageFile",
            conversation_id=active_conversation_id,
        )
