import json
import logging
import re
import urllib.parse
from html import unescape

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """Base exception for extraction logic failures."""
    pass


class RateLimitError(ScraperError):
    """Exception indicating network-level blocking (429/403)."""
    pass


class RestrictedMediaError(ScraperError):
    """Instagram explicitly marked the post as restricted."""
    pass


class AnonymousMediaUnavailableError(ScraperError):
    """Anonymous first-party surfaces returned no media and no decisive reason."""
    pass


class MediaNotFoundError(ScraperError):
    """The post was removed or does not exist."""
    pass


class InstagramGraphScraper:
    POLARIS_FRIENDLY_NAME = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
    SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    MAX_RELAY_BUNDLES = 40
    MAX_RELAY_BUNDLE_BYTES = 8_000_000
    SCRIPT_SRC_RE = re.compile(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
    OPERATION_MODULE_RE = re.compile(
        r'__d\("(?P<name>[A-Za-z0-9_$]+(?:Query|Mutation))_instagramRelayOperation"'
        r'.{0,600}?\.exports="(?P<doc_id>\d{12,})"',
        re.DOTALL,
    )
    DIRECT_OPERATION_RE = re.compile(
        r'params:\{id:"(?P<doc_id>\d{12,})".{0,600}?'
        r'name:"(?P<name>[A-Za-z0-9_$]+(?:Query|Mutation))"',
        re.DOTALL,
    )

    def __init__(self, session_factory=AsyncSession):
        self._session_factory = session_factory
        self.base_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _is_network_timeout(self, e: Exception) -> bool:
        """Check if an exception is a network timeout that should trigger retry."""
        error_msg = str(e).lower()
        return any(x in error_msg for x in ("timeout", "timed out", "connection", "curl: (28)"))

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
    def _normalize_media_candidate(
        cls,
        raw: object,
        expected_shortcode: str | None = None,
    ) -> dict | None:
        if not isinstance(raw, dict):
            return None
        shortcode = raw.get("shortcode") or raw.get("code")
        if not shortcode or (expected_shortcode is not None and shortcode != expected_shortcode):
            return None
        if not (cls._display_url(raw) or cls._video_url(raw) or cls._normalize_children(raw)):
            return None
        return cls._normalize_media_node(raw)

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
    def _find_media_node(cls, obj, expected_shortcode: str | None = None) -> dict | None:
        """Recursively search a JSON tree for Instagram media nodes."""
        if isinstance(obj, dict):
            polaris = cls._unwrap_polaris_media(obj)
            if polaris is not None:
                normalized = cls._normalize_media_candidate(polaris, expected_shortcode)
                if normalized is not None:
                    return normalized
            if isinstance(obj.get("if_not_gated_logged_out"), dict):
                normalized = cls._normalize_media_candidate(
                    obj["if_not_gated_logged_out"], expected_shortcode
                )
                if normalized is not None:
                    return normalized
            if isinstance(obj.get("xdt_shortcode_media"), dict):
                normalized = cls._normalize_media_candidate(
                    obj["xdt_shortcode_media"], expected_shortcode
                )
                if normalized is not None:
                    return normalized
            if isinstance(obj.get("shortcode_media"), dict):
                normalized = cls._normalize_media_candidate(
                    obj["shortcode_media"], expected_shortcode
                )
                if normalized is not None:
                    return normalized
            for value in obj.values():
                found = cls._find_media_node(value, expected_shortcode)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = cls._find_media_node(item, expected_shortcode)
                if found is not None:
                    return found
        return None

    async def _read_json_response(self, response, context: str) -> dict:
        if response.status_code in (401, 403, 429):
            raise RateLimitError(f"{context} HTTP {response.status_code}: IP restriction detected.")
        if response.status_code >= 400:
            raise ScraperError(f"{context} HTTP {response.status_code}.")
        try:
            data = response.json()
        except Exception as e:
            raise ScraperError(f"{context} returned invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ScraperError(
                f"{context} returned {type(data).__name__} instead of a JSON object."
            )
        return data

    @staticmethod
    def _walk_dicts(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from InstagramGraphScraper._walk_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from InstagramGraphScraper._walk_dicts(child)

    @staticmethod
    def _application_json_documents(html: str) -> list[object]:
        blocks = re.findall(
            r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        documents: list[object] = []
        for block in blocks:
            try:
                documents.append(json.loads(block))
            except (json.JSONDecodeError, ValueError):
                try:
                    documents.append(json.loads(unescape(block)))
                except (json.JSONDecodeError, ValueError):
                    continue
        return documents

    @classmethod
    def _is_internal_not_found(cls, documents: list[object]) -> bool:
        route_dicts = [item for document in documents for item in cls._walk_dicts(document)]
        has_404_flag = any(item.get("show_lox_redesigned_404_page") is True for item in route_dicts)
        has_error_page = any(item.get("pageID") == "httpErrorPage" for item in route_dicts)
        has_error_root = any(
            any(
                isinstance(value, str) and "PolarisErrorRoot" in value
                for value in item.values()
            )
            for item in route_dicts
        )
        return has_404_flag and has_error_page and has_error_root

    @classmethod
    def _restriction_details(cls, documents: list[object]) -> tuple[str | None, object] | None:
        for document in documents:
            for item in cls._walk_dicts(document):
                reason = item.get("failure_reason")
                age = item.get("restricted_age")
                if "page_type" in item and (reason is not None or age is not None):
                    return str(reason) if reason is not None else None, age
        return None

    @classmethod
    def _discover_html_preloader(cls, documents: list[object]) -> dict | None:
        matches: dict[str, dict] = {}
        for document in documents:
            for item in cls._walk_dicts(document):
                if item.get("queryName") != cls.POLARIS_FRIENDLY_NAME:
                    continue
                doc_id = str(item.get("queryID", ""))
                variables = item.get("variables")
                if not doc_id.isdigit() or len(doc_id) < 12 or not isinstance(variables, dict):
                    continue
                matches[doc_id] = {
                    "friendly_name": cls.POLARIS_FRIENDLY_NAME,
                    "doc_id": doc_id,
                    "variables": variables,
                    "source": "html_preloader",
                }
        if len(matches) > 1:
            raise ScraperError(
                f"HTML exposed multiple current {cls.POLARIS_FRIENDLY_NAME} doc_ids: "
                f"{sorted(matches)}"
            )
        return next(iter(matches.values()), None)

    @classmethod
    def _discover_operation_in_bundle_text(cls, text: str) -> str | None:
        matches: set[str] = set()
        for pattern in (cls.OPERATION_MODULE_RE, cls.DIRECT_OPERATION_RE):
            for match in pattern.finditer(text):
                if match.group("name") == cls.POLARIS_FRIENDLY_NAME:
                    matches.add(match.group("doc_id"))
        if len(matches) > 1:
            raise ScraperError(
                f"JS bundles exposed multiple current {cls.POLARIS_FRIENDLY_NAME} doc_ids: "
                f"{sorted(matches)}"
            )
        return next(iter(matches), None)

    @classmethod
    def _bundle_urls(cls, html: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for raw_url in cls.SCRIPT_SRC_RE.findall(html):
            url = urllib.parse.urljoin("https://www.instagram.com/", unescape(raw_url))
            if urllib.parse.urlsplit(url).hostname != "static.cdninstagram.com" or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls[: cls.MAX_RELAY_BUNDLES]

    async def _discover_bundle_operation(self, session: AsyncSession, html: str) -> dict | None:
        headers = {**self.base_headers, "Accept": "*/*", "Referer": "https://www.instagram.com/"}
        for url in self._bundle_urls(html):
            try:
                response = await session.get(url, headers=headers, timeout=15)
            except Exception as e:
                if self._is_network_timeout(e):
                    continue
                raise
            if response.status_code != 200:
                continue
            text = response.text
            if len(text.encode("utf-8")) > self.MAX_RELAY_BUNDLE_BYTES:
                continue
            doc_id = self._discover_operation_in_bundle_text(text)
            if doc_id:
                return {
                    "friendly_name": self.POLARIS_FRIENDLY_NAME,
                    "doc_id": doc_id,
                    "source": url,
                }
        return None

    def _navigation_headers(self) -> dict[str, str]:
        return {
            **self.base_headers,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }

    async def _fetch_post_page(self, session: AsyncSession, shortcode: str) -> str:
        url = f"https://www.instagram.com/p/{shortcode}/"
        try:
            response = await session.get(url, headers=self._navigation_headers(), timeout=15)
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during post-page fetch: {e}") from e
            raise

        if response.status_code in (401, 403, 429):
            raise RateLimitError(f"Post page HTTP {response.status_code}: IP restriction detected.")
        if response.status_code == 404:
            raise MediaNotFoundError(f"Instagram post {shortcode} was not found.")
        if response.status_code >= 400:
            raise ScraperError(f"Post page HTTP {response.status_code}.")
        return response.text

    async def _execute_relay_operation(
        self,
        session: AsyncSession,
        shortcode: str,
        operation: dict,
        page_html: str,
    ) -> dict | None:
        variables = operation.get("variables")
        if not isinstance(variables, dict):
            variables = {"media_id": self._shortcode_to_media_id(shortcode)}
        payload = {
            "av": "0",
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__req": "1",
            "__comet_req": "7",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": operation["friendly_name"],
            "variables": json.dumps(variables, separators=(",", ":")),
            "server_timestamps": "true",
            "doc_id": operation["doc_id"],
        }
        headers = {
            **self.base_headers,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.instagram.com",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
            "x-fb-friendly-name": operation["friendly_name"],
        }
        lsd_token = self._extract_lsd_token(page_html)
        if lsd_token:
            payload["lsd"] = lsd_token
            headers["x-fb-lsd"] = lsd_token
        cookies = getattr(session, "cookies", None)
        csrf_token = cookies.get("csrftoken") if cookies is not None else None
        csrf_token = csrf_token or self._extract_csrf_token(page_html)
        if csrf_token:
            headers["x-csrftoken"] = csrf_token

        try:
            response = await session.post(
                "https://www.instagram.com/api/graphql",
                headers=headers,
                data=urllib.parse.urlencode(payload),
                timeout=15,
            )
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during current Relay query: {e}") from e
            raise

        data = await self._read_json_response(response, "Current Relay query")
        media = self._find_media_node(data.get("data") or data, shortcode)
        if media is not None:
            logger.info(
                "Extracted media via discovered Relay operation %s for %s",
                operation["doc_id"],
                shortcode,
            )
            return media
        raw_preview = json.dumps(data, ensure_ascii=False)[:500]
        logger.warning("Current Relay query returned no public media for %s: %s", shortcode, raw_preview)
        return None

    async def extract_media(self, shortcode: str) -> dict:
        """Extract public media from Instagram's current anonymous web client."""
        async with self._session_factory(impersonate="chrome124") as session:
            page_html = await self._fetch_post_page(session, shortcode)
            documents = self._application_json_documents(page_html)

            if self._is_internal_not_found(documents):
                raise MediaNotFoundError(f"Instagram post {shortcode} was not found.")

            restriction = self._restriction_details(documents)
            if restriction is not None:
                reason, age = restriction
                details = ", ".join(
                    part
                    for part in (
                        f"reason={reason}" if reason else "",
                        f"age={age}" if age is not None else "",
                    )
                    if part
                )
                raise RestrictedMediaError(
                    f"Instagram explicitly restricted post {shortcode}"
                    + (f" ({details})." if details else ".")
                )

            for document in documents:
                media = self._find_media_node(document, shortcode)
                if media is not None:
                    logger.info("Extracted media from the initial HTML for %s", shortcode)
                    return media

            operation = self._discover_html_preloader(documents)
            if operation is not None:
                expected_media_id = self._shortcode_to_media_id(shortcode)
                observed_media_id = str(operation["variables"].get("media_id", ""))
                if observed_media_id != expected_media_id:
                    raise ScraperError(
                        f"HTML preloader media_id mismatch for {shortcode}: "
                        f"expected {expected_media_id}, got {observed_media_id or 'missing'}"
                    )
            if operation is None:
                operation = await self._discover_bundle_operation(session, page_html)
            if operation is not None:
                media = await self._execute_relay_operation(session, shortcode, operation, page_html)
                if media is not None:
                    return media

            raise AnonymousMediaUnavailableError(
                f"Instagram returned no anonymous media or decisive route reason for {shortcode}."
            )

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
