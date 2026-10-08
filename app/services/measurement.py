"""Measurement engine.

Computes area for polygonal geometry and length for linear geometry, always
in metres, using the CRS strategy chosen in :mod:`app.services.crs`.

Design notes
------------
* Dispatch is by geometry family, not by an if/elif ladder over every concrete
  type, so Multi* variants are handled by the same code path as their singular
  forms.
* Unsupported input never raises out of :func:`measure`. It returns a result
  with ``supported=False`` and a human-readable ``skip_reason``, so one bad
  feature cannot fail the whole file.
* Measurements are 2D. Z coordinates are present in many drone-derived files
  but "area" in a cadastral or survey context means planimetric area, not
  surface area draped over terrain. Called out in the README.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyproj import CRS, Geod, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from app.models import MeasurementType
from app.services.crs import (
    WGS84,
    CrsPlan,
    crs_to_string,
    linear_unit_to_metres,
    plan_for_geographic_geometry,
)

# WGS84 ellipsoid; matches the datum of EPSG:4326 input.
_GEOD = Geod(ellps="WGS84")

POLYGONAL = {"Polygon", "MultiPolygon"}
LINEAR = {"LineString", "MultiLineString", "LinearRing"}
PUNCTUAL = {"Point", "MultiPoint"}

# Transformers are expensive to build and safe to reuse.
_transformer_cache: dict[tuple[str, str], Transformer] = {}


def _get_transformer(src: CRS, dst: CRS) -> Transformer:
    key = (src.to_wkt(), dst.to_wkt())
    cached = _transformer_cache.get(key)
    if cached is None:
        # always_xy=True forces (longitude, latitude) ordering. Without it,
        # pyproj honours the authority's axis order, which for EPSG:4326 is
        # (lat, lon) -- the single most common source of silently wrong
        # coordinates in geospatial code.
        cached = Transformer.from_crs(src, dst, always_xy=True)
        _transformer_cache[key] = cached
    return cached


def _reproject(geom: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    if src.equals(dst):
        return geom
    transformer = _get_transformer(src, dst)
    return shapely_transform(transformer.transform, geom)


@dataclass
class Measurement:
    """Outcome of measuring one feature."""

    supported: bool = True
    measurement_type: MeasurementType = MeasurementType.NONE
    area_sqm: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    measurement_crs: str | None = None
    method: str | None = None
    skip_reason: str | None = None
    notes: list[str] = field(default_factory=list)


def measure(geom: BaseGeometry | None, source_crs: CRS) -> Measurement:
    """Measure a single geometry. Never raises for bad input."""
    if geom is None:
        return Measurement(
            supported=False,
            method="not_applicable",
            skip_reason="Feature has no geometry.",
        )

    try:
        if geom.is_empty:
            return Measurement(
                supported=False,
                method="not_applicable",
                skip_reason="Geometry is empty.",
            )

        gtype = geom.geom_type

        if gtype in PUNCTUAL:
            # Per the specification, points require no measurement. This is a
            # successful outcome, not a failure.
            return Measurement(
                supported=True,
                measurement_type=MeasurementType.NONE,
                method="not_applicable",
                skip_reason=None,
                notes=["Point geometry carries no area or length by definition."],
            )

        if gtype == "GeometryCollection":
            return _measure_collection(geom, source_crs)

        if gtype not in POLYGONAL and gtype not in LINEAR:
            return Measurement(
                supported=False,
                method="not_applicable",
                skip_reason=f"Geometry type '{gtype}' is not supported for measurement.",
            )

        return _measure_simple(geom, source_crs)

    except Exception as exc:  # noqa: BLE001 - deliberate catch-all boundary
        return Measurement(
            supported=False,
            method="error",
            skip_reason=f"Measurement failed: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _measure_simple(geom: BaseGeometry, source_crs: CRS) -> Measurement:
    """Measure polygonal or linear geometry."""
    is_area = geom.geom_type in POLYGONAL

    # --- Case 1: source is already a projected (planar) CRS ---------------
    if source_crs.is_projected:
        factor = linear_unit_to_metres(source_crs)
        return _planar_measurement(
            geom,
            is_area=is_area,
            unit_factor=factor,
            crs_string=crs_to_string(source_crs),
            method="source_projected_crs",
            notes=[
                f"Measured directly in the file's own projected CRS "
                f"({crs_to_string(source_crs)}); linear unit factor {factor} m."
            ],
        )

    # --- Case 2 & 3: source is geographic -> must project or go geodesic --
    geom_wgs84 = _reproject(geom, source_crs, WGS84)
    plan: CrsPlan = plan_for_geographic_geometry(geom_wgs84)

    if plan.strategy == "geodesic":
        return _geodesic_measurement(geom_wgs84, is_area=is_area, notes=[plan.rationale])

    assert plan.target_crs is not None
    projected = _reproject(geom_wgs84, WGS84, plan.target_crs)
    return _planar_measurement(
        projected,
        is_area=is_area,
        unit_factor=linear_unit_to_metres(plan.target_crs),
        crs_string=plan.crs_string,
        method="utm_projection",
        notes=[plan.rationale],
    )


def _planar_measurement(
    geom: BaseGeometry,
    *,
    is_area: bool,
    unit_factor: float,
    crs_string: str | None,
    method: str,
    notes: list[str],
) -> Measurement:
    """Shapely planar maths, scaled from CRS units into metres."""
    if is_area:
        return Measurement(
            measurement_type=MeasurementType.AREA,
            area_sqm=geom.area * unit_factor**2,
            perimeter_m=geom.length * unit_factor,
            measurement_crs=crs_string,
            method=method,
            notes=notes,
        )
    return Measurement(
        measurement_type=MeasurementType.LENGTH,
        length_m=geom.length * unit_factor,
        measurement_crs=crs_string,
        method=method,
        notes=notes,
    )


def _geodesic_measurement(
    geom_wgs84: BaseGeometry, *, is_area: bool, notes: list[str]
) -> Measurement:
    """Exact ellipsoidal measurement; no projection, no zone limits."""
    if is_area:
        # Returns a signed area (sign follows ring orientation) plus the
        # perimeter, both in metres on the WGS84 ellipsoid.
        area, perimeter = _GEOD.geometry_area_perimeter(geom_wgs84)
        return Measurement(
            measurement_type=MeasurementType.AREA,
            area_sqm=abs(area),
            perimeter_m=abs(perimeter),
            measurement_crs=None,
            method="geodesic",
            notes=[*notes, "Computed on the WGS84 ellipsoid; no projection applied."],
        )
    return Measurement(
        measurement_type=MeasurementType.LENGTH,
        length_m=abs(_GEOD.geometry_length(geom_wgs84)),
        measurement_crs=None,
        method="geodesic",
        notes=[*notes, "Computed on the WGS84 ellipsoid; no projection applied."],
    )


def _measure_collection(geom: BaseGeometry, source_crs: CRS) -> Measurement:
    """Aggregate a GeometryCollection rather than rejecting it.

    Mixed collections are legal in both Shapefile-derived and KML data
    (a KML <MultiGeometry> may hold a polygon and a line together). Summing
    the parts is more useful than refusing the feature.
    """
    total_area = 0.0
    total_perimeter = 0.0
    total_length = 0.0
    saw_area = False
    saw_length = False
    methods: list[str] = []
    crs_used: str | None = None

    for part in geom.geoms:
        sub = measure(part, source_crs)
        if not sub.supported:
            continue
        if sub.area_sqm is not None:
            total_area += sub.area_sqm
            total_perimeter += sub.perimeter_m or 0.0
            saw_area = True
        if sub.length_m is not None:
            total_length += sub.length_m
            saw_length = True
        if sub.method and sub.method != "not_applicable":
            methods.append(sub.method)
            crs_used = crs_used or sub.measurement_crs

    if not saw_area and not saw_length:
        return Measurement(
            supported=True,
            measurement_type=MeasurementType.NONE,
            method="not_applicable",
            notes=["GeometryCollection contained no measurable members."],
        )

    if saw_area and saw_length:
        mtype = MeasurementType.MIXED
    elif saw_area:
        mtype = MeasurementType.AREA
    else:
        mtype = MeasurementType.LENGTH

    return Measurement(
        supported=True,
        measurement_type=mtype,
        area_sqm=total_area if saw_area else None,
        perimeter_m=total_perimeter if saw_area else None,
        length_m=total_length if saw_length else None,
        measurement_crs=crs_used,
        method=methods[0] if methods else "not_applicable",
        notes=[f"Aggregated across {len(geom.geoms)} collection members."],
    )
