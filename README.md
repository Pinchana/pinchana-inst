# Pinchana Instagram

This FastAPI module extracts public Instagram posts, Reels, and compatible video posts through an HTTP GraphQL workflow. It downloads images, videos, and ordered carousels into the shared Pinchana cache.

## Processing flow

1. Validate and normalize the submitted Instagram URL.
2. Extract post data through Instagram GraphQL using the configured HTTP impersonation.
3. Rotate the Gluetun connection and retry within the bounded policy after relevant `403` or `429` responses.
4. Download media and store it under `/app/cache/instagram/{shortcode}` in containers.

Private, deleted, login-only, region-restricted, or changed posts can return a structured rejection even when the service is healthy.

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
