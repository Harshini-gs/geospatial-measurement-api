"""Liveness / readiness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
def health(db: Session = Depends(get_db)) -> HealthResponse:
    """Reports process health and database connectivity."""
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        database = f"error: {type(exc).__name__}"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        database=database,
    )
