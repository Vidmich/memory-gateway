# Multi-stage build. Task 18 hardens this further (read-only rootfs, distroless-style
# runtime, vulnerability scanning); what matters here is that dependencies are a cached
# layer and the runtime image carries no build tooling.
#
# One image serves both the API and the UI. That is not just convenience: same-origin
# means the refresh cookie needs no SameSite=None, there is no CORS preflight in front of
# the login request, and there is one thing to deploy and roll back rather than two that
# can be at different versions.

FROM node:22-alpine AS web-builder

WORKDIR /srv/web

# `npm ci` from the lockfile, in its own layer, so it is reused until the lockfile moves.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:0.5-python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv

# Dependencies first: this layer is reused until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/.venv/bin:$PATH" \
    WEB_DIST_DIR=/srv/web

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 gateway

WORKDIR /srv

COPY --from=builder --chown=gateway:gateway /srv/.venv /srv/.venv
COPY --chown=gateway:gateway alembic.ini ./
COPY --chown=gateway:gateway migrations ./migrations
COPY --chown=gateway:gateway app ./app
COPY --from=web-builder --chown=gateway:gateway /srv/web/dist ./web
COPY --chown=gateway:gateway deploy/compose/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

USER gateway
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
