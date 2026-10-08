"""ORM models.

Two tables:

* ``uploaded_files``   -- one row per upload, carrying the processing state machine.
* ``features``         -- one row per feature found inside an uploaded file,
                          with its geometry, attributes and computed measurements.

Measurements are persisted rather than recomputed per request: parsing and
projecting a large survey file is expensive, and the result is immutable once
the source file is stored.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class ProcessingStatus(str, enum.Enum):
    """Lifecycle of an uploaded file."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FileType(str, enum.Enum):
    SHAPEFILE = "SHAPEFILE"
    KML = "KML"


class MeasurementType(str, enum.Enum):
    AREA = "area"
    LENGTH = "length"
    MIXED = "mixed"          # GeometryCollection containing both
    NONE = "none"            # valid geometry, but nothing to measure (Point)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[FileType] = mapped_column(Enum(FileType), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus), nullable=False, default=ProcessingStatus.PENDING, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    # Populated once parsing succeeds.
    source_crs: Mapped[str | None] = mapped_column(String(128))
    source_crs_name: Mapped[str | None] = mapped_column(String(255))
    layer_name: Mapped[str | None] = mapped_column(String(255))
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Feature.feature_index",
    )

    @property
    def processing_duration_seconds(self) -> float | None:
        if self.processing_started_at and self.processing_completed_at:
            return (
                self.processing_completed_at - self.processing_started_at
            ).total_seconds()
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UploadedFile {self.id} {self.filename} {self.status.value}>"


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        Index("ix_features_file_id_index", "file_id", "feature_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- Identity ---------------------------------------------------------
    feature_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255))

    # --- Geometry ---------------------------------------------------------
    geometry_type: Mapped[str] = mapped_column(String(64), nullable=False)
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON)   # GeoJSON, source CRS
    bbox: Mapped[list[float] | None] = mapped_column(JSON)          # [minx, miny, maxx, maxy]
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # --- Measurement ------------------------------------------------------
    is_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    measurement_type: Mapped[MeasurementType] = mapped_column(
        Enum(MeasurementType), nullable=False, default=MeasurementType.NONE
    )
    area_sqm: Mapped[float | None] = mapped_column(Float)
    perimeter_m: Mapped[float | None] = mapped_column(Float)
    length_m: Mapped[float | None] = mapped_column(Float)
    measurement_crs: Mapped[str | None] = mapped_column(String(128))
    measurement_method: Mapped[str | None] = mapped_column(String(64))
    skip_reason: Mapped[str | None] = mapped_column(Text)

    file: Mapped[UploadedFile] = relationship(back_populates="features")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Feature {self.file_id}#{self.feature_index} {self.geometry_type}>"
