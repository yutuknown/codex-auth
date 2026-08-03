"""Bounded public-HTTPS attachment retrieval for the local M365 beta."""

from __future__ import annotations

import ipaddress
import mimetypes
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from curl_cffi.requests import Session

from beta.m365_bearer import BetaConfigurationError, BetaUpstreamError, USER_AGENT

MAX_REMOTE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3


@dataclass(frozen=True)
class RemoteAttachment:
    name: str
    content: bytes
    mime_type: str


def _public_addresses(hostname: str) -> list[str]:
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BetaConfigurationError("remote attachment host did not resolve") from exc
    addresses = sorted({str(record[4][0]) for record in records})
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise BetaConfigurationError(
            "remote attachment host must resolve only to public addresses"
        )
    return addresses


class RemoteAttachmentFetcher:
    """Download a small public HTTPS object without retaining cookies."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = lambda: Session(
            impersonate="chrome"
        ),
        resolver: Callable[[str], list[str]] = _public_addresses,
    ) -> None:
        self.session_factory = session_factory
        self.resolver = resolver

    @staticmethod
    def _validated_url(value: str) -> urllib.parse.SplitResult:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise BetaConfigurationError(
                "remote attachments require a public HTTPS URL"
            )
        return parsed

    def fetch(self, url: str, *, name: str = "") -> RemoteAttachment:
        current = url
        session = self.session_factory()
        if list(session.cookies):
            session.close()
            raise BetaUpstreamError("remote_attachment_cookie_free_session_failed")
        try:
            for redirect in range(MAX_REDIRECTS + 1):
                parsed = self._validated_url(current)
                self.resolver(str(parsed.hostname))
                response = session.get(
                    current,
                    headers={
                        "Accept": "*/*",
                        "User-Agent": USER_AGENT,
                    },
                    allow_redirects=False,
                    discard_cookies=True,
                    stream=True,
                    timeout=30,
                )
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect == MAX_REDIRECTS:
                            raise BetaUpstreamError(
                                "remote_attachment_too_many_redirects"
                            )
                        location = str(response.headers.get("location") or "")
                        if not location:
                            raise BetaUpstreamError(
                                "remote_attachment_redirect_missing_location"
                            )
                        current = urllib.parse.urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise BetaUpstreamError(
                            f"remote_attachment_http_{response.status_code}"
                        )
                    raw_length = str(response.headers.get("content-length") or "")
                    if raw_length.isdigit() and int(raw_length) > MAX_REMOTE_BYTES:
                        raise BetaConfigurationError(
                            "remote attachment exceeds the 20 MB limit"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_content():
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_REMOTE_BYTES:
                            raise BetaConfigurationError(
                                "remote attachment exceeds the 20 MB limit"
                            )
                        chunks.append(bytes(chunk))
                    if not chunks:
                        raise BetaConfigurationError(
                            "remote attachment response was empty"
                        )
                    mime_type = str(
                        response.headers.get("content-type")
                        or "application/octet-stream"
                    ).split(";", 1)[0].strip().lower()
                    suffix = mimetypes.guess_extension(mime_type) or ".bin"
                    filename = (
                        Path(name).name
                        or Path(urllib.parse.unquote(parsed.path)).name
                        or f"remote-attachment{suffix}"
                    )
                    return RemoteAttachment(
                        name=filename[:128],
                        content=b"".join(chunks),
                        mime_type=mime_type,
                    )
                finally:
                    response.close()
        finally:
            session.close()
        raise BetaUpstreamError("remote_attachment_failed")
