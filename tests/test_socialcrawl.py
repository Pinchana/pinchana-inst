from types import SimpleNamespace

import httpx
import pytest

from pinchana_inst import main
from pinchana_inst.scraper import RestrictedMediaError
from pinchana_inst.socialcrawl import (
    SocialCrawlDisabledError,
    SocialCrawlError,
    SocialCrawlResolver,
)


def socialcrawl_payload(media_urls, *, thumbnail_url=None, duration_seconds=None):
    return {
        "success": True,
        "platform": "instagram",
        "endpoint": "/v1/instagram/post",
        "data": {
            "post": {
                "content": {
                    "text": "caption",
                    "media_urls": media_urls,
                    "thumbnail_url": thumbnail_url,
                    "duration_seconds": duration_seconds,
                },
                "author": {"username": "author"},
                "ext": {},
            }
        },
        "credits_used": 1,
        "credits_remaining": 99,
        "cached": False,
        "request_id": "req-test",
    }


@pytest.mark.asyncio
async def test_socialcrawl_requires_api_key(monkeypatch):
    monkeypatch.delenv("SOCIALCRAWL_API_KEY", raising=False)
    resolver = SocialCrawlResolver()

    with pytest.raises(SocialCrawlDisabledError):
        await resolver.resolve("https://www.instagram.com/p/AGE123/", "AGE123")


@pytest.mark.asyncio
async def test_socialcrawl_normalises_carousel(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "test-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        assert request.url.params["url"] == "https://www.instagram.com/p/AGE123/"
        return httpx.Response(
            200,
            json=socialcrawl_payload([
                "https://cdn.example/one.jpg?sig=1",
                "https://cdn.example/two.jpg?sig=2",
            ]),
        )

    resolver = SocialCrawlResolver(transport=httpx.MockTransport(handler))
    raw = await resolver.resolve("https://www.instagram.com/p/AGE123/", "AGE123")

    assert raw["shortcode"] == "AGE123"
    assert raw["caption"] == "caption"
    assert raw["author"] == "author"
    assert raw["primary_media"]["media_type"] == "GraphSidecar"
    assert len(raw["carousel_children"]) == 2
    assert raw["carousel_children"][0]["display_url"].startswith("https://cdn.example/one.jpg")


@pytest.mark.asyncio
async def test_socialcrawl_normalises_video(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "test-key")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=socialcrawl_payload(
                ["https://cdn.example/video.mp4?sig=1"],
                thumbnail_url="https://cdn.example/thumb.jpg?sig=2",
                duration_seconds=12.5,
            ),
        )

    resolver = SocialCrawlResolver(transport=httpx.MockTransport(handler))
    raw = await resolver.resolve("https://www.instagram.com/reel/VID123/", "VID123")

    assert raw["primary_media"] == {
        "media_type": "GraphVideo",
        "display_url": "https://cdn.example/thumb.jpg?sig=2",
        "video_url": "https://cdn.example/video.mp4?sig=1",
    }
    assert raw["carousel_children"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"success": False}),
        httpx.Response(200, json={"success": False}),
        httpx.Response(200, json=socialcrawl_payload([])),
        httpx.Response(200, content=b"not-json"),
    ],
)
async def test_socialcrawl_rejects_failed_responses(monkeypatch, response):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "test-key")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    resolver = SocialCrawlResolver(transport=httpx.MockTransport(handler))
    with pytest.raises(SocialCrawlError):
        await resolver.resolve("https://www.instagram.com/p/AGE123/", "AGE123")


@pytest.mark.asyncio
async def test_exact_age_restriction_uses_socialcrawl_once(monkeypatch):
    extraction_attempts = 0
    fallback_attempts = 0
    rotations = 0

    async def fake_extract_media(_shortcode):
        nonlocal extraction_attempts
        extraction_attempts += 1
        raise RestrictedMediaError(
            "Instagram explicitly restricted post AGE123 (reason=MA, age=16)."
        )

    async def fake_resolve(post_url, shortcode):
        nonlocal fallback_attempts
        fallback_attempts += 1
        assert post_url == "https://www.instagram.com/p/AGE123/"
        assert shortcode == "AGE123"
        return {
            "caption": "caption",
            "author": "author",
            "primary_media": {
                "media_type": "GraphImage",
                "display_url": "https://cdn.example/image.jpg",
                "video_url": None,
            },
            "carousel_children": None,
        }

    async def fake_download(_shortcode, raw):
        return raw

    async def fake_rotate_ip():
        nonlocal rotations
        rotations += 1

    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.socialcrawl, "resolve", fake_resolve)
    monkeypatch.setattr(main, "_download_and_build_response", fake_download)
    monkeypatch.setattr(main.gluetun, "rotate_ip", fake_rotate_ip)

    result = await main._process_scrape_request(
        SimpleNamespace(url="https://www.instagram.com/p/AGE123/")
    )

    assert result["author"] == "author"
    assert extraction_attempts == 1
    assert fallback_attempts == 1
    assert rotations == 0


@pytest.mark.asyncio
async def test_non_age_restriction_never_uses_socialcrawl(monkeypatch):
    fallback_attempts = 0

    async def fake_extract_media(_shortcode):
        raise RestrictedMediaError("Instagram explicitly restricted post ABC123 (reason=OTHER).")

    async def fake_resolve(_post_url, _shortcode):
        nonlocal fallback_attempts
        fallback_attempts += 1
        raise AssertionError("SocialCrawl must not be called for non-age restrictions")

    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.socialcrawl, "resolve", fake_resolve)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.instagram.com/p/ABC123/")
        )

    assert exc_info.value.status_code == 403
    assert fallback_attempts == 0
