"""FastAPI application factory.

Centralises app construction so tests can build an isolated instance instead
of importing a module-level singleton with global state.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import files, health
from app.config import settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.database import init_db
from app.services.parsers import supported_extensions

logger = get_logger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

DESCRIPTION = """
Upload a **Shapefile** (as a `.zip`) or a **KML/KMZ** file and get per-feature
measurements back.

### How it works
1. `POST /api/files/` stores the file and returns `202 Accepted` with an id.
2. Parsing and measurement run in the background.
3. Poll `GET /api/files/{id}/` until `status` is `COMPLETED`.
4. Read `GET /api/files/{id}/measurements/`.

### Coordinate handling
Measurements are **never** computed from latitude/longitude degrees. Each
feature is measured either in its own projected CRS, in the UTM zone that
contains it, or geodesically on the WGS84 ellipsoid when it is too large or
too close to a pole for a single UTM zone. The method used is reported per
feature as `measurement_method`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    logger.info(
        "%s v%s ready (storage=%s, db=%s)",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.STORAGE_DIR,
        settings.DATABASE_URL.split("://", 1)[0],
    )
    yield
    logger.info("Shutting down.")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_exception_handlers(app)

    app.include_router(files.router, prefix="/api")
    app.include_router(health.router, prefix="/api")

    _mount_frontend(app)

    @app.get("/api", tags=["meta"], summary="API capabilities")
    def api_root() -> dict:
        return {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "supported_extensions": supported_extensions(),
            "docs": "/docs",
            "endpoints": {
                "upload": "POST /api/files/",
                "list": "GET /api/files/",
                "info": "GET /api/files/{id}/",
                "measurements": "GET /api/files/{id}/measurements/",
                "features": "GET /api/files/{id}/features/",
                "delete": "DELETE /api/files/{id}/",
                "health": "GET /api/health",
            },
        }

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Every error leaves the API in the same envelope shape."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "detail": _jsonable_errors(exc),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Log the traceback; return a generic message so internals never leak.
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected internal error occurred.",
                }
            },
        )


def _jsonable_errors(exc: RequestValidationError) -> list[dict]:
    """Strip non-serialisable context (e.g. exception objects) from errors."""
    cleaned = []
    for error in exc.errors():
        cleaned.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return cleaned


def _mount_frontend(app: FastAPI) -> None:
    """Serve the bundled upload UI at ``/`` when present."""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():  # pragma: no cover
        return

    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(index)


app = create_app()
