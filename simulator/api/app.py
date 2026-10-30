# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""FastAPI application factory."""

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from simulator.api.routes_pos import router as pos_router
from simulator.api.routes_equipment import router as equipment_router
from simulator.api.routes_inventory import router as inventory_router
from simulator.api.routes_schedule import router as schedule_router
from simulator.api.routes_sim import router as sim_router
from simulator.api.routes_chat import router as chat_router
from simulator.api.routes_alerts import router as alerts_router
from simulator.api.routes_push import router as push_router
from simulator.engine.clock import VirtualClock
from simulator.engine.engine import SimulationEngine
from simulator.state.store import StateStore

logger = logging.getLogger(__name__)

#: Location of the Vite build output relative to the backend working directory.
#: Overridable via ``FRONTEND_DIST_DIR`` for non-standard deployments.
_DEFAULT_FRONTEND_DIST = Path("frontend") / "dist"

#: Opt-in CORS. Env var name for a comma-separated list of EXACT allowed origins
#: (e.g. ``https://a.example,https://b.example``).
_CORS_ORIGINS_ENV = "SIM_CORS_ALLOW_ORIGINS"


def _parse_cors_origins(raw: str | None) -> list[str]:
    """Parse the CORS allow-list, rejecting the wildcard.

    CORS is OFF by default and there is no environment that needs it:

    * Deployed, FastAPI serves the SPA itself (see the ``_spa_*`` routes below),
      so the dashboard is same-origin.
    * Locally, the Vite dev server PROXIES ``/api`` and ``/ws`` to this process
      (``frontend/vite.config.js``), so the browser only ever sees same-origin
      requests there too.

    So the previous ``allow_origins=["*"]`` was pure attack surface, and it let
    any site on the internet read this API's responses cross-origin. It is also
    why the wildcard is REJECTED rather than merely discouraged here: the safe
    state must not depend on an operator remembering not to type ``*``. An
    origin list has to be explicit or there is no CORS at all.
    """
    if not raw or not raw.strip():
        return []
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise ValueError(
            f"{_CORS_ORIGINS_ENV} must list exact origins; '*' is not allowed. "
            "Leave it unset to disable CORS entirely (the default, and correct "
            "for both the deployed SPA and the Vite dev proxy)."
        )
    return origins

#: Files at the root of ``frontend/dist`` are served individually at their own
#: URL (Vite references them via absolute paths in ``index.html`` or at runtime:
#: ``favicon.svg``, ``icons.svg``, ``sw.js``, ...). Mounting ``/`` on
#: ``StaticFiles`` would shadow the ``/api`` and ``/ws`` routes, so they are
#: routed explicitly instead -- see ``_spa_root_file``, which tests for a real
#: file rather than consulting a hardcoded list (a list is what caused the
#: 2026-08-05 ``sw.js`` MIME-type bug).


def create_app(state_store: StateStore, engine: SimulationEngine, clock: VirtualClock) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Restaurant Simulator API")

    # CORS is opt-in and off by default -- see ``_parse_cors_origins``. Adding the
    # middleware with an empty allow-list would still emit CORS machinery for no
    # reason, so it is only attached when origins are actually configured.
    cors_origins = _parse_cors_origins(os.environ.get(_CORS_ORIGINS_ENV))
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )
        logger.info("CORS enabled for origins: %s", ", ".join(cors_origins))
    else:
        logger.info("CORS disabled (same-origin only); set %s to enable", _CORS_ORIGINS_ENV)

    # Store dependencies on app state for route access
    app.state.store = state_store
    app.state.engine = engine
    app.state.clock = clock

    app.include_router(pos_router, prefix="/api")
    app.include_router(equipment_router, prefix="/api")
    app.include_router(inventory_router, prefix="/api")
    app.include_router(schedule_router, prefix="/api")
    app.include_router(sim_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(alerts_router, prefix="/api")
    app.include_router(push_router, prefix="/api")

    # Serve the Vite-built SPA from ``frontend/dist`` when it exists. This
    # lets a single FastAPI process serve the dashboard, the REST API, and
    # the WebSocket endpoint on one port, which is what the cloud deployment
    # (spec 004) relies on. During local development the Vite dev server is
    # used instead, so a missing ``frontend/dist`` is not an error — the
    # dashboard is only served by FastAPI in built / production mode.
    frontend_dist = Path(os.environ.get("FRONTEND_DIST_DIR", _DEFAULT_FRONTEND_DIST))
    if frontend_dist.is_dir() and (frontend_dist / "index.html").is_file():
        index_html = frontend_dist / "index.html"

        # ``/`` returns the SPA bootstrap page. The React app then loads
        # bundled assets from ``/assets/...`` (see StaticFiles mount below).
        #
        # semgrep's useless-inner-function does not model decorator side
        # effects: the decorator registers the route, so nothing calls this by
        # name and that is correct. Same for the two SPA handlers below.
        # nosemgrep: useless-inner-function
        @app.get("/", include_in_schema=False)
        async def _spa_index() -> FileResponse:
            return FileResponse(index_html)

        # Vite-bundled JS / CSS chunks live under ``/assets/``.
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="spa-assets",
            )

        # Individual files at the dist root (favicon, icons, sw.js, ...) that the
        # HTML or the runtime references by absolute path. We don't mount a
        # StaticFiles at ``/`` because that would shadow ``/api`` and ``/ws``.
        #
        # Anything that is NOT a real file at the dist root is a CLIENT-SIDE
        # ROUTE (``/chat``, ``/architecture``, ``/voice``), so it falls through to
        # the SPA. This route is registered before the catch-all below and
        # FastAPI matches in registration order, so raising 404 here would
        # shadow the catch-all for every single-segment path -- which is exactly
        # what happened until 2026-07-31: a hard refresh or deep link to /chat or
        # /architecture returned 404, and it went unnoticed because navigating
        # from ``/`` is handled client-side by react-router and never hits the
        # server.
        #
        # Existence is the test, NOT a hardcoded allowlist. The allowlist was the
        # 2026-08-05 Web Push bug: ``frontend/public/sw.js`` shipped in the build
        # but was absent from the list, so ``GET /sw.js`` fell through and
        # returned ``index.html``. The browser then refused to register the
        # service worker -- "The script has an unsupported MIME type
        # ('text/html')" -- because a fallback HTML page is served for a URL that
        # must be JavaScript. Any future file dropped into ``frontend/public/``
        # would have repeated it.
        dist_root = frontend_dist.resolve()

        # nosemgrep: useless-inner-function
        @app.get("/{filename}", include_in_schema=False)
        async def _spa_root_file(filename: str) -> FileResponse:
            candidate = (frontend_dist / filename).resolve()
            # Containment check: with the allowlist gone, the filename reaches the
            # filesystem, so confine it to files sitting DIRECTLY in the dist root.
            # ``parent == dist_root`` rejects both traversal (``..``) and nested
            # paths, so this cannot be used to read arbitrary files.
            if candidate.parent == dist_root and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)

        # SPA catch-all: serve index.html for any unmatched path so that
        # client-side routing (react-router-dom) can handle it.  API routes
        # and the /assets mount are registered first and take priority — this
        # only fires for paths like /chat that have no server-side handler.
        # nosemgrep: useless-inner-function
        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_catch_all(full_path: str) -> FileResponse:
            return FileResponse(index_html)

        logger.info("SPA enabled: serving %s at /", frontend_dist)
    else:
        logger.info(
            "SPA not served: %s/index.html not found (run `npm run build` in frontend/)",
            frontend_dist,
        )

    return app
