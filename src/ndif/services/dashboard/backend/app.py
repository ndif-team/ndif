"""FastAPI entrypoint for the NDIF dashboard.

Run (production):
    uvicorn ndif.services.dashboard.backend.app:app --host 0.0.0.0 --port 8081

Run (frontend dev): the Vite dev server on :5173 proxies ``/api`` here.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .routers import auth as auth_router
from .routers import deploy as deploy_router
from .routers import deployments as deployments_router
from .routers import monitor as monitor_router
from .routers import schedule as schedule_router


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="NDIF Dashboard", version="0.1.0")

    # CORS: only needed for `npm run dev` against a separately-running backend.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router.router)
    app.include_router(monitor_router.router)
    app.include_router(schedule_router.router)
    app.include_router(deployments_router.router)
    app.include_router(deploy_router.router)

    @app.get("/api/health")
    def health():
        return {"ok": True, "dev_mode": settings.dev_mode}

    # Serve the built Vue app. In dev (no dist/), the Vite server hosts the UI;
    # the backend just emits a helpful 404 page for the root.
    dist = settings.frontend_dist
    if dist.exists() and (dist / "index.html").exists():
        # Mount assets first (long-cacheable hashed files)
        assets_dir = dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # API routes were already matched above; anything left is the SPA.
            target = dist / full_path
            if full_path and target.is_file():
                return FileResponse(target)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def root_dev_hint():
            return JSONResponse(
                {
                    "ok": True,
                    "hint": (
                        "Frontend dist not found. Either run `npm run build` in "
                        "src/ndif/services/dashboard/frontend, or use `npm run dev` "
                        "for the Vite dev server (proxies /api to this backend)."
                    ),
                    "frontend_dist": str(dist),
                }
            )

    return app


app = create_app()
