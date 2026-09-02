import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from pinchana_inst import main
from pinchana_inst.scraper import (
    AnonymousMediaUnavailableError,
    InstagramGraphScraper,
    MediaNotFoundError,
    RateLimitError,
    RestrictedMediaError,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "instagram"


def fixture_text(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text()


class FakeResponse:
    def __init__(self, *, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeSession:
    def __init__(self, page_html: str, *, page_status=200, graphql_payload=None, bundles=None):
        self.page_html = page_html
        self.page_status = page_status
        self.graphql_payload = graphql_payload or {"data": {"xig_polaris_media": None}}
        self.bundles = bundles or {}
        self.cookies = {}
        self.gets = []
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url in self.bundles:
            return FakeResponse(text=self.bundles[url])
        return FakeResponse(status_code=self.page_status, text=self.page_html)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(payload=self.graphql_payload)


def scraper_for_session(session: FakeSession) -> InstagramGraphScraper:
    return InstagramGraphScraper(session_factory=lambda **_kwargs: session)


TEST_URLS = [
    "https://www.instagram.com/p/DUlFguzjAwl/",
    "https://www.instagram.com/p/CuWXKUiMS-o/",
    "https://www.instagram.com/reels/DVqyGcuj7yA/",
    "https://www.instagram.com/p/DX_VVYRgOVz/",
]


def get_shortcode(url):
    match = re.search(r"(?:p|reels|reel|tv|share/v)/([^/?#&]+)", url)
    return match.group(1) if match else None


@pytest.fixture
def scraper():
    return InstagramGraphScraper()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main.storage, "base_path", tmp_path)
    return TestClient(main.app)


def test_serves_downloaded_media(client, tmp_path):
    media_path = tmp_path / "VID123" / "video.mp4"
    media_path.parent.mkdir()
    media_path.write_bytes(b"video-content")

    response = client.get("/media/instagram/VID123/video.mp4")

    assert response.status_code == 200
    assert response.content == b"video-content"
    assert response.headers["content-type"] == "video/mp4"


def test_serves_nested_carousel_media(client, tmp_path):
    media_path = tmp_path / "CAR123" / "carousel" / "1_video.mp4"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"carousel-content")

    response = client.get("/media/instagram/CAR123/carousel/1_video.mp4")

    assert response.status_code == 200
    assert response.content == b"carousel-content"


@pytest.mark.parametrize(
    "url",
    [
        "/media/twitter/VID123/video.mp4",
        "/media/instagram/VID123/missing.mp4",
    ],
)
def test_rejects_invalid_media_requests(client, url):
    response = client.get(url)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rejects_media_path_traversal():
    with pytest.raises(main.HTTPException) as exc_info:
        await main.serve_media("instagram", "VID123", "../secret")

    assert exc_info.value.status_code == 404


def test_cache_requires_current_version_and_nonempty_media(tmp_path, monkeypatch):
    monkeypatch.setattr(main.storage, "base_path", tmp_path)
    metadata = {
        "_cache_version": main.INSTAGRAM_CACHE_VERSION,
        "thumbnail_url": "/media/instagram/IMG123/thumbnail.jpg",
        "video_url": None,
        "carousel": None,
    }

    assert main._cached_media_ready({}) is False
    assert main._cached_media_ready(metadata) is False

    thumbnail = main.storage.thumbnail_path("IMG123")
    thumbnail.parent.mkdir(parents=True)
    thumbnail.write_bytes(b"")
    assert main._cached_media_ready(metadata) is False

    thumbnail.write_bytes(b"image")
    assert main._cached_media_ready(metadata) is True

    stale = {**metadata, "_cache_version": main.INSTAGRAM_CACHE_VERSION - 1}
    assert main._cached_media_ready(stale) is False


@pytest.mark.asyncio
async def test_required_download_failure_is_not_cached_and_keeps_retryable_code(
    tmp_path,
    monkeypatch,
):
    raw_media = {
        "__typename": "GraphImage",
        "shortcode": "IMG123",
        "edge_media_to_caption": {"edges": []},
        "owner": {"username": "author"},
        "display_url": "https://cdn.example/image.jpg",
        "is_video": False,
        "video_url": None,
    }

    async def fake_extract_media(_shortcode):
        return raw_media

    async def failed_download(_url, _destination):
        return False

    monkeypatch.setattr(main.storage, "base_path", tmp_path)
    monkeypatch.setattr(main.storage, "download", failed_download)
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.instagram.com/p/IMG123/")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "media_download_failed"
    assert main.storage.load_metadata("IMG123") is None


@pytest.mark.asyncio
async def test_successful_download_writes_current_cache_version(tmp_path, monkeypatch):
    raw = {
        "caption": "caption",
        "author": "author",
        "primary_media": {
            "media_type": "GraphImage",
            "display_url": "https://cdn.example/image.jpg",
            "video_url": None,
        },
        "carousel_children": None,
    }

    async def successful_download(_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"image")
        return True

    monkeypatch.setattr(main.storage, "base_path", tmp_path)
    monkeypatch.setattr(main.storage, "download", successful_download)

    response = await main._download_and_build_response("IMG123", raw)
    metadata = main.storage.load_metadata("IMG123")

    assert response.shortcode == "IMG123"
    assert metadata["_cache_version"] == main.INSTAGRAM_CACHE_VERSION
    assert main._cached_media_ready(metadata) is True


@pytest.mark.asyncio
async def test_process_scrape_request_fails_fast_when_vpn_disabled(monkeypatch):
    attempts = 0

    async def fake_extract_media(_shortcode):
        nonlocal attempts
        attempts += 1
        raise RateLimitError("blocked")

    monkeypatch.setenv("VPN_ENABLED", "0")
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(SimpleNamespace(url="https://www.instagram.com/p/ABC123/"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "rate_limited"
    assert attempts == 1


@pytest.mark.asyncio
async def test_process_scrape_request_rotates_once_and_retries_once(monkeypatch):
    attempts = 0
    rotations = 0

    async def fake_extract_media(_shortcode):
        nonlocal attempts
        attempts += 1
        raise RateLimitError("HTTP 429")

    async def fake_rotate_ip():
        nonlocal rotations
        rotations += 1

    monkeypatch.setenv("VPN_ENABLED", "1")
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.gluetun, "rotate_ip", fake_rotate_ip)
    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(SimpleNamespace(url="https://www.instagram.com/p/ABC123/"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "rate_limited"
    assert attempts == 2
    assert rotations == 1


@pytest.mark.asyncio
async def test_restricted_post_does_not_rotate_or_retry(monkeypatch):
    attempts = 0
    rotations = 0

    async def fake_extract_media(_shortcode):
        nonlocal attempts
        attempts += 1
        raise RestrictedMediaError("not accessible anonymously")

    async def fake_rotate_ip():
        nonlocal rotations
        rotations += 1

    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.gluetun, "rotate_ip", fake_rotate_ip)
    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(SimpleNamespace(url="https://www.instagram.com/p/ABC123/"))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "restricted_media"
    assert attempts == 1
    assert rotations == 0


@pytest.mark.asyncio
async def test_ambiguous_anonymous_miss_does_not_rotate_or_claim_restriction(monkeypatch):
    attempts = 0
    rotations = 0

    async def fake_extract_media(_shortcode):
        nonlocal attempts
        attempts += 1
        raise AnonymousMediaUnavailableError("no decisive route reason")

    async def fake_rotate_ip():
        nonlocal rotations
        rotations += 1

    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.gluetun, "rotate_ip", fake_rotate_ip)
    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(SimpleNamespace(url="https://www.instagram.com/p/ABC123/"))

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "anonymous_unavailable"
    assert attempts == 1
    assert rotations == 0


def test_shortcode_to_media_id_uses_instagram_base64_alphabet(scraper):
    assert scraper._shortcode_to_media_id("A") == "0"
    assert scraper._shortcode_to_media_id("B") == "1"
    assert scraper._shortcode_to_media_id("-") == "62"
    assert scraper._shortcode_to_media_id("_") == "63"
    assert scraper._shortcode_to_media_id("BA") == "64"


def test_extract_lsd_token_from_known_bootstrap_shapes(scraper):
    assert scraper._extract_lsd_token('<script>["LSD",[],{"token":"token-a"}]</script>') == "token-a"
    assert scraper._extract_lsd_token('{"__bbox":{"require":[["LSD",[],{"token":"token-b"}]]}}') == "token-b"


def test_graphql_execution_error_classification(scraper):
    payload = {
        "errors": [{"message": "execution error", "severity": "CRITICAL"}],
        "data": None,
        "status": "ok",
    }

    assert scraper._is_graphql_execution_error(payload) is True
    assert scraper._is_graphql_execution_error({"errors": [{"message": "not found"}]}) is False


def test_empty_or_wrong_shortcode_media_wrapper_is_not_concrete_media(scraper):
    empty = {
        "data": {
            "xig_polaris_media": {
                "if_not_gated_logged_out": {"code": "ABC123"}
            }
        }
    }
    wrong = {
        "data": {
            "xig_polaris_media": {
                "if_not_gated_logged_out": {
                    "code": "OTHER123",
                    "image_versions2": {
                        "candidates": [{"url": "https://cdn.example/wrong.jpg"}]
                    },
                }
            }
        }
    }

    assert scraper._find_media_node(empty, "ABC123") is None
    assert scraper._find_media_node(wrong, "ABC123") is None


@pytest.mark.asyncio
async def test_graphql_execution_error_is_an_anonymous_miss_not_a_rate_limit(scraper):
    payload = {
        "errors": [{"message": "execution error", "severity": "CRITICAL"}],
        "data": None,
        "status": "ok",
    }

    session = FakeSession("", graphql_payload=payload)
    result = await scraper._execute_relay_operation(
        session,
        "ABC123",
        {
            "friendly_name": scraper.POLARIS_FRIENDLY_NAME,
            "doc_id": "28309390568695038",
            "variables": {"media_id": "123"},
        },
        '<script>["LSD",[],{"token":"lsd"}]</script>',
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "shortcode", "media_type", "child_count"),
    [
        ("image-page.html", "IMG123", "GraphImage", 0),
        ("reel-page.html", "VID123", "GraphVideo", 0),
        ("carousel-page.html", "CAR123", "GraphSidecar", 2),
    ],
)
async def test_initial_html_is_primary_for_public_media(
    fixture_name, shortcode, media_type, child_count
):
    session = FakeSession(fixture_text(fixture_name))
    scraper = scraper_for_session(session)

    raw = await scraper.extract_media(shortcode)

    assert raw["shortcode"] == shortcode
    assert raw["__typename"] == media_type
    edges = (raw.get("edge_sidecar_to_children") or {}).get("edges", [])
    assert len(edges) == child_count
    assert len(session.gets) == 1
    assert session.posts == []
    navigation_headers = session.gets[0][1]["headers"]
    assert navigation_headers["Sec-Fetch-Dest"] == "document"
    assert navigation_headers["Sec-Fetch-Mode"] == "navigate"


@pytest.mark.asyncio
async def test_internal_http_200_404_is_not_found():
    session = FakeSession(fixture_text("not-found-page.html"))
    scraper = scraper_for_session(session)

    with pytest.raises(MediaNotFoundError):
        await scraper.extract_media("MISSING123")

    assert session.posts == []


@pytest.mark.asyncio
async def test_explicit_age_gate_is_restricted_with_details():
    session = FakeSession(fixture_text("age-restricted-page.html"))
    scraper = scraper_for_session(session)

    with pytest.raises(RestrictedMediaError, match=r"reason=MA, age=16"):
        await scraper.extract_media("AGE123")

    assert session.posts == []


@pytest.mark.asyncio
async def test_html_preloader_drives_current_relay_query():
    payload = {
        "data": {
            "xig_polaris_media": {
                "if_not_gated_logged_out": {
                    "code": "PRE123",
                    "user": {"username": "relay_author"},
                    "image_versions2": {
                        "candidates": [{"url": "https://cdn.example/preloader.jpg"}]
                    },
                }
            }
        }
    }
    session = FakeSession(fixture_text("preloader-only-page.html"), graphql_payload=payload)
    scraper = scraper_for_session(session)

    raw = await scraper.extract_media("PRE123")

    assert raw["shortcode"] == "PRE123"
    assert len(session.gets) == 1
    assert len(session.posts) == 1
    body = parse_qs(session.posts[0][1]["data"])
    assert body["doc_id"] == ["28309390568695038"]
    assert json.loads(body["variables"][0]) == {"media_id": "16392609207"}
    assert body["lsd"] == ["fixture-lsd"]
    assert "x-ig-app-id" not in {key.lower() for key in session.posts[0][1]["headers"]}


@pytest.mark.asyncio
async def test_bundle_operation_is_secondary_self_update_fallback():
    bundle_url = "https://static.cdninstagram.com/rsrc.php/v4/fixture-operation.js"
    payload = {
        "data": {
            "xig_polaris_media": {
                "if_not_gated_logged_out": {
                    "code": "BUNDLE123",
                    "image_versions2": {
                        "candidates": [{"url": "https://cdn.example/bundle.jpg"}]
                    },
                }
            }
        }
    }
    session = FakeSession(
        fixture_text("bundle-only-page.html"),
        graphql_payload=payload,
        bundles={bundle_url: fixture_text("operation-bundle.js")},
    )
    scraper = scraper_for_session(session)

    raw = await scraper.extract_media("BUNDLE123")

    assert raw["shortcode"] == "BUNDLE123"
    assert [url for url, _kwargs in session.gets] == [
        "https://www.instagram.com/p/BUNDLE123/",
        bundle_url,
    ]
    body = parse_qs(session.posts[0][1]["data"])
    assert body["doc_id"] == ["28309390568695038"]
    assert json.loads(body["variables"][0]) == {
        "media_id": scraper._shortcode_to_media_id("BUNDLE123")
    }


@pytest.mark.asyncio
async def test_ambiguous_empty_surface_is_not_claimed_as_restricted():
    session = FakeSession("<!doctype html><main>Login to continue</main>")
    scraper = scraper_for_session(session)

    with pytest.raises(AnonymousMediaUnavailableError):
        await scraper.extract_media("UNKNOWN123")


def test_normalizes_logged_out_image_payload(scraper):
    payload = {
        "data": {
            "xig_polaris_media": {
                "if_not_gated_logged_out": {
                    "code": "ABC123",
                    "caption": {"text": "caption text"},
                    "user": {"username": "author"},
                    "image_versions2": {"candidates": [{"url": "https://cdn.example/image.jpg"}]},
                }
            }
        }
    }

    raw = scraper._find_media_node(payload)
    parsed = scraper.parse_response(raw)

    assert parsed == {
        "shortcode": "ABC123",
        "caption": "caption text",
        "author": "author",
        "primary_media": {
            "media_type": "GraphImage",
            "display_url": "https://cdn.example/image.jpg",
            "video_url": None,
        },
        "carousel_children": None,
    }


def test_normalizes_logged_out_video_payload(scraper):
    payload = {
        "xig_polaris_media": {
            "if_not_gated_logged_out": {
                "code": "VID123",
                "caption": "video caption",
                "user": {"username": "video_author"},
                "image_versions2": {"candidates": [{"url": "https://cdn.example/thumb.jpg"}]},
                "video_versions": [{"url": "https://cdn.example/video.mp4"}],
            }
        }
    }

    raw = scraper._find_media_node(payload)
    parsed = scraper.parse_response(raw)

    assert parsed["shortcode"] == "VID123"
    assert parsed["caption"] == "video caption"
    assert parsed["author"] == "video_author"
    assert parsed["primary_media"] == {
        "media_type": "GraphVideo",
        "display_url": "https://cdn.example/thumb.jpg",
        "video_url": "https://cdn.example/video.mp4",
    }


def test_normalizes_logged_out_carousel_payload(scraper):
    payload = {
        "data": {
            "xig_polaris_media": {
                "if_not_gated_logged_out": {
                    "code": "CAR123",
                    "caption": {"text": "carousel caption"},
                    "user": {"username": "carousel_author"},
                    "image_versions2": {"candidates": [{"url": "https://cdn.example/cover.jpg"}]},
                    "carousel_media": [
                        {"image_versions2": {"candidates": [{"url": "https://cdn.example/one.jpg"}]}},
                        {
                            "image_versions2": {"candidates": [{"url": "https://cdn.example/two.jpg"}]},
                            "video_versions": [{"url": "https://cdn.example/two.mp4"}],
                        },
                    ],
                }
            }
        }
    }

    raw = scraper._find_media_node(payload)
    parsed = scraper.parse_response(raw)

    assert parsed["primary_media"]["media_type"] == "GraphSidecar"
    assert parsed["carousel_children"] == [
        {
            "media_type": "GraphImage",
            "display_url": "https://cdn.example/one.jpg",
            "video_url": None,
        },
        {
            "media_type": "GraphVideo",
            "display_url": "https://cdn.example/two.jpg",
            "video_url": "https://cdn.example/two.mp4",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("PINCHANA_INST_LIVE") != "1", reason="live Instagram test is opt-in")
@pytest.mark.parametrize("url", TEST_URLS)
async def test_graph_scraper_live(scraper, url):
    shortcode = get_shortcode(url)
    assert shortcode is not None

    raw_data = await scraper.extract_media(shortcode)
    result = scraper.parse_response(raw_data)

    assert result["shortcode"] == shortcode
    assert result.get("author")
    assert result["primary_media"]["display_url"].startswith("https://")
