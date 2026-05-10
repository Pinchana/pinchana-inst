# 📸 Pinchana Instagram Scraper

**Pinchana Instagram Scraper** is a high-performance module for extracting media from Instagram. It employs a dual-scraping strategy—combining high-speed GraphQL queries with a robust Playwright fallback—to ensure reliable data extraction even when platform protections are active.

---

## ✨ Key Features

- **🚀 Dual Scraping Strategy:**
    - **GraphQL (Primary):** High-speed direct queries to Instagram's internal API using `curl-cffi` with JA3/TLS fingerprint impersonation.
    - **Playwright (Fallback):** Headless Chromium extraction used when GraphQL is blocked, supporting DOM parsing and intercepted JSON.
- **🔄 Smart VPN Rotation:** Automatically detects rate limits (403/429) and signals the VPN (Gluetun) to rotate IPs.
- **💾 Local Caching:** Saves downloaded images, videos, and carousels to a persistent LRU cache.
- **🌐 Standalone Service:** Operates as a FastAPI service that can be easily integrated into the Pinchana Gateway.

---

## 🏗 Architecture

The scraper follows an "Extract -> Download -> Cache" workflow:
1. **Extraction:** Attempts GraphQL first; if it fails or returns an error, it falls back to Playwright.
2. **Download:** Media is downloaded through the secure VPN tunnel.
3. **Storage:** Files are organized under `/app/cache/instagram/{shortcode}`.

---

## 📡 API Reference

### `POST /scrape`
Extracts and downloads media for a given Instagram URL (Posts, Reels, TV).
```json
{
  "url": "https://www.instagram.com/p/C6_abcdefg/"
}
```

### `GET /health`
Checks service health, VPN connectivity, and scraper status.

---

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_PATH` | `./cache` | Base path for media storage. |
| `CACHE_MAX_SIZE_GB` | `10.0` | Max size for the LRU cache. |
| `GLUETUN_CONTROL_URL` | `http://localhost:8000` | URL for the Gluetun control API. |

---

## 🛠 Development

Managed by `uv`.

```bash
uv sync
# Install Playwright browsers
uv run playwright install chromium
# Run the service
uv run uvicorn src.pinchana_inst.main:app --host 0.0.0.0 --port 8082
```

---

## 📜 License

MIT
