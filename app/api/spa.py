"""Serving the built SPA from the API process.

In development Vite serves the assets and proxies ``/api`` here, so this is unused. In
production the same container serves both, which removes a second deployment unit and
makes the frontend same-origin with the API — so the refresh cookie needs no
``SameSite=None`` and there is no CORS preflight on the login path.

The only real work is the history fallback: a SPA route like ``/gateways/abc`` exists in
the browser's router but not on disk, and a hard refresh on it must return ``index.html``
rather than 404.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


class SinglePageApp(StaticFiles):
    """Static files, with unknown paths falling back to ``index.html``."""

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            response = await super().get_response(path, scope)
            if response.status_code != 404:
                return response
        except HTTPException as exc:
            # With `html=True`, StaticFiles *raises* on a miss rather than returning a
            # 404 response, so both shapes have to be handled.
            if exc.status_code != 404:
                raise

        # A missing *asset* should still 404. Returning HTML for a missing .js turns a
        # deploy mistake into a confusing MIME-type failure in the browser console.
        if "." in Path(path).name:
            raise HTTPException(status_code=404)
        return await super().get_response("index.html", scope)


def mount_spa(app: FastAPI, directory: str) -> bool:
    """Mount the built SPA at ``/``. Returns whether anything was mounted.

    Must be called after every API router: a mount at ``/`` matches everything, so any
    route registered afterwards would be unreachable.
    """
    if not directory:
        return False

    root = Path(directory)
    if not (root / "index.html").is_file():
        logger.warning(
            "WEB_DIST_DIR is set but has no index.html; not serving the UI",
            extra={"web_dist_dir": str(root)},
        )
        return False

    app.mount("/", SinglePageApp(directory=root, html=True), name="web")
    logger.info("serving web UI", extra={"web_dist_dir": str(root)})
    return True
