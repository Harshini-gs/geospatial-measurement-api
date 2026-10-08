"""Coordinate Reference System selection.

The central requirement of this service is that measurements are never
computed from latitude/longitude degrees. A degree of longitude is ~111 km at
the equator and 0 km at the poles, so planar maths on EPSG:4326 produces
numbers in "square degrees", which are meaningless.

Strategy, applied **per feature** rather than per file:

1. **Source CRS is already projected** -- measure directly in it, converting
   the CRS's linear unit to metres. Re-projecting would only add error.

2. **Source CRS is geographic, feature is local** -- project to the UTM zone
   containing the feature's centroid. UTM is conformal with scale error under
   ~1/1000 inside its 6-degree zone: the right tool for survey-sized geometry,
   which is what drone and site data actually looks like.

3. **Source CRS is geographic, feature is large or polar** -- fall back to
   geodesic computation on the WGS84 ellipsoid (Karney's algorithms, via
   ``pyproj.Geod``). This is exact at any size and has no zone limits, but is
   slower, so it is not the default.

Deciding per feature matters: a single file can hold a plot in Karnataka and
a plot in Queensland. A per-file CRS would measure one of them badly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.config import settings

WGS84 = CRS.from_epsg(4326)

MeasurementStrategy = Literal["utm_projection", "geodesic", "source_projected_crs"]


@dataclass(frozen=True)
class CrsPlan:
    """How a single feature should be measured."""

    strategy: MeasurementStrategy
    target_crs: CRS | None          # None for geodesic
    rationale: str

    @property
    def crs_string(self) -> str | None:
        return crs_to_string(self.target_crs) if self.target_crs else None


def crs_to_string(crs: CRS | None) -> str | None:
    """Render a CRS as ``EPSG:xxxx`` when possible, else its name."""
    if crs is None:
        return None
    epsg = crs.to_epsg()
    if epsg is not None:
        return f"EPSG:{epsg}"
    return crs.name or "UNKNOWN"


def utm_epsg_for(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone containing ``(lon, lat)``.

    Zones are 6 degrees wide, numbered 1..60 eastward from 180W.
    Northern hemisphere codes are 326xx, southern are 327xx.

    Zone boundaries are treated as half-open ``[start, end)``, so a point on a
    boundary belongs to the zone that starts there. Longitude is normalised
    first, which means exactly 180.0 wraps to -180.0 and lands in zone 1 (the
    zone beginning at the antimeridian) rather than closing out zone 60.

    Note: the Norway (zone 32V) and Svalbard zone-widening exceptions are not
    implemented. They affect a small band of northern Europe and would shift a
    feature into a neighbouring zone, not break the result. See README.
    """
    lon = _normalise_longitude(lon)
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    # Normalisation already constrains this to 1..60; the clamp is a defensive
    # guard against floating-point drift at the extremes.
    zone = max(1, min(60, zone))
    base = 32600 if lat >= 0 else 32700
    return base + zone


def _normalise_longitude(lon: float) -> float:
    """Wrap longitude into [-180, 180)."""
    if -180.0 <= lon < 180.0:
        return lon
    return ((lon + 180.0) % 360.0) - 180.0


def plan_for_geographic_geometry(geom: BaseGeometry) -> CrsPlan:
    """Choose UTM or geodesic for a geometry whose coordinates are lon/lat."""
    min_lon, min_lat, max_lon, max_lat = geom.bounds

    lon_span = abs(max_lon - min_lon)
    max_abs_lat = max(abs(min_lat), abs(max_lat))

    if max_abs_lat > settings.UTM_MAX_ABS_LATITUDE:
        return CrsPlan(
            strategy="geodesic",
            target_crs=None,
            rationale=(
                f"Feature reaches latitude {max_abs_lat:.3f} deg, outside the UTM "
                f"validity band of +/-{settings.UTM_MAX_ABS_LATITUDE} deg."
            ),
        )

    if lon_span > settings.MAX_UTM_LON_SPAN_DEGREES:
        return CrsPlan(
            strategy="geodesic",
            target_crs=None,
            rationale=(
                f"Feature spans {lon_span:.3f} deg of longitude, exceeding the "
                f"{settings.MAX_UTM_LON_SPAN_DEGREES} deg single-zone threshold."
            ),
        )

    # Representative point is guaranteed to lie inside the geometry, unlike a
    # centroid, which can fall outside a crescent-shaped or multipart feature.
    try:
        anchor = geom.representative_point()
        lon, lat = anchor.x, anchor.y
    except Exception:  # pragma: no cover - degenerate geometry
        lon = (min_lon + max_lon) / 2.0
        lat = (min_lat + max_lat) / 2.0

    epsg = utm_epsg_for(lon, lat)
    hemisphere = "north" if lat >= 0 else "south"
    return CrsPlan(
        strategy="utm_projection",
        target_crs=CRS.from_epsg(epsg),
        rationale=(
            f"Feature lies within UTM zone {epsg - (32600 if lat >= 0 else 32700)}"
            f" ({hemisphere}); projected to EPSG:{epsg} for planar measurement."
        ),
    )


def linear_unit_to_metres(crs: CRS) -> float:
    """Metres per one linear unit of ``crs``.

    Projected CRSs are not always metric -- US State Plane zones commonly use
    US survey feet. Multiplying by this factor keeps every response in metres.
    """
    try:
        axis = crs.axis_info[0]
        factor = getattr(axis, "unit_conversion_factor", None)
        if factor:
            return float(factor)
    except (IndexError, AttributeError):  # pragma: no cover - exotic CRS
        pass
    return 1.0
