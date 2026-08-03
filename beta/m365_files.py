"""Zero-cookie Microsoft 365 Graph upload stages for the local beta."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPOSITORY_DIRECTORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIRECTORY))
SOURCE_DIRECTORY = REPOSITORY_DIRECTORY / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from curl_cffi.requests import Session

from beta.m365_bearer import (
    BETA_CONFIRM_ENV,
    BetaConfigurationError,
    BetaUpstreamError,
    M365Attachment,
    M365BearerBeta,
)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class GraphCredential:
    access_token: str = field(repr=False)
    expires_at: float | None

    @classmethod
    def from_beta_record(cls, raw: dict[str, Any]) -> "GraphCredential":
        resources = raw.get("resources")
        graph = resources.get("graph") if isinstance(resources, dict) else None
        if not isinstance(graph, dict):
            raise BetaConfigurationError(
                "beta auth requires resources.graph for file uploads"
            )
        access_token = str(graph.get("access_token") or "").strip()
        if not access_token:
            raise BetaConfigurationError(
                "beta auth resources.graph requires access_token"
            )
        expires_at = graph.get("expires_at")
        if expires_at is None and graph.get("expires_in") is not None:
            captured_at = float(graph.get("captured_at") or time.time())
            expires_at = captured_at + int(graph["expires_in"])
        normalized_expiry = float(expires_at) if expires_at is not None else None
        if normalized_expiry is not None and normalized_expiry <= time.time():
            raise BetaConfigurationError("beta Graph access token is expired")
        return cls(access_token=access_token, expires_at=normalized_expiry)


@dataclass(frozen=True)
class UploadedM365File:
    name: str
    mime_type: str
    size: int
    drive_id: str
    item_id: str
    site_id: str
    annotation_id: str
    web_url: str = field(repr=False)
    extraction_ready: bool

    def safe_status(self) -> dict[str, Any]:
        return {
            "result": "passed",
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
            "annotation_ready": bool(self.annotation_id),
            # An annotation can be constructed locally even when M365 has not
            # accepted it as a chat attachment.  Keep this truthful until an
            # authenticated end-to-end prompt proves the private binding.
            "chat_binding_ready": False,
            "chat_binding_state": "unverified_upstream",
            "extraction_ready": self.extraction_ready,
            "cookie_count": 0,
        }


class M365GraphUploader:
    def __init__(
        self,
        credential: GraphCredential,
        *,
        session_factory: Callable[[], Any] = lambda: Session(
            impersonate="chrome"
        ),
    ) -> None:
        self.credential = credential
        self.session_factory = session_factory

    @classmethod
    def from_directory(
        cls,
        directory: Path | None = None,
        *,
        session_factory: Callable[[], Any] | None = None,
        acquire_if_needed: bool = False,
    ) -> "M365GraphUploader":
        beta = M365BearerBeta.from_directory(directory)
        raw = beta.credential.raw
        if acquire_if_needed:
            try:
                GraphCredential.from_beta_record(raw)
            except BetaConfigurationError:
                if os.environ.get(BETA_CONFIRM_ENV) != "1":
                    raise BetaConfigurationError(
                        "automatic Graph acquisition requires "
                        f"{BETA_CONFIRM_ENV}=1"
                    )
                beta.acquire_graph_credential()
                raw = beta.credential.raw
        return cls(
            GraphCredential.from_beta_record(raw),
            session_factory=session_factory
            or (lambda: Session(impersonate="chrome")),
        )

    def _session(self) -> Any:
        session = self.session_factory()
        if list(session.cookies):
            session.close()
            raise BetaUpstreamError("graph_cookie_free_session_failed")
        return session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credential.access_token}",
            "Accept": "application/json",
        }

    def _create_session_headers(self) -> dict[str, str]:
        """Headers evidenced by the browser's Graph upload-session request."""

        return {
            **self._headers(),
            "Content-Type": "application/json",
            "KnownConsumerLocation": "true",
            "Origin": "https://m365.cloud.microsoft",
            "Referer": "https://m365.cloud.microsoft/",
            "SdkVersion": "graph-js/3.0.7 (featureUsage=7)",
            "Client-Request-Id": str(uuid.uuid4()),
        }

    @staticmethod
    def _json(response: Any, phase: str) -> dict[str, Any]:
        try:
            value = response.json()
        except (TypeError, ValueError) as exc:
            raise BetaUpstreamError(f"{phase}_invalid_json") from exc
        if not isinstance(value, dict):
            raise BetaUpstreamError(f"{phase}_invalid_json")
        return value

    @staticmethod
    def _trusted_upload_url(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            hostname == "onedrive.live.com"
            or hostname.endswith(".sharepoint.com")
            or hostname.endswith(".1drv.com")
            or hostname.endswith(".onedrive.com")
            or hostname.endswith(".onedrive.live.com")
            or hostname.endswith(".microsoft.com")
            or hostname.endswith(".microsoftpersonalcontent.com")
        )

    @staticmethod
    def _spo_annotation_id(item_id: str, sharepoint_ids: Any) -> str:
        """Match M365 Chat's current local-file SPO identity construction.

        The web client does *not* use Graph's ``driveId`` here.  It encodes
        the SharePoint site/web/list triple and appends the item id.
        """
        if not isinstance(sharepoint_ids, dict):
            raise BetaUpstreamError("graph_item_missing_sharepoint_ids")
        values = [
            str(sharepoint_ids.get(key) or "").replace("{", "").replace("}", "")
            for key in ("siteId", "webId", "listId")
        ]
        if not all(values):
            raise BetaUpstreamError("graph_item_missing_sharepoint_ids")
        encoded = base64.b64encode(",".join(values).encode("utf-8")).decode("ascii")
        return f"SPO_{encoded.rstrip('=')}_{item_id}"

    def upload_bytes(
        self,
        *,
        name: str,
        content: bytes,
        mime_type: str,
    ) -> UploadedM365File:
        size = len(content)
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise BetaConfigurationError(
                "upload source must be between 1 byte and 20 MB"
            )
        safe_name = Path(name).name[:128]
        if not safe_name:
            raise BetaConfigurationError("upload source requires a filename")

        session = self._session()
        upload_session = None
        try:
            encoded_name = urllib.parse.quote(safe_name, safe="")
            create_response = session.post(
                f"{GRAPH_ROOT}/me/drive/special/copilotuploads:/{encoded_name}:"
                "/createUploadSession",
                headers=self._create_session_headers(),
                json={
                    "item": {
                        "@microsoft.graph.conflictBehavior": "rename",
                        "name": safe_name,
                    }
                },
                timeout=30,
            )
            try:
                if create_response.status_code not in {200, 201}:
                    raise BetaUpstreamError(
                        f"graph_upload_session_http_{create_response.status_code}"
                    )
                created = self._json(create_response, "graph_upload_session")
            finally:
                create_response.close()
            upload_url = str(created.get("uploadUrl") or "")
            if not self._trusted_upload_url(upload_url):
                raise BetaUpstreamError("graph_upload_url_rejected")

            upload_session = self._session()
            upload_response = upload_session.put(
                upload_url,
                data=content,
                headers={
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                    "Content-Type": "application/octet-stream",
                    "Prefer": "ExtractTextOnCommit, pacToken=N",
                    "KnownConsumerLocation": "true",
                    "Origin": "https://m365.cloud.microsoft",
                    "Referer": "https://m365.cloud.microsoft/",
                    "Accept": "*/*",
                },
                timeout=60,
            )
            try:
                if upload_response.status_code not in {200, 201}:
                    raise BetaUpstreamError(
                        f"graph_blob_upload_http_{upload_response.status_code}"
                    )
                uploaded = self._json(upload_response, "graph_blob_upload")
            finally:
                upload_response.close()
            item_id = str(uploaded.get("id") or "")
            web_url = str(uploaded.get("webUrl") or "")
            parent_reference = uploaded.get("parentReference")
            drive_id = (
                str(parent_reference.get("driveId") or "")
                if isinstance(parent_reference, dict)
                else ""
            )
            if not item_id or not drive_id or not self._trusted_upload_url(web_url):
                raise BetaUpstreamError("graph_blob_upload_missing_identity")

            # Upload-session responses omit the SharePoint identity set that
            # M365 Chat uses for its File annotation.  Fetch it explicitly
            # before constructing the attachment; ``driveId`` is not a valid
            # replacement for the web client's SPO identity.
            item_response = session.get(
                f"{GRAPH_ROOT}/me/drive/items/{urllib.parse.quote(item_id, safe='')}"
                "?$select=id,name,webUrl,webDavUrl,sharepointIds",
                headers=self._headers(),
                timeout=30,
            )
            try:
                if item_response.status_code != 200:
                    raise BetaUpstreamError("graph_item_metadata_rejected")
                item_metadata = self._json(item_response, "graph_item_metadata")
            finally:
                item_response.close()
            sharepoint_ids = item_metadata.get("sharepointIds")
            annotation_id = self._spo_annotation_id(item_id, sharepoint_ids)
            site_id = (
                str(sharepoint_ids.get("siteId") or "")
                if isinstance(sharepoint_ids, dict)
                else ""
            )

            extraction_response = session.get(
                f"{GRAPH_ROOT}/me/drive/items/{urllib.parse.quote(item_id, safe='')}"
                "/content?format=extractedtextandmetadatav1",
                headers={**self._headers(), "Prefer": "apiversion=2.1"},
                timeout=60,
            )
            try:
                extraction_ready = extraction_response.status_code == 200
            finally:
                extraction_response.close()
            return UploadedM365File(
                name=safe_name,
                mime_type=mime_type,
                size=size,
                drive_id=drive_id,
                item_id=item_id,
                site_id=site_id,
                annotation_id=annotation_id,
                web_url=web_url,
                extraction_ready=extraction_ready,
            )
        finally:
            if upload_session is not None:
                upload_session.close()
            session.close()

    def upload(self, source: Path) -> UploadedM365File:
        path = source.resolve()
        if not path.is_file():
            raise BetaConfigurationError("upload source must be a local file")
        name = path.name[:128]
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self.upload_bytes(
            name=name,
            content=path.read_bytes(),
            mime_type=mime_type,
        )

    def stage_attachment(
        self,
        *,
        name: str,
        content: bytes,
        mime_type: str,
    ) -> M365Attachment:
        uploaded = self.upload_bytes(
            name=name,
            content=content,
            mime_type=mime_type,
        )
        return M365Attachment(
            annotation_id=uploaded.annotation_id,
            url=uploaded.web_url,
            name=uploaded.name,
            mime_type=uploaded.mime_type,
        )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Local-only, zero-cookie M365 Graph upload beta"
    )
    parser.add_argument("command", choices=("status", "upload"))
    parser.add_argument("path", nargs="?")
    arguments = parser.parse_args()
    try:
        uploader = M365GraphUploader.from_directory()
        if arguments.command == "status":
            print(
                json.dumps(
                    {
                        "state": "active",
                        "cookie_count": 0,
                        "graph_token_configured": True,
                    }
                )
            )
            return 0
        if os.environ.get(BETA_CONFIRM_ENV) != "1":
            raise BetaConfigurationError(
                f"set {BETA_CONFIRM_ENV}=1 before running a live upload"
            )
        if not arguments.path:
            raise BetaConfigurationError("upload requires a local file path")
        print(
            json.dumps(
                uploader.upload(Path(arguments.path)).safe_status(),
                sort_keys=True,
            )
        )
        return 0
    except BetaConfigurationError as exc:
        if arguments.command == "status":
            print(
                json.dumps(
                    {
                        "state": "not_configured",
                        "cookie_count": 0,
                        "graph_token_configured": False,
                    }
                )
            )
            return 0
        print(json.dumps({"result": "failed", "phase": str(exc)}))
        return 1
    except BetaUpstreamError as exc:
        print(json.dumps({"result": "failed", "phase": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
