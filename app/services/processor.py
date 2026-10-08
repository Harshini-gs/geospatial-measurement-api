"""Processing orchestrator.

Drives the pipeline for one uploaded file:

    PENDING -> PROCESSING -> parse -> measure -> persist -> COMPLETED
                                  \\-> FAILED (with a stored reason)

Runs in a background thread via FastAPI's ``BackgroundTasks``, so the upload
request returns immediately instead of blocking for the duration of a large
file. It therefore opens its own database session -- the request-scoped
session is already closed by the time this executes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.database import SessionLocal
from app.models import Feature, MeasurementType, ProcessingStatus, UploadedFile
from app.services.crs import crs_to_string
from app.services.measurement import measure
from app.services.parsers import ParsedDataset, ParsedFeature, get_parser

logger = get_logger(__name__)

# Guard against a single pathological feature producing a multi-megabyte
# GeoJSON blob in the database.
MAX_STORED_COORDINATES = 100_000


def process_file_task(file_id: str) -> None:
    """Entry point for the background task. Owns its own session."""
    db = SessionLocal()
    try:
        process_file(db, file_id)
    except Exception:  # noqa: BLE001 - last-resort guard, background thread
        logger.exception("Unhandled error processing file %s", file_id)
    finally:
        db.close()


def process_file(db: Session, file_id: str) -> None:
    record = db.get(UploadedFile, file_id)
    if record is None:
        logger.error("process_file called for unknown file %s", file_id)
        return

    logger.info("Processing %s (%s)", record.id, record.filename)
    record.status = ProcessingStatus.PROCESSING
    record.processing_started_at = _utcnow()
    db.commit()

    try:
        parser = get_parser(record.file_type)
        dataset = parser.parse(Path(record.stored_path))
        _persist(db, record, dataset)

        record.status = ProcessingStatus.COMPLETED
        record.error_message = None
        logger.info(
            "Completed %s: %d features, CRS %s",
            record.id,
            record.feature_count,
            record.source_crs,
        )

    except AppError as exc:
        logger.warning("Processing failed for %s: %s", record.id, exc.message)
        record.status = ProcessingStatus.FAILED
        record.error_message = exc.message

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected failure processing %s", record.id)
        record.status = ProcessingStatus.FAILED
        record.error_message = f"Unexpected error: {type(exc).__name__}: {exc}"

    finally:
        record.processing_completed_at = _utcnow()
        db.commit()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist(db: Session, record: UploadedFile, dataset: ParsedDataset) -> None:
    """Measure every feature and write the whole set in one transaction."""
    record.source_crs = crs_to_string(dataset.crs)
    record.source_crs_name = dataset.crs.name
    record.layer_name = dataset.layer_name
    record.warnings = list(dataset.warnings)

    # Replace any features from a previous processing attempt.
    for stale in list(record.features):
        db.delete(stale)
    db.flush()

    rows = [_build_row(record.id, parsed, dataset) for parsed in dataset.features]
    db.add_all(rows)
    record.feature_count = len(rows)
    db.flush()


def _build_row(
    file_id: str, parsed: ParsedFeature, dataset: ParsedDataset
) -> Feature:
    geometry = parsed.geometry

    if geometry is None:
        return Feature(
            file_id=file_id,
            feature_index=parsed.index,
            source_id=parsed.source_id,
            geometry_type=parsed.geometry_type,
            geometry=None,
            bbox=None,
            properties=parsed.properties,
            is_supported=False,
            measurement_type=MeasurementType.NONE,
            skip_reason=parsed.parse_error or "No geometry present.",
            measurement_method="not_applicable",
        )

    result = measure(geometry, dataset.crs)

    return Feature(
        file_id=file_id,
        feature_index=parsed.index,
        source_id=parsed.source_id,
        geometry_type=parsed.geometry_type,
        geometry=_safe_geojson(geometry),
        bbox=_safe_bbox(geometry),
        properties=parsed.properties,
        is_supported=result.supported,
        measurement_type=result.measurement_type,
        area_sqm=result.area_sqm,
        perimeter_m=result.perimeter_m,
        length_m=result.length_m,
        measurement_crs=result.measurement_crs,
        measurement_method=result.method,
        skip_reason=result.skip_reason,
    )


def _safe_geojson(geometry: BaseGeometry) -> dict | None:
    """GeoJSON for the feature, unless it is too large to store inline."""
    try:
        if _count_coordinates(geometry) > MAX_STORED_COORDINATES:
            return {
                "type": geometry.geom_type,
                "truncated": True,
                "reason": (
                    f"Geometry exceeds {MAX_STORED_COORDINATES} coordinates and "
                    "is not stored inline."
                ),
            }
        return mapping(geometry)
    except Exception:  # noqa: BLE001 pragma: no cover
        return None


def _safe_bbox(geometry: BaseGeometry) -> list[float] | None:
    try:
        return [round(v, 9) for v in geometry.bounds]
    except Exception:  # noqa: BLE001 pragma: no cover
        return None


def _count_coordinates(geometry: BaseGeometry) -> int:
    if hasattr(geometry, "geoms"):
        return sum(_count_coordinates(part) for part in geometry.geoms)
    if geometry.geom_type == "Polygon":
        total = len(geometry.exterior.coords)
        return total + sum(len(ring.coords) for ring in geometry.interiors)
    try:
        return len(geometry.coords)
    except Exception:  # noqa: BLE001 pragma: no cover
        return 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
