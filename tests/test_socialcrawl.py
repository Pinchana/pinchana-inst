from types import SimpleNamespace

import httpx
import pytest

from pinchana_inst import main
from pinchana_inst.scraper import RestrictedMediaError
from pinchana_inst.socialcrawl import (
    SocialCrawlError,
    SocialCrawlNotConfiguredError,
    SocialCrawlResolver,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeClient:
    def __init__(self, response=None, error=None, calls=None, **_kwargs):
        self.response = response
        self.error = error
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def resolver_for(response=None, *, error=None, calls=None):
    return SocialCrawlResolver(
        client_factory=lambda **kwargs: FakeClient(
            response=response,
            error=error,
            calls=calls,
            **kwargs,
        )
    )


def socialcrawl_payload(media_urls, *, thumbnail_url=None):
    return {
        "success": True,
        "data": {
            "post": {
                "content": {
                    "text": "caption",
                    "media_urls": media_urls,
                    "thumbnail_url": thumbnail_url,
                    "duration_seconds": None,
                },
                "author": {"username": "author"},
            }
        },
        "credits_used": 1,
        "credits_remaining": 99,
        "cached": False,
        "request_id": "req-test",
    }


@pytest.mark.asyncio
async def test_socialcrawl_resolves_carousel_without_downloading_media(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "test-key")
    calls = []
    payload = socialcrawl_payload(
        [
            "https://instagram.example.fbcdn.net/a/photo.jpg?sig=one",
            "https://instagram.example.fbcdn.net/a/video.mp4?sig=two",
        ]
    )
    resolver = resolver_for(FakeResponse(payload=payload), calls=calls)

    raw = await resolver.resolve("https://www.instagram.com/p/ABC123/", "ABC123")

    assert len(calls) == 1
    assert calls[0][1]["params"] == {"url": "https://www.instagram.com/p/ABC123/"}
    assert calls[0][1]["headers"]["x-api-key"] == "test-key"
    assert raw["caption"] == "caption"
    assert raw["author"] == "author"
    assert raw["primary_media"]["media_type"] == "GraphSidecar"
    assert len(raw["carousel_children"]) == 2
    assert raw["carousel_children"][0]["display_url"].endswith("photo.jpg?sig=one")
    assert raw["carousel_children"][1]["video_url"].endswith("video.mp4?sig=two")


@pytest.mark.asyncio
async def test_socialcrawl_missing_key_fails_without_http_request(monkeypatch):
    monkeypatch.delenv("SOCIALCRAWL_API_KEY", raising=False)
    calls = []
    resolver = resolver_for(FakeResponse(payload={}), calls=calls)

    with pytest.raises(SocialCrawlNotConfiguredError):
        await resolver.resolve("https://www.instagram.com/p/ABC123/", "ABC123")

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status_code=500, payload={}),
        FakeResponse(payload={"success": False}),
        FakeResponse(payload=socialcrawl_payload([])),
        FakeResponse(payload=socialcrawl_payload(["https://example.com/not-instagram.jpg"])),
        FakeResponse(json_error=ValueError("bad json")),
    ],
)
async def test_socialcrawl_failures_are_single_attempt(monkeypatch, response):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "test-key")
    calls = []
    resolver = resolver_for(response, calls=calls)

    with pytest.raises(SocialCrawlError):
        await resolver.resolve("https://www.instagram.com/p/ABC123/", "ABC123")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_socialcrawl_timeout_is_not_retried(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "test-key")
    calls = []
    request = httpx.Request("GET", SocialCrawlResolver.ENDPOINT)
    resolver = resolver_for(error=httpx.ReadTimeout("timeout", request=request), calls=calls)

    with pytest.raises(SocialCrawlError, match="timed out"):
        await resolver.resolve("https://www.instagram.com/p/ABC123/", "ABC123")

    assert len(calls) == 1


def test_paid_fallback_classifier_only_accepts_explicit_age_gate():
    assert main._is_explicit_age_gate(
        RestrictedMediaError("Instagram explicitly restricted post ABC (reason=MA, age=16).")
    )
    assert main._is_explicit_age_gate(
        RestrictedMediaError("Instagram explicitly restricted post ABC (age=18).")
    )
    assert not main._is_explicit_age_gate(RestrictedMediaError("private or login required"))


@pytest.mark.asyncio
async def test_explicit_age_gate_uses_socialcrawl_exactly_once(monkeypatch):
    extract_calls = 0
    resolver_calls = 0
    expected_raw = {
        "caption": "fallback",
        "author": "author",
        "primary_media": {
            "media_type": "GraphImage",
            "display_url": "https://instagram.example.fbcdn.net/a/photo.jpg",
            "video_url": None,
        },
        "carousel_children": None,
    }

    async def fake_extract_media(_shortcode):
        nonlocal extract_calls
        extract_calls += 1
        raise RestrictedMediaError(
            "Instagram explicitly restricted post ABC123 (reason=MA, age=16)."
        )

    async def fake_resolve(_url, _shortcode):
        nonlocal resolver_calls
        resolver_calls += 1
        return expected_raw

    async def fake_download_and_build(shortcode, raw):
        assert shortcode == "ABC123"
        assert raw is expected_raw
        return "resolved"

    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.socialcrawl, "resolve", fake_resolve)
    monkeypatch.setattr(main, "_download_and_build_response", fake_download_and_build)

    result = await main._process_scrape_request(
        SimpleNamespace(url="https://www.instagram.com/p/ABC123/")
    )

    assert result == "resolved"
    assert extract_calls == 1
    assert resolver_calls == 1


@pytest.mark.asyncio
async def test_non_age_restriction_never_spends_socialcrawl_credit(monkeypatch):
    resolver_calls = 0

    async def fake_extract_media(_shortcode):
        raise RestrictedMediaError("private or otherwise restricted")

    async def fake_resolve(_url, _shortcode):
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("SocialCrawl must not be called")

    monkeypatch.setattr(main.storage, "is_cached", lambda _shortcode: False)
    monkeypatch.setattr(main.scraper, "extract_media", fake_extract_media)
    monkeypatch.setattr(main.socialcrawl, "resolve", fake_resolve)

    with pytest.raises(main.HTTPException) as exc_info:
        await main._process_scrape_request(
            SimpleNamespace(url="https://www.instagram.com/p/ABC123/")
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "restricted_media"
    assert resolver_calls == 0
