# InScraper — Agent Guide

## Project Overview

InScraper is an Instagram media downloader with VPN rotation. It exposes a FastAPI REST service that scrapes post metadata and downloads images/videos, then serves them from a local on-disk cache. All outbound traffic is forced through a VPN tunnel managed by a Gluetun sidecar container.

Two scraping strategies are implemented:
1. **GraphQL scraper** (`app/scraper.py`) — directly queries Instagram's internal API using `curl-cffi` with JA3/TLS impersonation. This is the primary path.
2. **Playwright scraper** (`app/playwright_scraper.py`) — headless Chromium fallback that intercepts GraphQL responses, extracts embedded JSON, reads the DOM, or falls back to Open Graph meta tags.

## Technology Stack

- **Language:** Python 3.13+
- **Package Manager:** `uv` (Astral)
- **Web Framework:** FastAPI + Uvicorn
- **HTTP Clients:** `curl-cffi` (scraper), `httpx` (storage downloads + VPN controller)
- **Browser Automation:** Playwright + `playwright-stealth`
- **Validation:** Pydantic v2
- **Retry Logic:** bounded endpoint retry with one optional VPN rotation
- **VPN Orchestration:** Gluetun control API
- **Container:** Docker (python:3.13-slim base)
- **CI/CD:** GitHub Actions → GHCR

## Project Structure

```
app/
  __init__.py
  main.py              # FastAPI app, route handlers, orchestration logic
  models.py            # Pydantic request/response models
  scraper.py           # InstagramGraphScraper + RateLimitError
  playwright_scraper.py # InstagramPlaywrightScraper (4 fallback strategies)
  storage.py           # MediaStorage (download, LRU eviction, metadata)
  vpn_controller.py    # GluetunController (rotate IP, poll connection)
tests/
  test_scrapers.py     # Live integration tests against real Instagram URLs
```

## Build & Run Commands

```bash
# Install dependencies
uv sync

# Run the API locally
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080

# Run tests (may skip if your IP is rate-limited)
uv run pytest tests/test_scrapers.py -s

# Build Docker image locally
docker build -t pinchana-inst .
```

## Runtime Architecture

The scraper container is designed to run inside Gluetun's network namespace (`network_mode: "service:gluetun"`). All outbound traffic is therefore tunneled through NordVPN (or another provider configured in Gluetun).

When the GraphQL scraper receives a definite 401/403/429 block or network timeout, it raises `RateLimitError`. The FastAPI endpoint owns the complete retry policy: it clears bootstrap state, rotates through `GluetunController.rotate_ip()`, and retries exactly once. Anonymous GraphQL responses that contain no media continue through the remaining GraphQL and HTML fallbacks without rotating the VPN.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/scrape` | Primary GraphQL scraper; downloads media and returns metadata |
| `POST` | `/scrape/playwright` | Playwright fallback scraper |
| `GET`  | `/media/{shortcode}/{filename}` | Serve cached media files |
| `GET`  | `/health` | Health check; verifies VPN is connected via Gluetun |

### Caching Behavior

`MediaStorage` maintains an on-disk cache under `CACHE_PATH` (default `./cache`). Each post gets a directory named by its shortcode containing:
- `metadata.json`
- `thumbnail.jpg`
- `video.mp4`
- `carousel/{index}_thumbnail.jpg`
- `carousel/{index}_video.mp4`

If the total cache size exceeds `CACHE_MAX_SIZE_GB`, the oldest post directory (by earliest file mtime) is evicted using LRU.

The `/scrape` endpoints check `storage.is_cached(shortcode)` first and return the stored metadata immediately if present.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NORDVPN_USER` | NordVPN service username | — |
| `NORDVPN_PASSWORD` | NordVPN service password | — |
| `GLUETUN_API_KEY` | API key for Gluetun HTTP control server | `secret-key` |
| `GLUETUN_CONTROL_URL` | Gluetun control server URL | `http://localhost:8000` |
| `CACHE_PATH` | Cache directory path | `./cache` |
| `CACHE_MAX_SIZE_GB` | Max cache size before LRU eviction | `10.0` |

## Testing Strategy

- **Framework:** pytest with `pytest-asyncio` (strict mode, function-scoped loop).
- **Test file:** `tests/test_scrapers.py`
- **Nature:** Live integration tests against real Instagram URLs (carousel, single image, reels, carousel with video).
- **Resilience:** The Graph scraper test catches 403/429 exceptions and calls `pytest.skip()` when running locally without a VPN sidecar. The Playwright test asserts the full data pipeline.
- **Running inside Docker:** For full fidelity, run tests inside the Docker Compose stack where Gluetun is present.

## Code Style Guidelines

- **Async first:** All I/O-bound operations use `async`/`await`.
- **Type hints:** Used consistently across modules.
- **Logging:** Standard `logging` module; `logger = logging.getLogger(__name__)` pattern in every module.
- **Exceptions:** Custom hierarchies (`ScraperError` → `RateLimitError`, `RestrictedMediaError`, `MediaNotFoundError`; `VpnRotationError`) distinguish retryable network blocks from post-specific restrictions and fatal extraction errors.
- **Pydantic models:** All API request/response contracts are modeled (`ScrapeRequest`, `ScrapeResponse`, `MediaItem`).
- **Path safety:** The media-serving route resolves paths and validates they stay inside `base_path` to prevent directory traversal.
- **Constants:** Volatile Instagram API parameters (e.g., `DOC_ID`, `x-ig-app-id`) are declared as class attributes near the top of the scraper for easy updates.

## Security Considerations

- **Zero-trust networking:** The scraper container has no direct outbound network access; it shares the network namespace with the Gluetun sidecar.
- **VPN rotation:** Automated IP rotation on blocking responses (403/429) to avoid persistent rate-limiting.
- **Gluetun restart behavior:** Gluetun unsets sensitive environment variables (VPN credentials) from memory after startup. **Never use `docker restart gluetun`** — this causes `AUTH_ERROR`. Always recreate the container (`docker compose up -d --force-recreate gluetun`). The programmatic rotation via the API avoids this issue entirely.
- **Media path traversal:** The `GET /media/{shortcode}/{filename}` endpoint rejects `..` segments and resolves the final path to ensure it lies within the cache directory.

## Deployment

- **Image registry:** GHCR (`ghcr.io/xxanqw/inscraper`)
- **CI/CD:** `.github/workflows/docker-publish.yml` builds and pushes on every push to `main` (when app/Dockerfile/pyproject.toml/uv.lock change) and on version tags (`v*.*.*`).
- **Recommended orchestration:** Docker Compose with a `gluetun` service and a `scraper` service using `network_mode: "service:gluetun"`.

## Important Implementation Notes

- `extract_media` has no retry decorator. `_process_scrape_request` is the single retry owner and permits two total extraction attempts with at most one VPN rotation.
- `GluetunController.rotate_ip()` enforces a 90-second cooldown (`ROTATION_COOLDOWN`) between rotations to avoid flapping.
- Playwright scraping blocks non-essential resources (images, fonts, stylesheets, analytics scripts) to reduce fingerprint and speed up loading.
- Playwright uses four fallback strategies in order: intercepted GraphQL response → embedded JSON in `<script type="application/json">` tags → DOM extraction → Open Graph meta tags.
