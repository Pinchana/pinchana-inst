import os
import re

import pytest

from pinchana_inst.scraper import InstagramGraphScraper


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
