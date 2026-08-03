from beta.m365_artifacts import M365ArtifactStore


class FakeRemote:
    def __init__(self, content=b"image-bytes", mime_type="image/png"):
        self.content = content
        self.mime_type = mime_type
        self.name = "generated.png"


class FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.urls = []

    def fetch(self, url, *, name=""):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.result


def test_artifact_store_returns_base64_only_for_verified_image_bytes():
    store = M365ArtifactStore(ttl_seconds=60)
    descriptor = store.put_image(b"png-bytes", "image/png", metadata={"title": "Proof", "url": "https://secret"})

    assert descriptor["availability"] == "embedded"
    assert "url" not in str(descriptor)
    block = store.image_block(descriptor["id"])
    assert block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "cG5nLWJ5dGVz"},
    }


def test_artifact_store_expires_and_never_makes_url_available():
    store = M365ArtifactStore(ttl_seconds=0)
    descriptor = store.put_image(b"x", "image/png")
    assert store.image_block(descriptor["id"]) is None
    assert M365ArtifactStore.unretrievable("image", {"url": "https://private"})["availability"] == "unretrievable"


def test_generated_card_resolution_embeds_only_verified_image_bytes():
    store = M365ArtifactStore(ttl_seconds=60)
    fetcher = FakeFetcher(FakeRemote())
    descriptors = store.resolve_generated_cards(
        [{"body": [{"images": [{"url": "https://cdn.example/image.png?secret"}]}]}],
        fetcher=fetcher,
    )
    assert fetcher.urls == ["https://cdn.example/image.png?secret"]
    assert descriptors[0]["availability"] == "embedded"
    assert "cdn.example" not in str(descriptors)
    assert store.image_block(descriptors[0]["id"])["source"]["data"] == "aW1hZ2UtYnl0ZXM="


def test_generated_card_resolution_redacts_failed_or_non_image_fetches():
    store = M365ArtifactStore(ttl_seconds=60)
    failed = store.resolve_generated_cards(
        [{"images": [{"url": "https://cdn.example/image.png"}]}],
        fetcher=FakeFetcher(error=RuntimeError("protected url")),
    )
    non_image = store.resolve_generated_cards(
        [{"image": {"url": "https://cdn.example/file"}}],
        fetcher=FakeFetcher(FakeRemote(mime_type="text/plain")),
    )
    assert failed[0]["availability"] == "unretrievable"
    assert non_image[0]["availability"] == "unretrievable"
    assert failed[0]["metadata"]["phase"] == "RuntimeError"
    assert "url" not in str(failed + non_image).lower()


def test_generated_reference_urls_are_resolved_without_returning_them():
    store = M365ArtifactStore(ttl_seconds=60)
    fetcher = FakeFetcher(FakeRemote())
    descriptors = store.resolve_generated_cards(
        [{"ImageReferenceUrls": ["https://cdn.example/generated.png?opaque"]}],
        fetcher=fetcher,
    )
    assert fetcher.urls == ["https://cdn.example/generated.png?opaque"]
    assert descriptors[0]["availability"] == "embedded"
    assert "cdn.example" not in str(descriptors)
