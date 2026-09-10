"""SocialCrawl resolver for explicitly age-gated Instagram posts."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


class SocialCrawlError(Exception):
    """Base error for SocialCrawl fallback failures."""


class SocialCrawlNotConfiguredError(SocialCrawlError):
    """Raised when the fallback is requested without an API key."""


class SocialCrawlResolver:
    """Resolve short-lived Instagram CDN URLs for one restricted post.

    This client intentionally performs exactly one HTTP request and never retries,
    because every successful SocialCrawl lookup consumes a credit.
    """

    ENDPOINT = "https://www.socialcrawl.dev/v1/instagram/post"
    DEFAULT_TIMEOUT_SECONDS = 15.0
    _ALLOWED_CDN_SUFFIXES = (".fbcdn.net", ".cdninstagram.com")

    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._client_factory = client_factory

    @staticmethod
    def _api_key() -> str:
        return os.getenv("SOCIALCRAWL_API_KEY", "").strip()

    @classmethod
    def _timeout_seconds(cls) -> float:
        raw = os.getenv("SOCIALCRAWL_TIMEOUT_SECONDS", "").strip()
        if not raw:
            return cls.DEFAULT_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "Invalid SOCIALCRAWL_TIMEOUT_SECONDS=%r; using %.1fs",
                raw,
                cls.DEFAULT_TIMEOUT_SECONDS,
            )
            return cls.DEFAULT_TIMEOUT_SECONDS
        return max(1.0, min(value, 60.0))

    @classmethod
    def _validate_media_url(cls, url: object) -> str:
        if not isinstance(url, str) or not url:
            raise SocialCrawlError("SocialCrawl returned an invalid media URL")
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            hostname.endswith(suffix) for suffix in cls._ALLOWED_CDN_SUFFIXES
        ):
            raise SocialCrawlError("SocialCrawl returned a non-Instagram CDN media URL")
        return url

    @staticmethod
    def _is_video_url(url: str) -> bool:
        return urlsplit(url).path.lower().endswith(".mp4")

    @classmethod
    def _media_item(cls, url: str, *, thumbnail_url: str | None = None) -> dict:
        if cls._is_video_url(url):
            return {
                "media_type": "GraphVideo",
                "display_url": thumbnail_url,
                "video_url": url,
            }
        return {
            "media_type": "GraphImage",
            "display_url": url,
            "video_url": None,
        }

    async def resolve(self, post_url: str, shortcode: str) -> dict:
        """Resolve a restricted Instagram post into pinchana-inst's parsed shape."""
        api_key = self._api_key()
        if not api_key:
            raise SocialCrawlNotConfiguredError("SOCIALCRAWL_API_KEY is not configured")

        try:
            async with self._client_factory(
                timeout=self._timeout_seconds(),
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    self.ENDPOINT,
                    params={"url": post_url},
                    headers={"x-api-key": api_key, "Accept": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise SocialCrawlError("SocialCrawl request timed out") from exc
        except httpx.HTTPError as exc:
            raise SocialCrawlError("SocialCrawl request failed") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise SocialCrawlError(f"SocialCrawl returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SocialCrawlError("SocialCrawl returned invalid JSON") from exc

        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise SocialCrawlError("SocialCrawl did not return a successful response")

        data = payload.get("data")
        post = data.get("post") if isinstance(data, dict) else None
        content = post.get("content") if isinstance(post, dict) else None
        author_data = post.get("author") if isinstance(post, dict) else None
        if not isinstance(content, dict):
            raise SocialCrawlError("SocialCrawl response is missing post content")

        raw_urls = content.get("media_urls")
        if not isinstance(raw_urls, list) or not raw_urls:
            raise SocialCrawlError("SocialCrawl returned no media URLs")
        media_urls = [self._validate_media_url(url) for url in raw_urls]

        thumbnail_url = content.get("thumbnail_url")
        if thumbnail_url:
            thumbnail_url = self._validate_media_url(thumbnail_url)
        else:
            thumbnail_url = None

        items = [self._media_item(url) for url in media_urls]
        caption = content.get("text") if isinstance(content.get("text"), str) else ""
        author = (
            author_data.get("username", "")
            if isinstance(author_data, dict) and isinstance(author_data.get("username"), str)
            else ""
        )

        if len(items) == 1:
            primary = dict(items[0])
            if primary["media_type"] == "GraphVideo" and thumbnail_url:
                primary["display_url"] = thumbnail_url
            carousel = None
        else:
            first = items[0]
            primary = {
                "media_type": "GraphSidecar",
                "display_url": first.get("display_url") or thumbnail_url,
                "video_url": None,
            }
            carousel = items

        logger.info(
            "SocialCrawl resolved restricted Instagram post %s: request_id=%s "
            "credits_used=%s credits_remaining=%s cached=%s media_count=%d",
            shortcode,
            payload.get("request_id"),
            payload.get("credits_used"),
            payload.get("credits_remaining"),
            payload.get("cached"),
            len(media_urls),
        )

        return {
            "shortcode": shortcode,
            "caption": caption,
            "author": author,
            "primary_media": primary,
            "carousel_children": carousel,
        }
