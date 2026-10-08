"""Parser and archive-safety tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core.exceptions import (
    InvalidArchiveError,
    InvalidGeospatialFileError,
    UnsupportedFileTypeError,
)
from app.models import FileType
from app.services.parsers import KmlParser, ShapefileParser, detect_file_type
from app.services.parsers.archive import safe_extract


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("plots.zip", FileType.SHAPEFILE),
        ("PLOTS.ZIP", FileType.SHAPEFILE),
        ("survey.kml", FileType.KML),
        ("survey.KML", FileType.KML),
        ("survey.kmz", FileType.KML),
    ],
)
def test_detect_file_type(filename, expected):
    assert detect_file_type(filename) is expected


@pytest.mark.parametrize("filename", ["notes.txt", "image.png", "data.geojson", "archive.tar.gz"])
def test_unknown_extensions_are_rejected(filename):
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(filename)


# ---------------------------------------------------------------------------
# Archive safety
# ---------------------------------------------------------------------------


def test_zip_slip_is_blocked(tmp_path: Path):
    """A member escaping the extraction root must be refused."""
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escaped.txt", "pwned")

    with pytest.raises(InvalidArchiveError, match="unsafe path"):
        safe_extract(archive, tmp_path / "out")

    assert not (tmp_path.parent / "escaped.txt").exists()


def test_absolute_path_member_is_blocked(tmp_path: Path):
    archive = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "root:x:0:0")

    with pytest.raises(InvalidArchiveError):
        safe_extract(archive, tmp_path / "out")


def test_zip_bomb_ratio_is_blocked(tmp_path: Path, monkeypatch):
    """Highly compressible padding must trip the ratio guard."""
    from app.config import settings

    monkeypatch.setattr(settings, "MAX_COMPRESSION_RATIO", 10)

    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\0" * (2 * 1024 * 1024))

    with pytest.raises(InvalidArchiveError, match="compression ratio"):
        safe_extract(archive, tmp_path / "out")


def test_uncompressed_size_limit_is_enforced(tmp_path: Path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAX_UNCOMPRESSED_BYTES", 1024)
    monkeypatch.setattr(settings, "MAX_COMPRESSION_RATIO", 10_000)

    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"\0" * 10_000)

    with pytest.raises(InvalidArchiveError, match="uncompressed size"):
        safe_extract(archive, tmp_path / "out")


def test_corrupt_archive_is_rejected(tmp_path: Path):
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"this is definitely not a zip file")

    with pytest.raises(InvalidArchiveError, match="readable ZIP"):
        safe_extract(archive, tmp_path / "out")


# ---------------------------------------------------------------------------
# Shapefile parser
# ---------------------------------------------------------------------------


def test_shapefile_parses_geometry_crs_and_attributes(polygon_shapefile_zip: Path):
    dataset = ShapefileParser().parse(polygon_shapefile_zip)

    assert len(dataset.features) == 1
    assert dataset.crs.to_epsg() == 4326

    feature = dataset.features[0]
    assert feature.geometry_type == "Polygon"
    assert feature.properties["plot_id"] == "PLOT-A1"
    assert feature.source_id == "PLOT-A1"
    assert not dataset.warnings


def test_missing_prj_warns_and_assumes_wgs84(shapefile_zip_without_prj: Path):
    dataset = ShapefileParser().parse(shapefile_zip_without_prj)

    assert dataset.crs.to_epsg() == 4326
    assert any("EPSG:4326" in w for w in dataset.warnings)


def test_archive_without_a_shp_is_rejected(tmp_path: Path):
    archive = tmp_path / "wrong.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "no shapefile here")

    with pytest.raises(InvalidArchiveError, match=r"\.shp"):
        ShapefileParser().parse(archive)


def test_shapefile_missing_sidecars_is_rejected(tmp_path: Path):
    archive = tmp_path / "partial.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("plots.shp", b"\x00" * 100)

    with pytest.raises(InvalidArchiveError, match="sidecar"):
        ShapefileParser().parse(archive)


def test_shapefile_nested_in_a_folder_is_found(tmp_path: Path, polygon_shapefile_zip: Path):
    """Real-world zips usually wrap the shapefile in a directory."""
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(polygon_shapefile_zip) as source, \
         zipfile.ZipFile(nested, "w") as target:
        for name in source.namelist():
            target.writestr(f"exports/survey/{name}", source.read(name))

    dataset = ShapefileParser().parse(nested)
    assert len(dataset.features) == 1


# ---------------------------------------------------------------------------
# KML parser
# ---------------------------------------------------------------------------


def test_kml_parses_every_supported_geometry(sample_kml: Path):
    dataset = KmlParser().parse(sample_kml)

    assert dataset.crs.to_epsg() == 4326           # fixed by the KML spec
    assert dataset.layer_name == "Sample Survey Site"

    kinds = {f.geometry_type for f in dataset.features}
    assert {"Polygon", "LineString", "MultiLineString", "Point", "MultiPolygon"} <= kinds


def test_kml_reads_nested_folders(sample_kml: Path):
    """Placemarks inside <Folder> must not be missed."""
    dataset = KmlParser().parse(sample_kml)
    ids = {f.source_id for f in dataset.features}
    assert "pipeline-01" in ids                     # this one lives in a Folder


def test_kml_extracts_extended_data(sample_kml: Path):
    dataset = KmlParser().parse(sample_kml)
    plot = next(f for f in dataset.features if f.source_id == "plot-A1")

    assert plot.properties["owner"] == "Site Operations"
    assert plot.properties["land_use"] == "industrial"
    assert plot.properties["name"] == "Plot A1"


def test_kml_reads_schemadata_simple_fields(sample_kml: Path):
    dataset = KmlParser().parse(sample_kml)
    pipe = next(f for f in dataset.features if f.source_id == "pipeline-01")

    assert pipe.properties["material"] == "steel"
    assert pipe.properties["diameter_mm"] == "450"


def test_kml_polygon_inner_boundary_becomes_a_hole(sample_kml: Path):
    dataset = KmlParser().parse(sample_kml)
    plot = next(f for f in dataset.features if f.source_id == "plot-A1")

    assert len(plot.geometry.interiors) == 1


def test_kml_altitude_is_dropped(sample_kml: Path):
    """Measurements are planimetric, so Z must not reach the geometry."""
    dataset = KmlParser().parse(sample_kml)
    for feature in dataset.features:
        if feature.geometry is not None:
            assert not feature.geometry.has_z


def test_kml_namespace_variants_are_handled(tmp_path: Path):
    """KML 2.0, 2.2 and no-namespace documents must all parse."""
    bodies = [
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<kml xmlns="http://earth.google.com/kml/2.0">',
        "<kml>",
    ]
    for i, opening in enumerate(bodies):
        path = tmp_path / f"variant_{i}.kml"
        path.write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>{opening}<Document>'
            "<Placemark><name>P</name><Point>"
            "<coordinates>77.5,12.9,0</coordinates>"
            "</Point></Placemark></Document></kml>",
            encoding="utf-8",
        )
        dataset = KmlParser().parse(path)
        assert len(dataset.features) == 1
        assert dataset.features[0].geometry_type == "Point"


def test_malformed_xml_is_rejected(tmp_path: Path):
    path = tmp_path / "broken.kml"
    path.write_text("<kml><Document><Placemark>", encoding="utf-8")

    with pytest.raises(InvalidGeospatialFileError, match="well-formed"):
        KmlParser().parse(path)


def test_one_bad_placemark_does_not_kill_the_file(tmp_path: Path):
    """A LineString with a single position is invalid; its neighbours are not."""
    path = tmp_path / "mixed.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>good</name><Point>"
        "<coordinates>77.5,12.9</coordinates></Point></Placemark>"
        "<Placemark><name>bad</name><LineString>"
        "<coordinates>77.5,12.9</coordinates></LineString></Placemark>"
        "<Placemark><name>also good</name><Point>"
        "<coordinates>77.6,12.8</coordinates></Point></Placemark>"
        "</Document></kml>",
        encoding="utf-8",
    )

    dataset = KmlParser().parse(path)

    assert len(dataset.features) == 3
    assert dataset.features[1].parse_error is not None
    assert dataset.features[0].geometry is not None
    assert dataset.features[2].geometry is not None


def test_kmz_archive_is_read(tmp_path: Path, sample_kml: Path):
    kmz = tmp_path / "survey.kmz"
    with zipfile.ZipFile(kmz, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", sample_kml.read_bytes())

    dataset = KmlParser().parse(kmz)
    assert len(dataset.features) > 0


def test_unsupported_gx_track_is_flagged_not_crashed(tmp_path: Path):
    path = tmp_path / "track.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2" '
        'xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>'
        "<Placemark><name>flight</name><gx:Track>"
        "<gx:coord>77.5 12.9 0</gx:coord></gx:Track></Placemark>"
        "</Document></kml>",
        encoding="utf-8",
    )

    dataset = KmlParser().parse(path)

    assert len(dataset.features) == 1
    assert dataset.features[0].geometry is None
    assert "not supported" in dataset.features[0].parse_error
