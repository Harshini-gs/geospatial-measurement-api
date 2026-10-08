"""CRS selection tests.

The UTM zone assertions use real cities whose zones are independently
documented, so a regression here is caught against ground truth rather than
against whatever the code happened to return when the test was written.
"""

from __future__ import annotations

import pytest
from pyproj import CRS
from shapely.geometry import Point, Polygon, box

from app.services.crs import (
    crs_to_string,
    linear_unit_to_metres,
    plan_for_geographic_geometry,
    utm_epsg_for,
)


@pytest.mark.parametrize(
    ("lon", "lat", "expected", "place"),
    [
        (77.5946, 12.9716, 32643, "Bengaluru -> 43N"),
        (72.8777, 19.0760, 32643, "Mumbai -> 43N"),
        (-0.1276, 51.5074, 32630, "London -> 30N"),
        (-74.0060, 40.7128, 32618, "New York -> 18N"),
        (151.2093, -33.8688, 32756, "Sydney -> 56S"),
        (-58.3816, -34.6037, 32721, "Buenos Aires -> 21S"),
        (0.0, 0.0, 32631, "Null Island -> 31N"),
        (-180.0, 10.0, 32601, "western edge -> zone 1"),
        (179.999, 10.0, 32660, "eastern edge -> zone 60"),
        # Boundaries are half-open, so exactly 180 normalises to -180 and
        # belongs to zone 1 rather than closing out zone 60.
        (180.0, 10.0, 32601, "exactly 180 wraps to zone 1"),
        (-186.0, 10.0, 32660, "out-of-range longitude wraps correctly"),
    ],
)
def test_utm_zone_matches_known_values(lon, lat, expected, place):
    assert utm_epsg_for(lon, lat) == expected, place


def test_hemisphere_changes_the_epsg_prefix():
    north = utm_epsg_for(77.6, 12.97)
    south = utm_epsg_for(77.6, -12.97)
    assert north == 32643
    assert south == 32743
    assert north - 32600 == south - 32700  # same zone number


def test_equator_counts_as_northern():
    assert utm_epsg_for(77.6, 0.0) == 32643


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def test_local_feature_uses_utm():
    plan = plan_for_geographic_geometry(box(77.59, 12.97, 77.60, 12.98))
    assert plan.strategy == "utm_projection"
    assert plan.crs_string == "EPSG:32643"


def test_feature_spanning_many_zones_falls_back_to_geodesic():
    # 40 degrees of longitude cannot sit in one 6-degree UTM zone.
    plan = plan_for_geographic_geometry(box(60.0, 10.0, 100.0, 20.0))
    assert plan.strategy == "geodesic"
    assert plan.target_crs is None
    assert "longitude" in plan.rationale


def test_polar_feature_falls_back_to_geodesic():
    plan = plan_for_geographic_geometry(box(10.0, 85.0, 10.5, 86.0))
    assert plan.strategy == "geodesic"
    assert "UTM validity" in plan.rationale


def test_anchor_point_lies_inside_a_concave_feature():
    """A centroid can fall outside a C-shaped polygon; the anchor must not.

    If the anchor escaped the geometry it could select a neighbouring UTM
    zone, so this guards a subtle source of wrong projections.
    """
    c_shape = Polygon([
        (77.50, 12.90), (77.70, 12.90), (77.70, 12.95),
        (77.55, 12.95), (77.55, 13.05), (77.70, 13.05),
        (77.70, 13.10), (77.50, 13.10),
    ])
    assert not c_shape.contains(c_shape.centroid)          # premise holds
    assert c_shape.contains(c_shape.representative_point())

    plan = plan_for_geographic_geometry(c_shape)
    assert plan.strategy == "utm_projection"


# ---------------------------------------------------------------------------
# Units and formatting
# ---------------------------------------------------------------------------


def test_metric_crs_has_unit_factor_of_one():
    assert linear_unit_to_metres(CRS.from_epsg(32643)) == pytest.approx(1.0)


def test_us_survey_foot_crs_converts_to_metres():
    # EPSG:2229 -- California State Plane Zone 5, US survey feet.
    factor = linear_unit_to_metres(CRS.from_epsg(2229))
    assert factor == pytest.approx(0.3048006, rel=1e-5)


def test_crs_to_string_prefers_epsg_codes():
    assert crs_to_string(CRS.from_epsg(4326)) == "EPSG:4326"
    assert crs_to_string(None) is None


def test_point_geometry_still_produces_a_plan():
    plan = plan_for_geographic_geometry(Point(77.5946, 12.9716))
    assert plan.strategy == "utm_projection"
    assert plan.crs_string == "EPSG:32643"
