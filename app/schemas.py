"""Pydantic request / response schemas.

Keeping these separate from the ORM models means the database layout can
change without silently changing the public API contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import FileType, MeasurementType, ProcessingStatus

_ORM = ConfigDict(from_attributes=True, use_enum_values=True)

# Square metres -> other units. Defined once so every response is consistent.
SQM_PER_HECTARE = 10_000.0
SQM_PER_SQKM = 1_000_000.0
M_PER_KM = 1_000.0


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    code: str = Field(..., examples=["invalid_archive"])
    message: str = Field(..., examples=["Archive does not contain a .shp file."])
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    """Returned by POST /api/files/ -- 202 Accepted, processing runs async."""

    model_config = _ORM

    id: str
    filename: str
    file_type: FileType
    size_bytes: int
    status: ProcessingStatus
    created_at: datetime
    message: str = "File accepted. Poll the status endpoint until status is COMPLETED."
    links: dict[str, str] = Field(default_factory=dict)


class FileInfoResponse(BaseModel):
    """Returned by GET /api/files/{id}/."""

    model_config = _ORM

    id: str
    filename: str
    file_type: FileType
    size_bytes: int
    status: ProcessingStatus
    crs: str | None = Field(None, description="Source CRS of the uploaded file, e.g. EPSG:4326")
    crs_name: str | None = None
    layer_name: str | None = None
    feature_count: int
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    processing_duration_seconds: float | None = None


class FileListResponse(BaseModel):
    pagination: "Pagination"
    files: list[FileInfoResponse]


# ---------------------------------------------------------------------------
# Features & measurements
# ---------------------------------------------------------------------------


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool


class MeasurementOut(BaseModel):
    """Measurement block for a single feature.

    ``area_*`` fields are populated for polygonal geometry, ``length_m`` for
    linear geometry, and both are ``null`` for points -- which is a valid
    result, not an error.
    """

    measurement_type: MeasurementType
    area_sqm: float | None = None
    area_hectares: float | None = None
    area_sqkm: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    length_km: float | None = None
    measurement_crs: str | None = Field(
        None, description="CRS the measurement was computed in (null for geodesic)"
    )
    measurement_method: str | None = Field(
        None,
        description="utm_projection | geodesic | source_projected_crs | not_applicable",
    )

    @classmethod
    def from_feature(cls, f: Any) -> "MeasurementOut":
        area = f.area_sqm
        length = f.length_m
        return cls(
            measurement_type=f.measurement_type,
            area_sqm=_round(area, 4),
            area_hectares=_round(area / SQM_PER_HECTARE if area is not None else None, 6),
            area_sqkm=_round(area / SQM_PER_SQKM if area is not None else None, 8),
            perimeter_m=_round(f.perimeter_m, 4),
            length_m=_round(length, 4),
            length_km=_round(length / M_PER_KM if length is not None else None, 7),
            measurement_crs=f.measurement_crs,
            measurement_method=f.measurement_method,
        )


class FeatureMeasurementOut(BaseModel):
    """One row of GET /api/files/{id}/measurements/."""

    feature_index: int
    feature_id: str | None
    geometry_type: str
    supported: bool
    skip_reason: str | None = None
    measurement: MeasurementOut
    properties: dict[str, Any] = Field(default_factory=dict)
    bbox: list[float] | None = None
    geometry: dict[str, Any] | None = Field(
        None, description="GeoJSON geometry in the source CRS; omitted unless requested"
    )

    @classmethod
    def from_feature(cls, f: Any, *, include_geometry: bool = False) -> "FeatureMeasurementOut":
        return cls(
            feature_index=f.feature_index,
            feature_id=f.source_id,
            geometry_type=f.geometry_type,
            supported=f.is_supported,
            skip_reason=f.skip_reason,
            measurement=MeasurementOut.from_feature(f),
            properties=f.properties or {},
            bbox=f.bbox,
            geometry=f.geometry if include_geometry else None,
        )


class MeasurementSummary(BaseModel):
    """Aggregate totals across every feature in the file."""

    feature_count: int
    measured_count: int
    unsupported_count: int
    geometry_type_counts: dict[str, int]
    total_area_sqm: float | None = None
    total_area_hectares: float | None = None
    total_area_sqkm: float | None = None
    total_length_m: float | None = None
    total_length_km: float | None = None
    measurement_methods_used: list[str] = Field(default_factory=list)


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    status: ProcessingStatus
    crs: str | None
    summary: MeasurementSummary
    pagination: Pagination
    measurements: list[FeatureMeasurementOut]


class FeatureCollectionResponse(BaseModel):
    """GET /api/files/{id}/features/ -- valid GeoJSON FeatureCollection.

    Returning real GeoJSON means the output drops straight into Leaflet,
    QGIS, Mapbox or geopandas with no translation step.
    """

    type: str = "FeatureCollection"
    file_id: str
    crs: str | None
    pagination: Pagination
    features: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    database: str


FileListResponse.model_rebuild()
