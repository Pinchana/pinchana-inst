FROM python:3.13-slim

WORKDIR /workspace/pinchana-inst

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget gnupg libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpangocairo-1.0-0 libpango-1.0-0 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy pinchana-core (local path dependency) first
COPY pinchana-core/pyproject.toml pinchana-core/uv.lock pinchana-core/README.md ../pinchana-core/
RUN mkdir -p ../pinchana-core/src
COPY pinchana-core/src ../pinchana-core/src

# Copy scraper package files
COPY pinchana-inst/pyproject.toml pinchana-inst/uv.lock pinchana-inst/README.md ./
RUN uv sync --frozen --no-install-project

# Install Playwright browsers (use venv python directly to avoid building package before src is copied)
RUN .venv/bin/python -m playwright install chromium

COPY pinchana-inst/src ./src

RUN mkdir -p /app/cache
ENV CACHE_PATH=/app/cache
ENV CACHE_MAX_SIZE_GB=10.0

EXPOSE 8082
CMD ["uv", "run", "uvicorn", "pinchana_inst.main:app", "--host", "0.0.0.0", "--port", "8082"]
