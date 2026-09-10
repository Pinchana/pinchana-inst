"""SocialCrawl fallback for explicitly age-restricted Instagram posts."""

import logging
import os
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


class SocialCrawlError(Exception):
    """Base error raised by the SocialCrawl resolver."""


class SocialCrawlDisabledError(SocialCrawlError):
    """The SocialCrawl fallback is not configured."""


class SocialCrawlResolver:
    """Resolve signed Instagram media URLs through SocialCrawl.

    This is intentionally a resolver only: media is downloaded immediately by
    Pinchana's existing MediaStorage flow, so short-lived Instagram CDN URLs are
    never persisted in metadata.
    """

    ENDPOINT = "https://www.socialcrawl.dev/v1/instagram/post"
    DEFAULT_TIMEOUT_SECONDS = 20.0
    ALLOWED_MEDIA_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    @staticmethod
    def _api_key() -> str:
        return os.getenv("SOCIALCRAWL_API_KEY", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key())

    @staticmethod
    def _timeout_seconds() -> float:
        raw = os.getenv("SOCIALCRAWL_TIMEOUT_SECONDS", "20").strip()
        try:
            value = float(raw)
        except ValueError:
            return SocialCrawlResolver.DEFAULT_TIMEOUT_SECONDS
        return value if value > 0 else SocialCrawlResolver.DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def _is_allowed_media_url(cls, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        hostname = parsed.hostname.lower().rstrip(".")
        return any(hostname.endswith(suffix) for suffix in cls.ALLOWED_MEDIA_HOST_SUFFIXES)

    @staticmethod
    def _is_video_url(url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        return (
            path.endswith((".mp4", ".m4v", ".mov", ".webm"))
            or "mime_type=video" in query
            or "video_mp4" in query
        )

    @classmethod
    def _normalise_media(cls, payload: dict, shortcode: str) -> dict:
        if payload.get("platform") != "instagram":
            raise SocialCrawlError("SocialCrawl returned an unexpected platform")

        data = payload.get("data")
        post = data.get("post") if isinstance(data, dict) else None
        content = post.get("content") if isinstance(post, dict) else None
        author = post.get("author") if isinstance(post, dict) else None
        ext = post.get("ext") if isinstance(post, dict) else None

        if not isinstance(content, dict):
            raise SocialCrawlError("SocialCrawl response is missing post content")

        raw_urls = content.get("media_urls")
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        if not isinstance(raw_urls, list):
            raise SocialCrawlError("SocialCrawl response is missing media_urls")

        media_urls: list[str] = []
        for item in raw_urls:
            if not isinstance(item, str):
                continue
            url = item.strip()
            if not url:
                continue
            if not cls._is_allowed_media_url(url):
                raise SocialCrawlError("SocialCrawl returned a non-Instagram media URL")
            media_urls.append(url)
        if not media_urls:
            raise SocialCrawlError("SocialCrawl returned no usable media URLs")

        thumbnail_url = content.get("thumbnail_url")
        if isinstance(thumbnail_url, str):
            thumbnail_url = thumbnail_url.strip()
            if thumbnail_url and not cls._is_allowed_media_url(thumbnail_url):
                raise SocialCrawlError("SocialCrawl returned a non-Instagram thumbnail URL")
        if not isinstance(thumbnail_url, str) or not thumbnail_url:
            thumbnail_url = None

        content_type = ""
        if isinstance(ext, dict):
            content_type = str(ext.get("content_type") or "").lower()
        duration = content.get("duration_seconds")

        items: list[dict] = []
        for index, media_url in enumerate(media_urls):
            is_video = cls._is_video_url(media_url)
            if len(media_urls) == 1 and not is_video:
                is_video = content_type in {"video", "reel"} or duration not in (None, 0, 0.0, "")

            items.append({
                "media_type": "GraphVideo" if is_video else "GraphImage",
                "display_url": thumbnail_url if is_video and index == 0 else (None if is_video else media_url),
                "video_url": media_url if is_video else None,
            })

        if len(items) == 1:
            primary = items[0]
            carousel = None
        else:
            first = items[0]
            primary = {
                "media_type": "GraphSidecar",
                "display_url": first.get("display_url") or thumbnail_url,
                "video_url": first.get("video_url"),
            }
            carousel = items

        caption = content.get("text")
        username = author.get("username") if isinstance(author, dict) else ""

        return {
            "shortcode": shortcode,
            "caption": caption if isinstance(caption, str) else "",
            "author": username if isinstance(username, str) else "",
            "primary_media": primary,
            "carousel_children": carousel,
        }

    async def resolve(self, post_url: str, shortcode: str) -> dict:
        """Resolve one Instagram post without retrying the paid endpoint."""
        api_key = self._api_key()
        if not api_key:
            raise SocialCrawlDisabledError("SOCIALCRAWL_API_KEY is not configured")

        timeout = httpx.Timeout(self._timeout_seconds())
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                response = await client.get(
                    self.ENDPOINT,
                    params={"url": post_url},
                    headers={"x-api-key": api_key, "Accept": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise SocialCrawlError("SocialCrawl request timed out") from exc
        except httpx.HTTPError as exc:
            raise SocialCrawlError("SocialCrawl request failed") from exc

        if not 200 <= response.status_code < 300:
            raise SocialCrawlError(f"SocialCrawl returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SocialCrawlError("SocialCrawl returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise SocialCrawlError("SocialCrawl returned an invalid response object")
        if payload.get("success") is not True:
            raise SocialCrawlError("SocialCrawl reported an unsuccessful lookup")

        logger.info(
            "SocialCrawl resolved restricted Instagram post %s: request_id=%s credits_used=%s "
            "credits_remaining=%s cached=%s",
            shortcode,
            payload.get("request_id"),
            payload.get("credits_used"),
            payload.get("credits_remaining"),
            payload.get("cached"),
        )
        return self._normalise_media(payload, shortcode)
