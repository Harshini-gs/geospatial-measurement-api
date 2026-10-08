"""End-to-end API tests.

Starlette's TestClient drains BackgroundTasks before returning the response,
so an upload is fully processed by the time the next request is issued.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import upload


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


def test_health(client: TestClient):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_api_root_advertises_capabilities(client: TestClient):
    body = client.get("/api").json()
    assert ".kml" in body["supported_extensions"]
    assert ".zip" in body["supported_extensions"]


def test_openapi_schema_is_served(client: TestClient):
    assert client.get("/openapi.json").status_code == 200


def test_frontend_is_served_at_root(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Upload pipeline -- Shapefile
# ---------------------------------------------------------------------------


def test_shapefile_upload_runs_the_full_pipeline(
    client: TestClient, polygon_shapefile_zip: Path
):
    accepted = upload(client, polygon_shapefile_zip)

    assert accepted["status_code"] == 202
    assert accepted["status"] == "PENDING"
    assert accepted["file_type"] == "SHAPEFILE"
    file_id = accepted["id"]

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["crs"] == "EPSG:4326"
    assert info["feature_count"] == 1
    assert info["error_message"] is None

    measurements = client.get(f"/api/files/{file_id}/measurements/").json()
    row = measurements["measurements"][0]

    assert row["geometry_type"] == "Polygon"
    assert row["supported"] is True
    assert row["measurement"]["measurement_type"] == "area"
    assert row["measurement"]["area_sqm"] > 1_000_000      # not square degrees
    assert row["measurement"]["measurement_crs"] == "EPSG:32643"
    assert row["measurement"]["measurement_method"] == "utm_projection"
    assert row["properties"]["plot_id"] == "PLOT-A1"


def test_response_matches_the_specification_example_shape(
    client: TestClient, polygon_shapefile_zip: Path
):
    """The spec's example body is id / filename / feature_count / crs / status."""
    file_id = upload(client, polygon_shapefile_zip)["id"]
    info = client.get(f"/api/files/{file_id}/").json()

    for key in ("id", "filename", "feature_count", "crs", "status"):
        assert key in info, f"missing required key: {key}"


def test_missing_prj_surfaces_a_warning(
    client: TestClient, shapefile_zip_without_prj: Path
):
    file_id = upload(client, shapefile_zip_without_prj)["id"]
    info = client.get(f"/api/files/{file_id}/").json()

    assert info["status"] == "COMPLETED"
    assert any("EPSG:4326" in w for w in info["warnings"])


# ---------------------------------------------------------------------------
# Upload pipeline -- KML
# ---------------------------------------------------------------------------


def test_kml_upload_measures_every_geometry_family(client: TestClient, sample_kml: Path):
    file_id = upload(client, sample_kml)["id"]

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["crs"] == "EPSG:4326"
    assert info["file_type"] == "KML"

    body = client.get(f"/api/files/{file_id}/measurements/").json()
    rows = {r["feature_id"]: r for r in body["measurements"]}

    polygon = rows["plot-A1"]
    assert polygon["measurement"]["area_sqm"] > 1_000_000
    assert polygon["measurement"]["perimeter_m"] > 0

    line = rows["pipeline-01"]
    assert line["measurement"]["measurement_type"] == "length"
    assert line["measurement"]["length_m"] > 0
    assert line["measurement"]["area_sqm"] is None

    point = rows["benchmark-01"]
    assert point["supported"] is True
    assert point["measurement"]["measurement_type"] == "none"
    assert point["measurement"]["area_sqm"] is None


def test_summary_totals_are_consistent(client: TestClient, sample_kml: Path):
    file_id = upload(client, sample_kml)["id"]
    body = client.get(f"/api/files/{file_id}/measurements/?limit=1000").json()

    summary = body["summary"]
    rows = body["measurements"]

    expected_area = sum(r["measurement"]["area_sqm"] or 0 for r in rows)

    assert summary["total_area_sqm"] == pytest.approx(expected_area, rel=1e-6)
    assert summary["feature_count"] == len(rows)
    assert summary["total_area_hectares"] == pytest.approx(
        expected_area / 10_000, rel=1e-6
    )
    assert "utm_projection" in summary["measurement_methods_used"]


