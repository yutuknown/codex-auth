import pytest

from beta.m365_bearer import BetaConfigurationError
from beta.m365_remote import RemoteAttachmentFetcher


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def iter_content(self):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.cookies = []
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def test_remote_fetch_is_cookie_free_bounded_and_redirect_aware():
    redirect = FakeResponse(302, {"location": "https://cdn.example/pixel.png"})
    image = FakeResponse(
        200,
        {"content-type": "image/png", "content-length": "8"},
        [b"png-", b"data"],
    )
    session = FakeSession([redirect, image])
    resolved = []
    fetcher = RemoteAttachmentFetcher(
        session_factory=lambda: session,
        resolver=lambda host: resolved.append(host) or ["203.0.113.10"],
    )

    result = fetcher.fetch("https://example.test/start", name="pixel.png")

    assert result.name == "pixel.png"
    assert result.mime_type == "image/png"
    assert result.content == b"png-data"
    assert resolved == ["example.test", "cdn.example"]
    assert all(request[1]["discard_cookies"] is True for request in session.requests)
    assert session.closed is True


def test_remote_fetch_rejects_private_resolution_before_request():
    session = FakeSession([])
    fetcher = RemoteAttachmentFetcher(
        session_factory=lambda: session,
        resolver=lambda _: (_ for _ in ()).throw(
            BetaConfigurationError(
                "remote attachment host must resolve only to public addresses"
            )
        ),
    )

    with pytest.raises(BetaConfigurationError, match="public addresses"):
        fetcher.fetch("https://localhost/private")

    assert session.requests == []
