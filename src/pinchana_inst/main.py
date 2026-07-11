"""Instagram scraper plugin — mounts as a FastAPI router."""

import asyncio
import os
import re
import logging
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import FileResponse
from pinchana_core.models import ScrapeRequest, ScrapeResponse, MediaItem
from pinchana_core.storage import MediaStorage
from pinchana_core.vpn import GluetunController, VpnRotationError
from pinchana_core.plugins import ScraperPlugin, registry
from .scraper import InstagramGraphScraper, RateLimitError, ScraperError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
scraper = InstagramGraphScraper()
gluetun = GluetunController()
storage = MediaStorage(
    base_path=os.getenv("CACHE_PATH", "./cache"),
    max_size_gb=float(os.getenv("CACHE_MAX_SIZE_GB", "10.0")),
)


def _media_url_to_path(url: str | None):
    if not url:
        return None
    url = str(url)
    if not url.startswith("/media/"):
        return None
    path_part = url.split("?", 1)[0][len("/media/"):]
    parts = path_part.split("/", 2)
    if len(parts) < 3:
        return None
    platform, shortcode, filename = parts[0], parts[1], parts[2]
    if platform != "instagram" or not shortcode or not filename:
        return None
    return storage.base_path / shortcode / filename


def _cached_media_ready(metadata: dict) -> bool:
    if not isinstance(metadata, dict):
        return False

    urls: list[str] = []
    for key in ("thumbnail_url", "video_url"):
        url = metadata.get(key)
        if url:
            urls.append(url)

    carousel = metadata.get("carousel") or []
    if isinstance(carousel, list):
        for item in carousel:
            if not isinstance(item, dict):
                continue
            for key in ("thumbnail_url", "video_url"):
                url = item.get(key)
                if url:
                    urls.append(url)

    for url in urls:
        path = _media_url_to_path(url)
        if not path or not path.exists():
            return False

    return True


def extract_shortcode(url: str) -> str:
    match = re.search(r"(?:p|reels|reel|tv|share/v)/([^/?#&]+)", str(url))
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Instagram URL format.")
    return match.group(1)


async def _download_and_build_response(shortcode: str, raw: dict) -> ScrapeResponse:
    storage.prepare_post_dir(shortcode)
    primary = raw["primary_media"]
    carousel = raw.get("carousel_children")

    tasks = []
    if primary.get("display_url"):
        tasks.append(storage.download(primary["display_url"], storage.thumbnail_path(shortcode)))
    if primary.get("video_url"):
        tasks.append(storage.download(primary["video_url"], storage.video_path(shortcode)))

    if carousel:
        for idx, child in enumerate(carousel):
            if not isinstance(child, dict):
                logger.warning("Skipping non-dict carousel child at index %d for %s", idx, shortcode)
                continue
            if child.get("display_url"):
                tasks.append(storage.download(child["display_url"], storage.carousel_thumbnail_path(shortcode, idx)))
            if child.get("video_url"):
                tasks.append(storage.download(child["video_url"], storage.carousel_video_path(shortcode, idx)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Download error: {r}")

    carousel_items = []
    if carousel:
        for idx, child in enumerate(carousel):
            if not isinstance(child, dict):
                continue
            carousel_items.append(MediaItem(
                index=idx,
                media_type=child.get("media_type", "Unknown"),
                thumbnail_url=f"/media/instagram/{shortcode}/carousel/{idx}_thumbnail.jpg",
                video_url=f"/media/instagram/{shortcode}/carousel/{idx}_video.mp4"
                if storage.carousel_video_path(shortcode, idx).exists() else None,
            ))

    response = ScrapeResponse(
        shortcode=shortcode,
        caption=raw["caption"],
        author=raw["author"],
        media_type=primary["media_type"],
        thumbnail_url=f"/media/instagram/{shortcode}/thumbnail.jpg"
        if storage.thumbnail_path(shortcode).exists() else "",
        video_url=f"/media/instagram/{shortcode}/video.mp4"
        if storage.video_path(shortcode).exists() else None,
        carousel=carousel_items if carousel else None,
    )

    storage.save_metadata(shortcode, response.model_dump())
    return response


async def _process_scrape_request(request: ScrapeRequest):
    shortcode = extract_shortcode(str(request.url))

    if storage.is_cached(shortcode):
        cached = storage.load_metadata(shortcode)
        if cached and _cached_media_ready(cached):
            logger.info("Cache hit for %s", shortcode)
            return ScrapeResponse(**cached)
        logger.info("Cache invalid for %s, missing media; re-scraping", shortcode)

    logger.info(f"Scraping Instagram shortcode: {shortcode}")
    last_error = None

    for attempt in range(1, 4):
        try:
            raw_graph_data = await scraper.extract_media(shortcode)
            raw = scraper.parse_response(raw_graph_data)
            return await _download_and_build_response(shortcode, raw)
        except RateLimitError as e:
            last_error = e
            logger.warning(f"Attempt {attempt} rate-limited: {e}")
            if attempt < 3:
                await asyncio.sleep(15)
        except VpnRotationError as e:
            last_error = e
            logger.warning(f"Attempt {attempt} VPN rotation failed: {e}")
            if attempt < 3:
                await asyncio.sleep(30)
        except ScraperError as e:
            logger.exception(f"Permanent scraper error (not retrying): {e}")
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            last_error = e
            logger.exception(f"Attempt {attempt} failed: {e}")
            if attempt < 3:
                await asyncio.sleep(15)

    raise HTTPException(
        status_code=503 if isinstance(last_error, RateLimitError) else 500,
        detail=str(last_error)
    )


@router.post("/scrape", response_model=ScrapeResponse)
async def process_scrape_request(request: ScrapeRequest):
    shortcode = extract_shortcode(str(request.url))
    return await storage.singleflight(shortcode, lambda: _process_scrape_request(request))


@router.get("/media/{platform}/{post_id}/{filename:path}")
async def serve_media(platform: str, post_id: str, filename: str):
    if platform != "instagram":
        raise HTTPException(status_code=404, detail="Invalid platform")
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=404, detail="Invalid path")

    resolved = (storage.base_path / post_id / filename).resolve()
    base_resolved = storage.base_path.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise HTTPException(status_code=404, detail="Invalid path")

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(resolved)


@router.get("/health")
async def health_check():
    try:
        status = await gluetun.get_vpn_status()
        vpn_status = status.get("status", "").lower()
        if gluetun.enabled and vpn_status != "running":
            raise HTTPException(status_code=503, detail=f"VPN not running: {vpn_status}")
        return {"status": "healthy", "service": "instagram", "vpn": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"VPN check failed: {e}")


# Register with the global plugin registry on import.
registry.register(ScraperPlugin(
    name="instagram",
    router=router,
    route_patterns=["instagram.com", "instagr.am"],
))

# Standalone FastAPI app for container mode
app = FastAPI(title="Pinchana Instagram", version="0.1.0")
app.include_router(router)


@app.on_event("shutdown")
async def close_storage_client():
    await storage.close()
