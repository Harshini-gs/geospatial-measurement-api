"""Shared test fixtures.

Environment variables are set *before* importing the application so the
settings singleton picks up an isolated temporary database and storage
directory. Tests never touch the developer's real data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="geoapi_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_ROOT / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_TMP_ROOT / "storage")
os.environ["LOG_LEVEL"] = "WARNING"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import create_app  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"

WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)


@pytest.fixture(scope="session", autouse=True)
def _cleanup() -> None:
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture
def client() -> TestClient:
    """A TestClient against a fresh schema.

    Starlette's TestClient runs BackgroundTasks to completion before the
    response is handed back, so uploads are already processed by the time the
    next request is made. That makes the async pipeline deterministic to test.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def tmp_path_factory_dir(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# File builders
# ---------------------------------------------------------------------------


@pytest.fixture
def polygon_shapefile_zip(tmp_path: Path) -> Path:
    """A one-polygon shapefile, zipped, with a .prj declaring WGS84."""
    import shapefile

    stem = tmp_path / "plots"
    with shapefile.Writer(str(stem), shapeType=shapefile.POLYGON) as w:
        w.field("plot_id", "C", size=16)
        # Outer rings are clockwise per the Shapefile specification.
        w.poly([[
            [77.5900, 12.9700], [77.5900, 12.9800],
            [77.6000, 12.9800], [77.6000, 12.9700], [77.5900, 12.9700],
        ]])
        w.record("PLOT-A1")

    stem.with_suffix(".prj").write_text(WGS84_PRJ, encoding="utf-8")
    return _zip_shapefile(stem, tmp_path / "plots.zip")


@pytest.fixture
def shapefile_zip_without_prj(tmp_path: Path) -> Path:
    import shapefile

    stem = tmp_path / "noprj"
    with shapefile.Writer(str(stem), shapeType=shapefile.POLYGON) as w:
        w.field("name", "C", size=16)
        w.poly([[
            [77.5900, 12.9700], [77.5900, 12.9750],
            [77.5950, 12.9750], [77.5950, 12.9700], [77.5900, 12.9700],
        ]])
        w.record("plot")

    return _zip_shapefile(stem, tmp_path / "noprj.zip", extensions=(".shp", ".shx", ".dbf"))


@pytest.fixture
def sample_kml() -> Path:
    path = SAMPLE_DIR / "survey_site.kml"
    if not path.exists():  # pragma: no cover
        pytest.skip("sample_data/survey_site.kml is missing")
    return path


def _zip_shapefile(
    stem: Path,
    archive: Path,
    extensions: tuple[str, ...] = (".shp", ".shx", ".dbf", ".prj"),
) -> Path:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in extensions:
            part = stem.with_suffix(ext)
            if part.exists():
                zf.write(part, arcname=part.name)
    return archive


def upload(client: TestClient, path: Path, *, filename: str | None = None) -> dict:
    """Upload a file and return the parsed JSON body."""
    with path.open("rb") as fh:
        response = client.post(
            "/api/files/",
            files={"file": (filename or path.name, fh, "application/octet-stream")},
        )
    return {"status_code": response.status_code, **response.json()}
