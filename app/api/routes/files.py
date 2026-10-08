"""File upload, inspection and measurement endpoints."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, UploadFile, status
from fastapi import File as FileParam
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import (
    EmptyFileError,
    FileNotReadyError,
    FileTooLargeError,
    ResourceNotFoundError,
)
from app.core.logging import get_logger
from app.database import get_db
from app.models import Feature, ProcessingStatus, UploadedFile
from app.schemas import (
    ErrorResponse,
    FeatureCollectionResponse,
    FeatureMeasurementOut,
    FileInfoResponse,
    FileListResponse,
    MeasurementsResponse,
    MeasurementSummary,
    Pagination,
    UploadResponse,
)
from app.services.parsers import detect_file_type, supported_extensions
from app.services.processor import process_file_task

logger = get_logger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

# Read uploads in chunks so a large file never lands in memory all at once.
CHUNK_SIZE = 1024 * 1024  # 1 MB

COMMON_ERRORS = {
    404: {"model": ErrorResponse, "description": "File not found"},
    422: {"model": ErrorResponse, "description": "Invalid geospatial file"},
}


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a geospatial file",
    responses={
        413: {"model": ErrorResponse, "description": "File too large"},
        415: {"model": ErrorResponse, "description": "Unsupported file type"},
    },
)
async def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = FileParam(..., description="A .zip Shapefile, or a .kml / .kmz file"),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Accept a geospatial file and queue it for processing.

    Returns **202 Accepted** immediately. Parsing and measurement run in the
    background, so poll `GET /api/files/{id}/` until `status` is `COMPLETED`
    (or `FAILED`), then fetch the measurements.
    """
    original_name = Path(file.filename or "upload").name
    file_type = detect_file_type(original_name)        # raises 415 if unknown

    file_id = uuid.uuid4().hex
    destination = settings.STORAGE_DIR / f"{file_id}{Path(original_name).suffix.lower()}"

    size = await _stream_to_disk(file, destination)

    record = UploadedFile(
        id=file_id,
        filename=original_name,
        file_type=file_type,
        content_type=file.content_type,
        size_bytes=size,
        stored_path=str(destination),
        status=ProcessingStatus.PENDING,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    background_tasks.add_task(process_file_task, file_id)
    logger.info("Accepted upload %s (%s, %d bytes)", file_id, original_name, size)

    base = str(request.base_url).rstrip("/")
    return UploadResponse(
        id=record.id,
        filename=record.filename,
        file_type=record.file_type,
        size_bytes=record.size_bytes,
        status=record.status,
        created_at=record.created_at,
        links={
            "self": f"{base}/api/files/{file_id}/",
            "measurements": f"{base}/api/files/{file_id}/measurements/",
            "features": f"{base}/api/files/{file_id}/features/",
        },
    )


async def _stream_to_disk(upload: UploadFile, destination: Path) -> int:
    """Write the upload to disk in chunks, enforcing the size limit as we go."""
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with destination.open("wb") as out:
            while chunk := await upload.read(CHUNK_SIZE):
                size += len(chunk)
                if size > settings.MAX_UPLOAD_BYTES:
                    raise FileTooLargeError(
                        "Uploaded file exceeds the maximum permitted size.",
                        detail={"limit_bytes": settings.MAX_UPLOAD_BYTES},
                    )
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)      # never leave a partial file
        raise
    finally:
        await upload.close()

    if size == 0:
        destination.unlink(missing_ok=True)
        raise EmptyFileError("Uploaded file is empty.")

    return size


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get(
    "/",
    response_model=FileListResponse,
    summary="List uploaded files",
)
def list_files(
    db: Session = Depends(get_db),
    limit: int | None = Query(default=None, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    file_status: ProcessingStatus | None = Query(default=None, alias="status"),
) -> FileListResponse:
    """Most recent uploads first."""
    limit = limit or settings.DEFAULT_PAGE_SIZE

    stmt = select(UploadedFile)
    count_stmt = select(func.count()).select_from(UploadedFile)
    if file_status is not None:
        stmt = stmt.where(UploadedFile.status == file_status)
        count_stmt = count_stmt.where(UploadedFile.status == file_status)

    total = db.scalar(count_stmt) or 0
    records = db.scalars(
        stmt.order_by(UploadedFile.created_at.desc()).limit(limit).offset(offset)
    ).all()

    return FileListResponse(
        pagination=Pagination(
            total=total, limit=limit, offset=offset, has_more=offset + len(records) < total
        ),
        files=[_to_info(r) for r in records],
    )


@router.get(
    "/{file_id}/",
    response_model=FileInfoResponse,
    summary="Get file information and processing status",
    responses=COMMON_ERRORS,
)
def get_file(file_id: str, db: Session = Depends(get_db)) -> FileInfoResponse:
    """Status, source CRS and feature count for one uploaded file."""
    return _to_info(_require_file(db, file_id))


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get measurements for every feature in a file",
    responses={**COMMON_ERRORS, 409: {"model": ErrorResponse, "description": "Still processing"}},
)
def get_measurements(
    file_id: str,
    db: Session = Depends(get_db),
    limit: int | None = Query(default=None, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    include_geometry: bool = Query(
        default=False,
        description="Include each feature's GeoJSON geometry (large responses)",
    ),
) -> MeasurementsResponse:
    """Per-feature area / length plus whole-file totals.

    Geometry is excluded by default: a survey file can carry millions of
    coordinates, and a client asking for measurements usually does not want
    them. Set `include_geometry=true` when you do.
    """
    record = _require_file(db, file_id)
    _require_completed(record)

    total = db.scalar(
        select(func.count()).select_from(Feature).where(Feature.file_id == file_id)
    ) or 0
    limit = limit or settings.DEFAULT_PAGE_SIZE

    page = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index)
        .limit(limit)
        .offset(offset)
    ).all()

    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        crs=record.source_crs,
        summary=_summarise(db, file_id),
        pagination=Pagination(
            total=total, limit=limit, offset=offset, has_more=offset + len(page) < total
        ),
        measurements=[
            FeatureMeasurementOut.from_feature(f, include_geometry=include_geometry)
            for f in page
        ],
    )


