# Pinchana Instagram

This FastAPI module extracts public Instagram posts and Reels through Instagram's anonymous first-party web surfaces. It downloads images, videos, and ordered carousels into the shared Pinchana cache.

## Processing flow

1. Validate and normalize the submitted Instagram URL.
2. Fetch the canonical post document with browser-compatible navigation headers and extract its embedded Relay media payload.
3. If the initial document has no media, discover the current persisted operation from `expectedPreloaders`; scan referenced Relay bundles only when that metadata is absent.
4. Classify Instagram's HTTP-200 internal 404 and explicit route restrictions before treating an empty anonymous surface as ambiguous.
5. Rotate the Gluetun connection and retry within the bounded policy only after request-level `401`, `403`, `429`, or timeout evidence.
6. Download media and store it under `/app/cache/instagram/{shortcode}` in containers.

Deleted posts return `not_found`; explicit route gates return `restricted_media`; anonymous misses without decisive evidence return `anonymous_unavailable`. The latter deliberately does not guess whether a post is private, login-only, expired, or otherwise unavailable.

## API

- `POST /scrape` accepts `{"url":"https://www.instagram.com/p/SHORTCODE/"}`.
- `GET /health` reports module and VPN readiness.

External clients should use the gateway's authenticated `POST /v1/scrape` route.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_PATH` | `./cache` | Base media cache path |
| `CACHE_MAX_SIZE_GB` | `10.0` | Maximum cache size before oldest-post eviction |
| `GLUETUN_CONTROL_URL` | `http://localhost:8000` | Private Gluetun control endpoint |

## Development and validation

```sh
uv sync --frozen
uv run uvicorn pinchana_inst.main:app --host 0.0.0.0 --port 8082 --reload
```

```sh
uv run pytest -q
PINCHANA_INST_LIVE=1 uv run pytest -q
```

Live tests contact Instagram and are opt-in. Build the image from the parent repository so `pinchana-core` is available:

```sh
docker build --file pinchana-inst/Dockerfile --tag pinchana-inst:local .
```
