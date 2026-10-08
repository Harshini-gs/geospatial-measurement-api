"""Measurement engine tests.

The important ones are:

* :func:`test_area_is_not_computed_in_square_degrees` -- the specific failure
  mode the specification calls out.
* :func:`test_utm_and_geodesic_agree_for_a_local_polygon` -- two mathematically
  independent methods cross-validating each other. If either were wrong they
  would not agree to four significant figures.
"""

from __future__ import annotations

import math

import pytest
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.models import MeasurementType
from app.services.measurement import measure

WGS84 = CRS.from_epsg(4326)
UTM43N = CRS.from_epsg(32643)

# A ~1.1 km x 1.1 km plot near Bengaluru, in lon/lat.
PLOT = box(77.5900, 12.9700, 77.6000, 12.9800)


# ---------------------------------------------------------------------------
# The core requirement
# ---------------------------------------------------------------------------


def test_area_is_not_computed_in_square_degrees():
    """The plot is 0.0001 square *degrees*. A correct answer is ~1.2e6 m²."""
    naive_degree_area = PLOT.area
    assert naive_degree_area == pytest.approx(1e-4)

    result = measure(PLOT, WGS84)

    assert result.supported
    assert result.measurement_type is MeasurementType.AREA
    assert result.area_sqm > 1_000_000        # square kilometres, not degrees
    assert result.area_sqm / naive_degree_area > 1e9


def test_utm_and_geodesic_agree_for_a_local_polygon():
    """Cross-validate the projected path against the ellipsoidal one."""
    from pyproj import Geod

    projected = measure(PLOT, WGS84)
    assert projected.method == "utm_projection"

    geodesic_area, _ = Geod(ellps="WGS84").geometry_area_perimeter(PLOT)

    assert projected.area_sqm == pytest.approx(abs(geodesic_area), rel=1e-3)


def test_local_polygon_selects_the_right_utm_zone():
    result = measure(PLOT, WGS84)
    assert result.measurement_crs == "EPSG:32643"
    assert result.method == "utm_projection"


def test_continent_sized_polygon_uses_geodesic():
    huge = box(60.0, 5.0, 100.0, 35.0)
    result = measure(huge, WGS84)

    assert result.method == "geodesic"
    assert result.measurement_crs is None
    assert result.area_sqm > 1e13      # roughly a tenth of Asia


# ---------------------------------------------------------------------------
# Projected source CRS
# ---------------------------------------------------------------------------


def test_projected_source_is_measured_in_place_and_is_exact():
    """A 100 m x 200 m rectangle in a metric CRS must be exactly 20 000 m²."""
    rect = Polygon([(0, 0), (100, 0), (100, 200), (0, 200)])
    result = measure(rect, UTM43N)

    assert result.method == "source_projected_crs"
    assert result.measurement_crs == "EPSG:32643"
    assert result.area_sqm == pytest.approx(20_000.0)
    assert result.perimeter_m == pytest.approx(600.0)


def test_non_metric_projected_crs_is_converted_to_metres():
    """EPSG:2229 is in US survey feet; a 100 ft square is ~929 m²."""
    square_in_feet = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    result = measure(square_in_feet, CRS.from_epsg(2229))

    expected = (100 * 0.3048006) ** 2
    assert result.area_sqm == pytest.approx(expected, rel=1e-4)


# ---------------------------------------------------------------------------
# Geometry types
# ---------------------------------------------------------------------------


def test_linestring_returns_length_and_no_area():
    line = LineString([(77.5900, 12.9700), (77.6000, 12.9700)])
    result = measure(line, WGS84)

    assert result.measurement_type is MeasurementType.LENGTH
    assert result.area_sqm is None
    # 0.01 deg of longitude at 12.97 N is roughly 1085 m.
    assert result.length_m == pytest.approx(1085, abs=15)


def test_multipolygon_sums_component_areas():
    a = box(77.60, 12.97, 77.61, 12.98)
    b = box(77.62, 12.97, 77.63, 12.98)

    combined = measure(MultiPolygon([a, b]), WGS84)
    separate = measure(a, WGS84).area_sqm + measure(b, WGS84).area_sqm

    assert combined.area_sqm == pytest.approx(separate, rel=1e-6)


def test_multilinestring_sums_component_lengths():
    a = LineString([(77.59, 12.97), (77.60, 12.97)])
    b = LineString([(77.61, 12.97), (77.62, 12.97)])

    combined = measure(MultiLineString([a, b]), WGS84)
    separate = measure(a, WGS84).length_m + measure(b, WGS84).length_m

    assert combined.length_m == pytest.approx(separate, rel=1e-6)


def test_polygon_with_hole_excludes_the_hole():
    outer = [(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)]
    hole = [(77.593, 12.973), (77.595, 12.973), (77.595, 12.975), (77.593, 12.975)]

    solid = measure(Polygon(outer), WGS84).area_sqm
    with_hole = measure(Polygon(outer, [hole]), WGS84).area_sqm

    assert with_hole < solid
    assert with_hole > 0


@pytest.mark.parametrize(
    "geometry",
    [Point(77.5946, 12.9716), MultiPoint([(77.59, 12.97), (77.60, 12.98)])],
)
def test_points_are_supported_but_carry_no_measurement(geometry):
    """Per the specification, points need no measurement -- that is success."""
    result = measure(geometry, WGS84)

    assert result.supported is True
    assert result.measurement_type is MeasurementType.NONE
    assert result.area_sqm is None
    assert result.length_m is None
    assert result.skip_reason is None


def test_geometry_collection_aggregates_mixed_members():
    collection = GeometryCollection([
        box(77.59, 12.97, 77.60, 12.98),
        LineString([(77.61, 12.97), (77.62, 12.97)]),
    ])
    result = measure(collection, WGS84)

    assert result.supported
    assert result.measurement_type is MeasurementType.MIXED
    assert result.area_sqm > 0
    assert result.length_m > 0


# ---------------------------------------------------------------------------
# Graceful failure -- measure() must never raise
# ---------------------------------------------------------------------------


def test_missing_geometry_is_reported_not_raised():
    result = measure(None, WGS84)
    assert result.supported is False
    assert "no geometry" in result.skip_reason.lower()


def test_empty_geometry_is_reported_not_raised():
    result = measure(Polygon(), WGS84)
    assert result.supported is False
    assert "empty" in result.skip_reason.lower()


def test_self_intersecting_polygon_still_returns_a_number():
    """A bow-tie is invalid under OGC rules but must not crash the request."""
    bowtie = Polygon([
        (77.59, 12.97), (77.60, 12.98), (77.60, 12.97), (77.59, 12.98),
    ])
    result = measure(bowtie, WGS84)

    assert result.area_sqm is not None
    assert math.isfinite(result.area_sqm)
