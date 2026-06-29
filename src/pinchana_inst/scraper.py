from curl_cffi.requests import AsyncSession
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import urllib.parse
import json
import logging
import asyncio
from pinchana_core.vpn import GluetunController, VpnRotationError

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """Base exception for extraction logic failures."""
    pass


class RateLimitError(ScraperError):
    """Exception indicating network-level blocking (429/403)."""
    pass


gluetun = GluetunController()


async def trigger_rotation(retry_state):
    """Trigger VPN IP rotation before each retry."""
    logger.warning(f"Retry attempt {retry_state.attempt_number}. Rotating VPN IP...")
    try:
        await gluetun.rotate_ip()
    except VpnRotationError as e:
        logger.warning(f"VPN rotation failed: {e}")
        # Re-raise as RateLimitError so tenacity continues retrying with backoff
        raise RateLimitError(str(e))


class InstagramGraphScraper:
    # Volatile parameter; may need updating if Instagram changes their API.
    DOC_ID = "8845758582119845"

    def __init__(self):
        self.base_headers = {
            "x-ig-app-id": "936619743392459",
            "x-asbd-id": "198387",
            "x-ig-www-claim": "0",
            "x-requested-with": "XMLHttpRequest",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
        }

    def _is_network_timeout(self, e: Exception) -> bool:
        """Check if an exception is a network timeout that should trigger retry."""
        error_msg = str(e).lower()
        return any(x in error_msg for x in ("timeout", "timed out", "connection", "curl: (28)"))

    async def _bootstrap_session(self, session: AsyncSession):
        """Harvest CSRF token and tracking cookies from Instagram's homepage."""
        try:
            response = await session.get("https://www.instagram.com/", timeout=15)
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during bootstrap: {e}")
            raise
        if response.status_code in (401, 403, 429):
            raise RateLimitError(f"Bootstrap HTTP {response.status_code}: IP restriction detected.")
        if response.status_code >= 400:
            raise RateLimitError(f"Bootstrap HTTP {response.status_code}: retrying after rotation.")
        csrf_token = session.cookies.get("csrftoken")
        if csrf_token:
            self.base_headers["x-csrftoken"] = csrf_token
        else:
            logger.warning("Failed to extract CSRF token during bootstrap.")

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1.5, min=4, max=30),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=trigger_rotation
    )
    async def extract_media(self, shortcode: str) -> dict:
        """Query Instagram's GraphQL API with a spoofed JA3 TLS fingerprint."""
        async with AsyncSession(impersonate="chrome124") as session:
            await self._bootstrap_session(session)

            variables = json.dumps({
                "shortcode": shortcode,
                "child_comment_count": 0,
                "fetch_comment_count": 0
            })

            payload = {
                "doc_id": self.DOC_ID,
                "variables": variables
            }

            encoded_payload = urllib.parse.urlencode(payload)
            headers = self.base_headers.copy()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

            try:
                response = await session.post(
                    "https://www.instagram.com/graphql/query/",
                    headers=headers,
                    data=encoded_payload,
                    timeout=15
                )
            except Exception as e:
                if self._is_network_timeout(e):
                    raise RateLimitError(f"Network timeout, will retry: {e}")
                raise

            if response.status_code in [401, 403, 429]:
                raise RateLimitError(f"HTTP {response.status_code}: IP restriction detected.")

            if response.status_code >= 400:
                raise RateLimitError(f"HTTP {response.status_code}: retrying after rotation.")

            data = response.json()

            media = data.get('data', {}).get('xdt_shortcode_media')
            if media is None:
                raise ScraperError("Media not found. Post may be private or deleted.")
            return media
    def parse_response(self, raw_data: dict) -> dict:
        """Transform raw GraphQL response into a plain dict."""
        typename = raw_data.get("__typename")

        caption_edges = (raw_data.get("edge_media_to_caption") or {}).get("edges", [])
        caption = ""
        if caption_edges:
            node = caption_edges[0].get("node") if isinstance(caption_edges[0], dict) else None
            if isinstance(node, dict):
                caption = node.get("text") or ""

        owner = raw_data.get("owner") or {}
        author = owner.get("username", "") if isinstance(owner, dict) else ""

        primary_media = {
            "media_type": typename,
            "display_url": raw_data.get("display_url"),
            "video_url": raw_data.get("video_url") if raw_data.get("is_video") else None,
        }

        carousel_children = None
        if typename in ("GraphSidecar", "XDTGraphSidecar"):
            carousel_children = []
            edges = (raw_data.get("edge_sidecar_to_children") or {}).get("edges", [])
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                node = edge.get("node")
                if not isinstance(node, dict):
                    continue
                carousel_children.append({
                    "media_type": node.get("__typename"),
                    "display_url": node.get("display_url"),
                    "video_url": node.get("video_url") if node.get("is_video") else None,
                })

        return {
            "shortcode": raw_data.get("shortcode"),
            "caption": caption,
            "author": author,
            "primary_media": primary_media,
            "carousel_children": carousel_children,
        }
