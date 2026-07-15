import os
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinchana_inst import main
from pinchana_inst.scraper import (
    InstagramGraphScraper,
    RateLimitError,
    RestrictedMediaError,
)


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
    clear_calls = 0

    async def fake_extract_media(_shortcode):
        nonlocal attempts
        attempts += 1
        raise RateLimitError("HTTP 429")

    async def fake_rotate_ip():
        nonlocal rotations
        rotations += 1

    def fake_clear_bootstrap_cache():
        nonlocal clear_calls
        clear_calls += 1

    monkeypatch.setenv("VPN_ENABLED", "1")
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.scraper, "clear_bootstrap_cache", fake_clear_bootstrap_cache)
    monkeypatch.setattr(main.gluetun, "rotate_ip", fake_rotate_ip)
    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(SimpleNamespace(url="https://www.instagram.com/p/ABC123/"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "rate_limited"
    assert attempts == 2
    assert rotations == 1
    assert clear_calls == 1


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


@pytest.mark.asyncio
async def test_graphql_execution_error_is_an_anonymous_miss_not_a_rate_limit(scraper):
    payload = {
        "errors": [{"message": "execution error", "severity": "CRITICAL"}],
        "data": None,
        "status": "ok",
    }

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class Session:
        async def post(self, *_args, **_kwargs):
            return Response()

    result = await scraper._extract_polaris_logged_out(
        Session(),
        "ABC123",
        {"csrf_token": "csrf", "lsd_token": "lsd"},
    )

    assert result is None


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
