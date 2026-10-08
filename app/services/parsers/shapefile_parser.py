"""Shapefile parser (``.zip`` containing ``.shp`` + sidecars).

Uses ``pyshp``, a pure-Python reader, rather than GDAL/OGR. A shapefile is
really a small family of files:

===========  ====================================================
``.shp``     geometry                              (required)
``.shx``     positional index into the ``.shp``    (required)
``.dbf``     attribute table, dBase III format     (required)
``.prj``     the coordinate reference system, WKT  (optional)
``.cpg``     the ``.dbf`` character encoding       (optional)
===========  ====================================================

A missing ``.prj`` is the single most common defect in real uploads. The spec
says to handle files gracefully, so we assume EPSG:4326 and attach a loud
warning to the response rather than refusing the file or, worse, silently
measuring in the wrong CRS.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import shapefile  # pyshp
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import shape as shapely_shape

from app.core.exceptions import InvalidArchiveError, InvalidGeospatialFileError
from app.core.logging import get_logger
from app.services.parsers.archive import find_by_extension, safe_extract
from app.services.parsers.base import GeospatialParser, ParsedDataset, ParsedFeature

logger = get_logger(__name__)

REQUIRED_SIDECARS = (".shx", ".dbf")

# pyshp shapeType 0 is the explicit "null shape" sentinel.
NULL_SHAPE_TYPE = 0


class ShapefileParser(GeospatialParser):
    format_name = "SHAPEFILE"
    extensions = (".zip",)

    def parse(self, path: Path) -> ParsedDataset:
        with tempfile.TemporaryDirectory(prefix="shp_") as tmp:
            workdir = Path(tmp)
            safe_extract(path, workdir)
            shp_path = self._locate_shp(workdir)
            return self._read(shp_path)

    # --- Locating the dataset inside the archive -------------------------

    def _locate_shp(self, root: Path) -> Path:
        candidates = find_by_extension(root, ".shp")
        if not candidates:
            raise InvalidArchiveError(
                "Archive does not contain a .shp file.",
                detail={"hint": "Zip the .shp, .shx, .dbf and .prj files together."},
            )
        if len(candidates) > 1:
            logger.warning(
                "Archive contains %d shapefiles; using %s",
                len(candidates),
                candidates[0].name,
            )

        shp = candidates[0]
        missing = [
            ext for ext in REQUIRED_SIDECARS if not _sibling(shp, ext).exists()
        ]
        if missing:
            raise InvalidArchiveError(
                "Shapefile is missing required sidecar files.",
                detail={"shapefile": shp.name, "missing": missing},
            )
        return shp

    # --- Reading ----------------------------------------------------------

    def _read(self, shp_path: Path) -> ParsedDataset:
        warnings: list[str] = []
        crs = self._read_projection(shp_path, warnings)

        try:
            reader = shapefile.Reader(
                str(shp_path.with_suffix("")),
                encoding=self._read_encoding(shp_path),
                encodingErrors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            raise InvalidGeospatialFileError(
                f"Could not read shapefile: {exc}"
            ) from exc

        features: list[ParsedFeature] = []
        with reader:
            # reader.fields[0] is the internal DeletionFlag; skip it.
            field_names = [f[0] for f in reader.fields[1:]]

            for index, record in enumerate(reader.iterShapeRecords()):
                features.append(
                    self._build_feature(index, record, field_names)
                )

        if not features:
            warnings.append("Shapefile contains no features.")

        return ParsedDataset(
            features=features,
            crs=crs,
            layer_name=shp_path.stem,
            warnings=warnings,
        )

    def _build_feature(
        self, index: int, record: Any, field_names: list[str]
    ) -> ParsedFeature:
        properties = self._clean_record(record.record, field_names)
        source_id = _derive_source_id(properties)

        shape = record.shape
        if getattr(shape, "shapeType", None) == NULL_SHAPE_TYPE:
            return ParsedFeature(
                index=index,
                geometry=None,
                geometry_type="Null",
                properties=properties,
                source_id=source_id,
                parse_error="Record holds a null shape.",
            )

        try:
            geo_interface = shape.__geo_interface__
            geometry = shapely_shape(geo_interface)
            geometry_type = geometry.geom_type
            parse_error = None
        except Exception as exc:  # noqa: BLE001 - one bad record must not kill the file
            logger.warning("Feature %d is unreadable: %s", index, exc)
            geometry = None
            geometry_type = "Invalid"
            parse_error = f"Unreadable geometry: {type(exc).__name__}: {exc}"

        return ParsedFeature(
            index=index,
            geometry=geometry,
            geometry_type=geometry_type,
            properties=properties,
            source_id=source_id,
            parse_error=parse_error,
        )

    # --- Helpers ----------------------------------------------------------

    @staticmethod
    def _read_projection(shp_path: Path, warnings: list[str]) -> CRS:
        prj = _sibling(shp_path, ".prj")
        if not prj.exists():
            warnings.append(
                "No .prj file found. Assuming EPSG:4326 (WGS84 lon/lat). "
                "Measurements may be wrong if the data is in another CRS."
            )
            return CRS.from_epsg(4326)

        try:
            wkt = prj.read_text(encoding="utf-8", errors="replace").strip()
            return CRS.from_wkt(wkt)
        except (CRSError, OSError, ValueError) as exc:
            warnings.append(
                f"Could not interpret the .prj file ({exc}). Assuming EPSG:4326."
            )
            return CRS.from_epsg(4326)

    @staticmethod
    def _read_encoding(shp_path: Path) -> str:
        """Honour a ``.cpg`` sidecar if present, else default to UTF-8."""
        cpg = _sibling(shp_path, ".cpg")
        if cpg.exists():
            try:
                declared = cpg.read_text(encoding="ascii", errors="ignore").strip()
                if declared:
                    return declared
            except OSError:  # pragma: no cover
                pass
        return "utf-8"

    @staticmethod
    def _clean_record(values: Any, field_names: list[str]) -> dict[str, Any]:
        """Coerce dBase values into JSON-serialisable Python types."""
        import datetime as _dt

        cleaned: dict[str, Any] = {}
        for name, value in zip(field_names, list(values)):
            if isinstance(value, (_dt.date, _dt.datetime)):
                cleaned[name] = value.isoformat()
            elif isinstance(value, bytes):
                cleaned[name] = value.decode("utf-8", errors="replace")
            elif isinstance(value, (int, float, bool, str)) or value is None:
                cleaned[name] = value
            else:
                cleaned[name] = str(value)
        return cleaned


def _sibling(shp_path: Path, extension: str) -> Path:
    """Sidecar path, tolerating upper-case extensions on case-sensitive disks."""
    lower = shp_path.with_suffix(extension)
    if lower.exists():
        return lower
    upper = shp_path.with_suffix(extension.upper())
    return upper if upper.exists() else lower


_ID_FIELDS = ("id", "fid", "gid", "objectid", "uid", "uuid")
_NAME_FIELDS = ("name", "title", "label")


def _derive_source_id(properties: dict[str, Any]) -> str | None:
    """Pick a human-meaningful identifier from the attribute table.

    A shapefile's .dbf has no standard identifier column, so this is a
    heuristic applied in priority order:

    1. a well-known identifier field -- id, fid, gid, objectid, uid, uuid
    2. any field ending in ``_id`` -- plot_id, asset_id, parcel_id, ...
    3. a descriptive label -- name, title, label

    Rule 2 matters in practice: real survey data almost never has a bare
    ``id`` column, it has a domain-specific one. Matching is case-insensitive
    because .dbf field names arrive upper-case from some toolchains and
    lower-case from others.

    Returns None when nothing suitable exists. The caller always has
    ``feature_index`` as a fallback identifier, so this is never load-bearing.
    """
    # First spelling of each name wins, following .dbf field order.
    by_lower: dict[str, str] = {}
    for key in properties:
        by_lower.setdefault(key.lower(), key)

    def value_of(original_key: str) -> str | None:
        value = properties.get(original_key)
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None

    for candidate in _ID_FIELDS:
        original = by_lower.get(candidate)
        if original and (found := value_of(original)):
            return found

    for lower, original in by_lower.items():
        if lower.endswith("_id") and (found := value_of(original)):
            return found

    for candidate in _NAME_FIELDS:
        original = by_lower.get(candidate)
        if original and (found := value_of(original)):
            return found

    return None
