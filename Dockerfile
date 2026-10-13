# Container image for any Docker host or PaaS (e.g. Railway). State lives on the /data volume.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/app/.venv/bin:$PATH
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY deploy ./deploy
RUN uv sync --frozen --no-dev

CMD ["deploy/railway-start.sh"]
