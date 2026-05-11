# 📸 Pinchana Instagram Scraper

**Pinchana Instagram Scraper** is a high-performance module for extracting media from Instagram using a GraphQL-only strategy backed by VPN rotation and resilient retries.

---

## ✨ Key Features

- **🚀 GraphQL-Only Scraping:** High-speed direct queries to Instagram's internal API using `curl-cffi` with JA3/TLS fingerprint impersonation.
- **🔄 Smart VPN Rotation:** Automatically detects rate limits (403/429) and signals the VPN (Gluetun) to rotate IPs.
- **💾 Local Caching:** Saves downloaded images, videos, and carousels to a persistent LRU cache.
- **🌐 Standalone Service:** Operates as a FastAPI service that can be easily integrated into the Pinchana Gateway.

---

## 🏗 Architecture

The scraper follows an "Extract -> Download -> Cache" workflow:
1. **Extraction:** Uses Instagram GraphQL with robust retry and anti-block handling.
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
uv run uvicorn src.pinchana_inst.main:app --host 0.0.0.0 --port 8082
```

---

## 📜 License

MIT
