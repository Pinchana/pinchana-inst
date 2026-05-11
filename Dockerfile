FROM python:3.13-slim

WORKDIR /workspace/pinchana-inst

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy pinchana-core (local path dependency) first
COPY pinchana-core/pyproject.toml pinchana-core/uv.lock pinchana-core/README.md ../pinchana-core/
RUN mkdir -p ../pinchana-core/src
COPY pinchana-core/src ../pinchana-core/src

# Copy scraper package files
COPY pinchana-inst/pyproject.toml pinchana-inst/uv.lock pinchana-inst/README.md ./
RUN uv sync --frozen --no-install-project

COPY pinchana-inst/src ./src

RUN mkdir -p /app/cache
ENV CACHE_PATH=/app/cache
ENV CACHE_MAX_SIZE_GB=10.0

EXPOSE 8082
CMD ["uv", "run", "uvicorn", "pinchana_inst.main:app", "--host", "0.0.0.0", "--port", "8082"]