@router.get(
    "/{file_id}/features/",
    response_model=FeatureCollectionResponse,
    summary="Get features as a GeoJSON FeatureCollection",
    responses={**COMMON_ERRORS, 409: {"model": ErrorResponse, "description": "Still processing"}},
)
def get_features(
    file_id: str,
    db: Session = Depends(get_db),
    limit: int | None = Query(default=None, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> FeatureCollectionResponse:
    """Full feature detail as valid GeoJSON.

    Drops straight into Leaflet, Mapbox, QGIS or geopandas. Measurements are
    carried inside each feature's `properties` under the `_measurement` key,
    namespaced so it cannot collide with a source attribute.
    """
    record = _require_file(db, file_id)
    _require_completed(record)

    total = db.scalar(
        select(func.count()).select_from(Feature).where(Feature.file_id == file_id)
    ) or 0
    limit = limit or settings.DEFAULT_PAGE_SIZE

    page = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index)
        .limit(limit)
        .offset(offset)
    ).all()

    features = []
    for f in page:
        out = FeatureMeasurementOut.from_feature(f, include_geometry=True)
        features.append(
            {
                "type": "Feature",
                "id": f.source_id or f.feature_index,
                "geometry": f.geometry,
                "bbox": f.bbox,
                "properties": {
                    **(f.properties or {}),
                    "_feature_index": f.feature_index,
                    "_geometry_type": f.geometry_type,
                    "_supported": f.is_supported,
                    "_skip_reason": f.skip_reason,
                    "_measurement": out.measurement.model_dump(exclude_none=True),
                },
            }
        )

    return FeatureCollectionResponse(
        file_id=record.id,
        crs=record.source_crs,
        pagination=Pagination(
            total=total, limit=limit, offset=offset, has_more=offset + len(page) < total
        ),
        features=features,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{file_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a file and its features",
    responses=COMMON_ERRORS,
)
def delete_file(file_id: str, db: Session = Depends(get_db)) -> None:
    """Removes the database rows and the stored file from disk."""
    record = _require_file(db, file_id)

    stored = Path(record.stored_path)
    db.delete(record)          # features cascade
    db.commit()

    try:
        stored.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        logger.warning("Could not delete %s: %s", stored, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_file(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise ResourceNotFoundError(
            "No uploaded file with that id.", detail={"file_id": file_id}
        )
    return record


def _require_completed(record: UploadedFile) -> None:
    if record.status is ProcessingStatus.COMPLETED:
        return
    if record.status is ProcessingStatus.FAILED:
        raise FileNotReadyError(
            "Processing failed for this file; no measurements are available.",
            detail={"status": record.status.value, "error": record.error_message},
        )
    raise FileNotReadyError(
        "File is still being processed. Poll the file endpoint until status is COMPLETED.",
        detail={"status": record.status.value},
    )


def _to_info(record: UploadedFile) -> FileInfoResponse:
    return FileInfoResponse(
        id=record.id,
        filename=record.filename,
        file_type=record.file_type,
        size_bytes=record.size_bytes,
        status=record.status,
        crs=record.source_crs,
        crs_name=record.source_crs_name,
        layer_name=record.layer_name,
        feature_count=record.feature_count,
        error_message=record.error_message,
        warnings=record.warnings or [],
        created_at=record.created_at,
        updated_at=record.updated_at,
        processing_duration_seconds=record.processing_duration_seconds,
    )


def _summarise(db: Session, file_id: str) -> MeasurementSummary:
    """Aggregate totals.

    Computed in SQL rather than by loading every feature into Python, so the
    summary stays cheap for files with hundreds of thousands of features.
    """
    totals = db.execute(
        select(
            func.count(Feature.id),
            func.sum(func.coalesce(Feature.area_sqm, 0.0)),
            func.sum(func.coalesce(Feature.length_m, 0.0)),
            func.sum(case((Feature.is_supported.is_(False), 1), else_=0)),
        ).where(Feature.file_id == file_id)
    ).one()

    count, area_sum, length_sum, unsupported = totals
    count = int(count or 0)
    unsupported = int(unsupported or 0)
    area_sum = float(area_sum or 0.0)
    length_sum = float(length_sum or 0.0)

    type_rows = db.execute(
        select(Feature.geometry_type, func.count(Feature.id))
        .where(Feature.file_id == file_id)
        .group_by(Feature.geometry_type)
    ).all()

    method_rows = db.scalars(
        select(Feature.measurement_method)
        .where(
            Feature.file_id == file_id,
            Feature.measurement_method.is_not(None),
            Feature.measurement_method != "not_applicable",
        )
        .distinct()
    ).all()

    return MeasurementSummary(
        feature_count=count,
        measured_count=count - unsupported,
        unsupported_count=unsupported,
        geometry_type_counts={str(t): int(c) for t, c in type_rows},
        total_area_sqm=round(area_sum, 4) if area_sum else 0.0,
        total_area_hectares=round(area_sum / 10_000.0, 6) if area_sum else 0.0,
        total_area_sqkm=round(area_sum / 1_000_000.0, 8) if area_sum else 0.0,
        total_length_m=round(length_sum, 4) if length_sum else 0.0,
        total_length_km=round(length_sum / 1_000.0, 7) if length_sum else 0.0,
        measurement_methods_used=sorted(m for m in method_rows if m),
    )
