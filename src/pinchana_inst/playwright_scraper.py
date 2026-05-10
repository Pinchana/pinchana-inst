import asyncio
import json
import logging
import re
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Route
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)


class InstagramPlaywrightScraper:
    def __init__(self):
        self.SELECTORS = {
            "next_button": "button[aria-label='Next']",
            "article": "article[role='presentation']",
        }

    @staticmethod
    def _should_block(route: Route) -> bool:
        """Block non-essential resources to speed up loading and reduce fingerprint."""
        resource_type = route.request.resource_type
        url = route.request.url
        if resource_type in ("image", "font", "stylesheet", "media"):
            return True
        if resource_type == "script" and any(x in url for x in (
            "google-analytics", "googletagmanager", "facebook", "connect.facebook",
        )):
            return True
        return False

    async def scrape(self, url: str) -> Optional[Dict]:
        """Scrape using Playwright with multiple fallback strategies."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)

            # Block heavy resources
            await page.route("**/*", lambda route: route.abort() if self._should_block(route) else route.continue_())

            intercepted_data = {"media_node": None}

            async def on_response(response):
                if "/graphql/query" in response.url and response.status == 200:
                    try:
                        data = await response.json()
                        node = self._find_media_node(data)
                        if node:
                            intercepted_data["media_node"] = node
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                logger.info(f"Navigating to {url} via Playwright...")
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                # Wait for article to appear (max 15s)
                try:
                    await page.wait_for_selector(self.SELECTORS["article"], timeout=15000)
                except Exception:
                    logger.debug("Article selector not found within timeout.")

                # Bypass login walls and cookie dialogs
                await self._bypass_overlays(page)

                # Give a moment for scripts to populate the DOM
                await asyncio.sleep(2)

                # Strategy 1: Intercepted GraphQL
                if intercepted_data["media_node"]:
                    logger.info("Playwright: using intercepted GraphQL data.")
                    return self._parse_graphql_node(intercepted_data["media_node"])

                # Strategy 2: Embedded JSON scripts
                embedded_node = await self._extract_embedded_json(page)
                if embedded_node:
                    logger.info("Playwright: using embedded JSON data.")
                    return self._parse_graphql_node(embedded_node)

                # Strategy 3: DOM extraction
                dom_result = await self._extract_from_dom(page, url)
                if dom_result:
                    logger.info("Playwright: using DOM extraction.")
                    return dom_result

                # Strategy 4: Meta tags
                meta_result = await self._extract_from_meta_tags(page, url)
                if meta_result:
                    logger.info("Playwright: using meta tag extraction.")
                    return meta_result

                logger.warning("Playwright: all extraction methods failed.")
                return None

            except Exception as e:
                logger.error(f"Playwright scrape failed for {url}: {e}")
                return None
            finally:
                await browser.close()

    async def _bypass_overlays(self, page):
        """Remove login modals, cookie banners, and scroll locks."""
        await page.evaluate("""() => {
            const removeOverlays = () => {
                const dialogs = document.querySelectorAll('div[role="dialog"]');
                dialogs.forEach(el => {
                    const text = el.innerText.toLowerCase();
                    if (text.includes('sign up') || text.includes('log in') || text.includes('cookie') || text.includes('войти') || text.includes('зарегистрироваться')) {
                        el.remove();
                    }
                });
                document.querySelectorAll('div.x1n2onr6, div._aa8k, div._a9-z, div._a9_1').forEach(el => el.remove());
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
                document.body.style.position = 'static';
            };
            removeOverlays();
            setInterval(removeOverlays, 500);
        }""")

    async def _extract_embedded_json(self, page) -> Optional[Dict]:
        """Search script tags for the media object."""
        return await page.evaluate("""
            () => {
                const findMedia = (obj) => {
                    if (!obj || typeof obj !== 'object') return null;
                    if (obj.xdt_shortcode_media) return obj.xdt_shortcode_media;
                    if (obj.shortcode_media) return obj.shortcode_media;
                    for (const k in obj) {
                        const res = findMedia(obj[k]);
                        if (res) return res;
                    }
                    return null;
                };
                const scripts = Array.from(document.querySelectorAll('script[type="application/json"]'));
                for (const s of scripts) {
                    try {
                        const data = JSON.parse(s.innerText);
                        const media = findMedia(data);
                        if (media) return media;
                    } catch (e) {}
                }
                return null;
            }
        """)

    async def _extract_from_dom(self, page, url: str) -> Optional[Dict]:
        """Extract media URLs directly from the DOM."""
        dom_data = await page.evaluate("""
            () => {
                const imgs = Array.from(document.querySelectorAll('article img'));
                const vids = Array.from(document.querySelectorAll('article video'));
                const sources = Array.from(document.querySelectorAll('article video source'));
                const captionEl = document.querySelector('article h1, article div[dir="auto"] span');
                const caption = captionEl ? captionEl.innerText : '';
                return {
                    images: imgs.map(i => i.src).filter(s => s && s.includes('cdninstagram')),
                    videos: vids.map(v => v.src).filter(s => s),
                    sources: sources.map(s => s.src).filter(s => s),
                    caption: caption,
                };
            }
        """)

        images = dom_data.get("images", [])
        videos = dom_data.get("videos", []) + dom_data.get("sources", [])
        caption = dom_data.get("caption", "")

        if not images and not videos:
            return None

        items = []
        seen = set()

        # Prioritize videos — don't add poster images when we have actual video URLs
        for v in videos:
            if v not in seen:
                seen.add(v)
                items.append({"url": v, "type": "video", "thumbnail": images[0] if images else v, "has_audio": True})

        # Only add images if no videos were found (avoids poster frames being treated as separate media)
        if not items:
            for img in images:
                if img not in seen:
                    seen.add(img)
                    items.append({"url": img, "type": "image", "thumbnail": img})

        shortcode = self._extract_shortcode(url)

        return {
            "shortcode": shortcode,
            "caption": caption,
            "author": "",
            "primary_media": {
                "media_type": "GraphSidecar" if len(items) > 1 else ("GraphVideo" if items and items[0]["type"] == "video" else "GraphImage"),
                "display_url": items[0]["url"] if items else "",
                "video_url": items[0]["url"] if items and items[0]["type"] == "video" else None,
            },
            "carousel_children": items[1:] if len(items) > 1 else None,
        }

    async def _extract_from_meta_tags(self, page, url: str) -> Optional[Dict]:
        """Last-resort extraction from Open Graph meta tags."""
        meta = await page.evaluate("""
            () => {
                const getMeta = (prop) => {
                    const el = document.querySelector('meta[property="' + prop + '"]');
                    return el ? el.content : null;
                };
                return {
                    image: getMeta('og:image'),
                    video: getMeta('og:video') || getMeta('og:video:secure_url'),
                    description: getMeta('og:description'),
                };
            }
        """)

        if not meta.get("image") and not meta.get("video"):
            return None

        shortcode = self._extract_shortcode(url)
        items = []
        if meta.get("video"):
            items.append({"url": meta["video"], "type": "video", "thumbnail": meta.get("image", meta["video"]), "has_audio": True})
        elif meta.get("image"):
            items.append({"url": meta["image"], "type": "image", "thumbnail": meta["image"]})

        return {
            "shortcode": shortcode,
            "caption": meta.get("description", ""),
            "author": "",
            "primary_media": {
                "media_type": "GraphVideo" if meta.get("video") else "GraphImage",
                "display_url": items[0]["url"] if items else "",
                "video_url": items[0]["url"] if items and items[0]["type"] == "video" else None,
            },
            "carousel_children": None,
        }

    @staticmethod
    def _extract_shortcode(url: str) -> str:
        match = re.search(r"(?:p|reels|reel|tv|share/v)/([^/?#&]+)", url)
        return match.group(1) if match else ""

    def _find_media_node(self, obj: Any) -> Optional[Dict]:
        if isinstance(obj, dict):
            if "xdt_shortcode_media" in obj:
                return obj["xdt_shortcode_media"]
            if "shortcode_media" in obj:
                return obj["shortcode_media"]
            for v in obj.values():
                res = self._find_media_node(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = self._find_media_node(item)
                if res:
                    return res
        return None

    def _parse_graphql_node(self, node: Dict) -> Dict:
        """Map a GraphQL node to a plain dict."""
        typename = node.get("__typename", "GraphImage")

        caption_edges = (node.get("edge_media_to_caption") or {}).get("edges", [])
        caption = caption_edges[0]["node"]["text"] if caption_edges else ""
        author = (node.get("owner") or {}).get("username", "")

        primary_media = {
            "media_type": typename,
            "display_url": node.get("display_url", ""),
            "video_url": node.get("video_url") if "video" in typename.lower() else None,
        }

        carousel_children = None
        if typename in ("GraphSidecar", "XDTGraphSidecar"):
            carousel_children = []
            edges = (node.get("edge_sidecar_to_children") or {}).get("edges", [])
            for edge in edges:
                child = edge["node"]
                carousel_children.append({
                    "media_type": child.get("__typename", "GraphImage"),
                    "display_url": child.get("display_url", ""),
                    "video_url": child.get("video_url") if child.get("is_video") else None,
                })

        return {
            "shortcode": node.get("shortcode", ""),
            "caption": caption,
            "author": author,
            "primary_media": primary_media,
            "carousel_children": carousel_children,
        }
