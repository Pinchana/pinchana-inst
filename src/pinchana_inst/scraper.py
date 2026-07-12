from curl_cffi.requests import AsyncSession
from tenacity import retry, stop_after_attempt, wait_exponential
import json
import logging
import re
import urllib.parse
import os

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
    if retry_state.args:
        scraper_inst = retry_state.args[0]
        if hasattr(scraper_inst, "clear_bootstrap_cache"):
            scraper_inst.clear_bootstrap_cache()
            logger.info("Cleared Instagram scraper bootstrap cache.")
    try:
        await gluetun.rotate_ip()
    except VpnRotationError as e:
        logger.warning(f"VPN rotation failed: {e}")
        raise RateLimitError(str(e))


def _should_retry_rate_limit(retry_state):
    """Only retry rate limits if VPN rotation is enabled."""
    vpn_enabled = os.getenv("VPN_ENABLED", "true").lower() in ("1", "true", "yes")
    if not vpn_enabled:
        return False
    exception = retry_state.outcome.exception()
    return isinstance(exception, RateLimitError)


class InstagramGraphScraper:
    # Volatile web API parameters; keep them grouped for quick updates.
    LEGACY_SHORTCODE_DOC_ID = "8845758582119845"
    POLARIS_LOGGED_OUT_DOC_ID = "27130156389949648"
    POLARIS_FRIENDLY_NAME = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
    SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    def __init__(self):
        self.base_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "x-ig-app-id": "936619743392459",
            "x-asbd-id": "129477",
            "x-ig-www-claim": "0",
            "x-requested-with": "XMLHttpRequest",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
        }
        self._bootstrap_cache = None
        self._cookie_cache = None

    def clear_bootstrap_cache(self):
        self._bootstrap_cache = None
        self._cookie_cache = None

    def _is_network_timeout(self, e: Exception) -> bool:
        """Check if an exception is a network timeout that should trigger retry."""
        error_msg = str(e).lower()
        return any(x in error_msg for x in ("timeout", "timed out", "connection", "curl: (28)"))

    async def _bootstrap_session(self, session: AsyncSession) -> dict[str, str]:
        """Harvest CSRF, LSD, and tracking cookies from Instagram's homepage."""
        if self._bootstrap_cache and self._cookie_cache:
            for k, v in self._cookie_cache.items():
                session.cookies.set(k, v)
            return self._bootstrap_cache

        try:
            response = await session.get("https://www.instagram.com/", timeout=15)
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during bootstrap: {e}") from e
            raise

        if response.status_code in (401, 403, 429):
            raise RateLimitError(f"Bootstrap HTTP {response.status_code}: IP restriction detected.")
        if response.status_code >= 400:
            raise RateLimitError(f"Bootstrap HTTP {response.status_code}: retrying after rotation.")

        csrf_token = session.cookies.get("csrftoken") or self._extract_csrf_token(response.text)
        if not csrf_token:
            raise RateLimitError("Bootstrap did not return a CSRF token; treating as a soft block.")

        lsd_token = self._extract_lsd_token(response.text)
        if not lsd_token:
            raise RateLimitError("Bootstrap did not return an LSD token; treating as a soft block.")

        self._bootstrap_cache = {"csrf_token": csrf_token, "lsd_token": lsd_token}
        self._cookie_cache = {k: v for k, v in session.cookies.items()}
        return self._bootstrap_cache

    @classmethod
    def _shortcode_to_media_id(cls, shortcode: str) -> str:
        media_id = 0
        for char in shortcode:
            try:
                media_id = media_id * 64 + cls.SHORTCODE_ALPHABET.index(char)
            except ValueError as e:
                raise ScraperError(f"Invalid Instagram shortcode character: {char}") from e
        return str(media_id)

    @staticmethod
    def _extract_csrf_token(html: str) -> str | None:
        match = re.search(r'"csrf_token"\s*:\s*"([^"]+)"', html)
        return match.group(1) if match else None

    @staticmethod
    def _extract_lsd_token(html: str) -> str | None:
        patterns = [
            r'\["LSD",\[\],\{"token":"([^"]+)"\}',
            r'"LSD",\s*\[\],\s*\{"token"\s*:\s*"([^"]+)"\}',
            r'"__bbox"\s*:\s*\{.*?"LSD".*?"token"\s*:\s*"([^"]+)".*?\}',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _is_graphql_execution_error(data: dict) -> bool:
        errors = data.get("errors")
        if not isinstance(errors, list):
            return False
        for error in errors:
            if not isinstance(error, dict):
                continue
            if "execution error" in str(error.get("message", "")).lower():
                return True
        return False

    @staticmethod
    def _first_dict(items) -> dict | None:
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                return item
        return None

    @classmethod
    def _display_url(cls, raw: dict) -> str | None:
        if raw.get("display_url"):
            return raw.get("display_url")
        if raw.get("thumbnail_src"):
            return raw.get("thumbnail_src")
        if raw.get("image_url"):
            return raw.get("image_url")

        resource = cls._first_dict(raw.get("display_resources"))
        if resource and resource.get("src"):
            return resource.get("src")

        candidate = cls._first_dict((raw.get("image_versions2") or {}).get("candidates"))
        if candidate and candidate.get("url"):
            return candidate.get("url")

        return None

    @classmethod
    def _video_url(cls, raw: dict) -> str | None:
        if raw.get("video_url"):
            return raw.get("video_url")
        video = cls._first_dict(raw.get("video_versions"))
        if video and video.get("url"):
            return video.get("url")
        return None

    @staticmethod
    def _caption_text(raw: dict) -> str:
        caption_edges = (raw.get("edge_media_to_caption") or {}).get("edges", [])
        if caption_edges:
            node = caption_edges[0].get("node") if isinstance(caption_edges[0], dict) else None
            if isinstance(node, dict):
                return node.get("text") or ""

        caption = raw.get("caption")
        if isinstance(caption, str):
            return caption
        if isinstance(caption, dict):
            return caption.get("text") or ""
        return ""

    @staticmethod
    def _owner(raw: dict) -> dict:
        owner = raw.get("owner")
        if isinstance(owner, dict):
            return owner
        user = raw.get("user")
        if isinstance(user, dict):
            return user
        return {}

    @classmethod
    def _media_typename(cls, raw: dict, children: list[dict]) -> str:
        typename = raw.get("__typename")
        if typename:
            return str(typename)
        if children:
            return "GraphSidecar"
        if raw.get("is_video") or cls._video_url(raw):
            return "GraphVideo"
        return "GraphImage"

    @classmethod
    def _normalize_single_child(cls, raw: dict) -> dict:
        return {
            "__typename": cls._media_typename(raw, []),
            "display_url": cls._display_url(raw),
            "is_video": bool(raw.get("is_video") or cls._video_url(raw)),
            "video_url": cls._video_url(raw),
        }

    @classmethod
    def _normalize_children(cls, raw: dict) -> list[dict]:
        children: list[dict] = []

        edges = (raw.get("edge_sidecar_to_children") or {}).get("edges", [])
        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    children.append(cls._normalize_single_child(node))

        carousel_media = raw.get("carousel_media")
        if isinstance(carousel_media, list):
            for child in carousel_media:
                if isinstance(child, dict):
                    children.append(cls._normalize_single_child(child))

        return children

    @classmethod
    def _normalize_media_node(cls, raw: dict) -> dict:
        children = cls._normalize_children(raw)
        typename = cls._media_typename(raw, children)
        owner = cls._owner(raw)
        caption = cls._caption_text(raw)

        media = {
            "__typename": typename,
            "shortcode": raw.get("shortcode") or raw.get("code"),
            "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]} if caption else {"edges": []},
            "owner": {"username": owner.get("username", "")},
            "display_url": cls._display_url(raw),
            "is_video": bool(raw.get("is_video") or cls._video_url(raw)),
            "video_url": cls._video_url(raw),
        }

        if children:
            media["__typename"] = "GraphSidecar"
            media["edge_sidecar_to_children"] = {"edges": [{"node": child} for child in children]}

        return media

    @classmethod
    def _unwrap_polaris_media(cls, data: dict) -> dict | None:
        media = data.get("xig_polaris_media") if isinstance(data, dict) else None
        if not isinstance(media, dict):
            return None
        if_not_gated = media.get("if_not_gated_logged_out")
        if isinstance(if_not_gated, dict):
            return if_not_gated
        return media

    @classmethod
    def _find_media_node(cls, obj) -> dict | None:
        """Recursively search a JSON tree for Instagram media nodes."""
        if isinstance(obj, dict):
            polaris = cls._unwrap_polaris_media(obj)
            if polaris is not None:
                return cls._normalize_media_node(polaris)
            if isinstance(obj.get("if_not_gated_logged_out"), dict):
                return cls._normalize_media_node(obj["if_not_gated_logged_out"])
            if isinstance(obj.get("xdt_shortcode_media"), dict):
                return cls._normalize_media_node(obj["xdt_shortcode_media"])
            if isinstance(obj.get("shortcode_media"), dict):
                return cls._normalize_media_node(obj["shortcode_media"])
            for value in obj.values():
                found = cls._find_media_node(value)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = cls._find_media_node(item)
                if found is not None:
                    return found
        return None

    async def _read_json_response(self, response, context: str) -> dict:
        if response.status_code in (401, 403, 429):
            raise RateLimitError(f"{context} HTTP {response.status_code}: IP restriction detected.")
        if response.status_code >= 400:
            raise RateLimitError(f"{context} HTTP {response.status_code}: retrying after rotation.")
        try:
            data = response.json()
        except Exception as e:
            raise RateLimitError(f"{context} returned invalid JSON; treating as a soft block: {e}") from e
        if not isinstance(data, dict):
            raise RateLimitError(
                f"{context} returned {type(data).__name__} instead of JSON object; treating as a soft block."
            )
        return data

    def _headers(self, bootstrap: dict[str, str], referer: str = "https://www.instagram.com/") -> dict[str, str]:
        headers = self.base_headers.copy()
        headers["Referer"] = referer
        headers["x-csrftoken"] = bootstrap["csrf_token"]
        return headers

    async def _extract_polaris_logged_out(
        self,
        session: AsyncSession,
        shortcode: str,
        bootstrap: dict[str, str],
    ) -> dict:
        media_id = self._shortcode_to_media_id(shortcode)
        payload = {
            "av": "0",
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__req": "1",
            "__comet_req": "7",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": self.POLARIS_FRIENDLY_NAME,
            "variables": json.dumps({"media_id": media_id}, separators=(",", ":")),
            "server_timestamps": "true",
            "doc_id": self.POLARIS_LOGGED_OUT_DOC_ID,
        }
        headers = self._headers(bootstrap, f"https://www.instagram.com/p/{shortcode}/")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["x-fb-friendly-name"] = self.POLARIS_FRIENDLY_NAME
        headers["x-fb-lsd"] = bootstrap["lsd_token"]

        try:
            response = await session.post(
                "https://www.instagram.com/api/graphql",
                headers=headers,
                data=urllib.parse.urlencode(payload),
                timeout=15,
            )
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during logged-out GraphQL: {e}") from e
            raise

        data = await self._read_json_response(response, "Logged-out GraphQL")
        media = self._find_media_node(data.get("data") or data)
        if media is not None:
            logger.info("Extracted media via logged-out GraphQL for %s", shortcode)
            return media

        raw_preview = json.dumps(data, ensure_ascii=False)[:500]
        if self._is_graphql_execution_error(data):
            raise RateLimitError(f"Logged-out GraphQL execution error for {shortcode}: {raw_preview}")
        if data.get("data") is None and data.get("status") == "ok":
            raise RateLimitError(f"Logged-out GraphQL returned empty data for {shortcode}: {raw_preview}")

        raise ScraperError(f"Logged-out GraphQL did not include public media for {shortcode}.")

    async def _extract_legacy_shortcode(
        self,
        session: AsyncSession,
        shortcode: str,
        bootstrap: dict[str, str],
    ) -> dict | None:
        payload = {
            "variables": json.dumps({"shortcode": shortcode}, separators=(",", ":")),
            "doc_id": self.LEGACY_SHORTCODE_DOC_ID,
            "server_timestamps": "true",
        }
        headers = self._headers(bootstrap)
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            response = await session.post(
                "https://www.instagram.com/graphql/query",
                headers=headers,
                data=urllib.parse.urlencode(payload),
                timeout=15,
            )
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during legacy GraphQL: {e}") from e
            raise

        data = await self._read_json_response(response, "Legacy GraphQL")
        media = self._find_media_node(data.get("data") or data)
        if media is not None:
            logger.info("Extracted media via legacy GraphQL for %s", shortcode)
            return media

        raw_preview = json.dumps(data, ensure_ascii=False)[:500]
        logger.warning("Legacy GraphQL returned no media for %s: %s", shortcode, raw_preview)
        if self._is_graphql_execution_error(data) or (data.get("data") is None and data.get("status") == "ok"):
            raise RateLimitError(f"Legacy GraphQL returned a soft-block response for {shortcode}: {raw_preview}")
        return None

    async def _extract_from_html(self, session: AsyncSession, shortcode: str) -> dict:
        """Fallback: fetch the post page and extract embedded JSON media node."""
        url = f"https://www.instagram.com/p/{shortcode}/"
        logger.info("GraphQL returned no media; trying HTML fallback for %s", shortcode)
        try:
            response = await session.get(url, timeout=15)
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during HTML fetch: {e}") from e
            raise

        if response.status_code in (401, 403, 429):
            raise RateLimitError(f"HTML fetch HTTP {response.status_code}: IP restriction detected.")
        if response.status_code >= 400:
            raise RateLimitError(f"HTML fetch HTTP {response.status_code}: retrying after rotation.")

        blocks = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', response.text, re.DOTALL)
        for block in blocks:
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            media = self._find_media_node(data)
            if media is not None:
                logger.info("Extracted media from embedded HTML JSON for %s", shortcode)
                return media

        raise ScraperError(
            "Media not found in Instagram anonymous GraphQL responses or HTML fallback. "
            "The post may be private, login-gated, region-gated, or blocked by IP reputation."
        )

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1.5, min=4, max=30),
        retry=_should_retry_rate_limit,
        before_sleep=trigger_rotation,
    )
    async def extract_media(self, shortcode: str) -> dict:
        """Query Instagram public media using anonymous web endpoints."""
        async with AsyncSession(impersonate="chrome124") as session:
            bootstrap = await self._bootstrap_session(session)

            try:
                return await self._extract_polaris_logged_out(session, shortcode, bootstrap)
            except RateLimitError:
                raise
            except ScraperError as e:
                logger.warning("Logged-out GraphQL failed for %s: %s", shortcode, e)

            try:
                legacy = await self._extract_legacy_shortcode(session, shortcode, bootstrap)
                if legacy is not None:
                    return legacy
            except RateLimitError:
                raise

            return await self._extract_from_html(session, shortcode)

    def parse_response(self, raw_data: dict) -> dict:
        """Transform normalized GraphQL response into a plain dict."""
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
