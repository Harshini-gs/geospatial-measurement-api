#!/usr/bin/env python3
"""Generate sample files for exercising the API.

    python scripts/make_sample_data.py

Writes into ``sample_data/``:

* ``survey_plots.zip``     -- Shapefile (polygons) with a .prj, WGS84
* ``pipelines.zip``        -- Shapefile (lines) with a .prj, WGS84
* ``no_projection.zip``    -- Shapefile with the .prj deliberately removed,
                              to demonstrate graceful degradation
* ``survey_site.kml``      -- committed to the repo, not regenerated

Coordinates sit near Bengaluru so everything lands in UTM zone 43N
(EPSG:32643), which is what India's survey data normally uses.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

try:
    import shapefile  # pyshp
except ImportError:  # pragma: no cover
    sys.exit("pyshp is required: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_data"

# WGS84 geographic, as ESRI writes it into a .prj file.
WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)


def _zip_up(stem: Path, archive: Path, *, include_prj: bool = True) -> None:
    """Bundle a shapefile's parts into one archive."""
    extensions = [".shp", ".shx", ".dbf"] + ([".prj"] if include_prj else [])
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in extensions:
            part = stem.with_suffix(ext)
            if part.exists():
                zf.write(part, arcname=part.name)
    for ext in (".shp", ".shx", ".dbf", ".prj"):
        stem.with_suffix(ext).unlink(missing_ok=True)


def make_polygons() -> None:
    stem = OUT / "survey_plots"
    with shapefile.Writer(str(stem), shapeType=shapefile.POLYGON) as w:
        w.field("plot_id", "C", size=24)
        w.field("owner", "C", size=48)
        w.field("land_use", "C", size=24)

        # The Shapefile specification orders outer rings CLOCKWISE and inner
        # rings ANTICLOCKWISE; readers use ring orientation to tell a hole
        # from an island. pyshp does not reorder for you, so the winding
        # below is deliberate.

        # A simple square plot, ~0.01 deg on a side (~1.1 km).
        w.poly([[
            [77.5900, 12.9700], [77.5900, 12.9800],
            [77.6000, 12.9800], [77.6000, 12.9700], [77.5900, 12.9700],
        ]])
        w.record("PLOT-A1", "Site Operations", "industrial")

        # A plot with a hole.
        w.poly([
            # outer, clockwise
            [[77.6100, 12.9700], [77.6100, 12.9800],
             [77.6200, 12.9800], [77.6200, 12.9700], [77.6100, 12.9700]],
            # inner, anticlockwise
            [[77.6130, 12.9730], [77.6160, 12.9730],
             [77.6160, 12.9760], [77.6130, 12.9760], [77.6130, 12.9730]],
        ])
        w.record("PLOT-A2", "Site Operations", "storage")

        # A small triangle, clockwise.
        w.poly([[
            [77.6300, 12.9700], [77.6325, 12.9750],
            [77.6350, 12.9700], [77.6300, 12.9700],
        ]])
        w.record("PLOT-A3", "Contractor", "laydown")

    stem.with_suffix(".prj").write_text(WGS84_PRJ, encoding="utf-8")
    _zip_up(stem, OUT / "survey_plots.zip")
    print("wrote sample_data/survey_plots.zip        (3 polygons, EPSG:4326)")


def make_lines() -> None:
    stem = OUT / "pipelines"
    with shapefile.Writer(str(stem), shapeType=shapefile.POLYLINE) as w:
        w.field("asset_id", "C", size=24)
        w.field("material", "C", size=24)
        w.field("diameter", "N", decimal=0)

        w.line([[
            [77.5905, 12.9705], [77.5950, 12.9725],
            [77.5985, 12.9760], [77.5995, 12.9795],
        ]])
        w.record("PIPE-01", "steel", 450)

        # Multipart line -> MultiLineString, lengths summed.
        w.line([
            [[77.6010, 12.9710], [77.6060, 12.9710]],
            [[77.6080, 12.9730], [77.6120, 12.9780]],
        ])
        w.record("PIPE-02", "HDPE", 250)

    stem.with_suffix(".prj").write_text(WGS84_PRJ, encoding="utf-8")
    _zip_up(stem, OUT / "pipelines.zip")
    print("wrote sample_data/pipelines.zip           (2 lines, EPSG:4326)")


def make_missing_projection() -> None:
    """A shapefile with no .prj -- the most common real-world defect."""
    stem = OUT / "no_projection"
    with shapefile.Writer(str(stem), shapeType=shapefile.POLYGON) as w:
        w.field("name", "C", size=32)
        w.poly([[
            [77.5900, 12.9700], [77.5900, 12.9750],
            [77.5950, 12.9750], [77.5950, 12.9700], [77.5900, 12.9700],
        ]])
        w.record("Unprojected plot")

    _zip_up(stem, OUT / "no_projection.zip", include_prj=False)
    print("wrote sample_data/no_projection.zip       (no .prj -> warning path)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_polygons()
    make_lines()
    make_missing_projection()
    print("\nsurvey_site.kml is committed to the repo and is not regenerated.")


if __name__ == "__main__":
    main()
