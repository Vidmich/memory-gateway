# One image, two processes. The API and the worker run the same code against the same
# backing services and differ only in their command — SPEC §15 makes the application one
# stateless artefact, and a second image would be a second thing to keep at the same
# version and a second thing to scan.
#
# The SPA is built into it as well. That is not just convenience: same-origin means the
# refresh cookie needs no SameSite=None, there is no CORS preflight in front of the login
# request, and the UI cannot be at a different version from the API that serves it.
#
# Hardening (task 18), in the order it matters:
#   * non-root, with a fixed uid the chart can assert in its security context;
#   * a runtime layer with no package manager, no compiler and no shell utilities beyond
#     what the base image ships — nothing that reads a URL, in particular;
#   * writable state confined to /tmp, so the container runs with a read-only root
#     filesystem and the chart can say so.

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

FROM python:3.14-slim-bookworm AS runtime

# `TIKTOKEN_CACHE_DIR` is the one that makes a read-only root filesystem work: tiktoken
# writes its downloaded vocabulary to a cache directory on first use, and with nowhere to
# write it re-fetches on every call. /tmp is an emptyDir in the chart, so the cache is
# per-pod and warms once.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/.venv/bin:$PATH" \
    HOME=/tmp \
    TIKTOKEN_CACHE_DIR=/tmp/tiktoken \
    WEB_DIST_DIR=/srv/web

# No `apt-get install` here at all. The previous version added curl for a container
# healthcheck; Kubernetes probes over HTTP itself and compose now uses the interpreter
# that is already in the image, so the only thing curl was doing was adding a tool that
# fetches URLs to an image whose whole threat model is about fetching URLs.
RUN useradd --create-home --uid 10001 gateway

WORKDIR /srv

COPY --from=builder --chown=gateway:gateway /srv/.venv /srv/.venv
COPY --chown=gateway:gateway alembic.ini ./
COPY --chown=gateway:gateway migrations ./migrations
COPY --chown=gateway:gateway app ./app
COPY --from=web-builder --chown=gateway:gateway /srv/web/dist ./web
COPY --chown=gateway:gateway deploy/compose/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

# Read by the chart's `checksum` annotation and by anyone holding an image with no tag.
LABEL org.opencontainers.image.title="memory-gateway" \
      org.opencontainers.image.description="AI model gateway that augments requests with memory" \
      org.opencontainers.image.source="https://github.com/memory-gateway/memory-gateway" \
      org.opencontainers.image.licenses="MIT"

USER 10001
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