# ---------------------------------------------------------------------------
# GeoJSON output
# ---------------------------------------------------------------------------


def test_features_endpoint_returns_valid_geojson(client: TestClient, sample_kml: Path):
    file_id = upload(client, sample_kml)["id"]
    body = client.get(f"/api/files/{file_id}/features/").json()

    assert body["type"] == "FeatureCollection"
    for feature in body["features"]:
        assert feature["type"] == "Feature"
        assert "geometry" in feature
        assert "properties" in feature
        assert "_measurement" in feature["properties"]


def test_measurements_omit_geometry_unless_requested(
    client: TestClient, polygon_shapefile_zip: Path
):
    file_id = upload(client, polygon_shapefile_zip)["id"]

    lean = client.get(f"/api/files/{file_id}/measurements/").json()
    assert lean["measurements"][0]["geometry"] is None

    full = client.get(
        f"/api/files/{file_id}/measurements/?include_geometry=true"
    ).json()
    assert full["measurements"][0]["geometry"]["type"] == "Polygon"


# ---------------------------------------------------------------------------
# Listing, pagination, deletion
# ---------------------------------------------------------------------------


def test_listing_and_pagination(client: TestClient, polygon_shapefile_zip: Path):
    for _ in range(3):
        upload(client, polygon_shapefile_zip)

    page = client.get("/api/files/?limit=2").json()
    assert page["pagination"]["total"] == 3
    assert page["pagination"]["has_more"] is True
    assert len(page["files"]) == 2

    rest = client.get("/api/files/?limit=2&offset=2").json()
    assert rest["pagination"]["has_more"] is False
    assert len(rest["files"]) == 1


def test_delete_removes_the_file(client: TestClient, polygon_shapefile_zip: Path):
    file_id = upload(client, polygon_shapefile_zip)["id"]

    assert client.delete(f"/api/files/{file_id}/").status_code == 204
    assert client.get(f"/api/files/{file_id}/").status_code == 404


# ---------------------------------------------------------------------------
# Error handling -- every failure returns the same envelope
# ---------------------------------------------------------------------------


def test_unknown_id_returns_404_envelope(client: TestClient):
    response = client.get("/api/files/does-not-exist/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unsupported_extension_returns_415(client: TestClient):
    response = client.post(
        "/api/files/", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 415
    body = response.json()
    assert body["error"]["code"] == "unsupported_file_type"
    assert ".kml" in body["error"]["detail"]["supported_extensions"]


def test_empty_file_returns_400(client: TestClient):
    response = client.post(
        "/api/files/", files={"file": ("empty.kml", b"", "application/vnd.google-earth.kml+xml")}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_missing_file_field_returns_422_envelope(client: TestClient):
    response = client.post("/api/files/")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_corrupt_archive_is_recorded_as_failed_not_crashed(client: TestClient):
    """A bad file must mark the record FAILED, never return a 500."""
    response = client.post(
        "/api/files/", files={"file": ("broken.zip", b"not a zip at all", "application/zip")}
    )
    assert response.status_code == 202          # accepted, then fails async
    file_id = response.json()["id"]

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert info["error_message"]


def test_measurements_for_a_failed_file_return_409(client: TestClient):
    file_id = client.post(
        "/api/files/", files={"file": ("broken.zip", b"nope", "application/zip")}
    ).json()["id"]

    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "file_not_ready"


def test_zip_slip_upload_is_rejected_at_processing_time(client: TestClient):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../../escape.shp", b"\x00" * 10)
    buffer.seek(0)

    file_id = client.post(
        "/api/files/", files={"file": ("evil.zip", buffer.read(), "application/zip")}
    ).json()["id"]

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert "unsafe path" in info["error_message"].lower()
