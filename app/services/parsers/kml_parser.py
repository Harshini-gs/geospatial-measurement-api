"""KML / KMZ parser.

Written directly against the XML rather than delegating to GDAL's KML driver.
Three reasons:

1. **No GDAL dependency.** The whole point of this stack is that it installs
   from wheels on any OS. Pulling in GDAL just to read an XML file would undo
   that.
2. **Namespace tolerance.** Files in the wild declare KML 2.0, 2.1, 2.2, or
   Google's ``gx`` extensions, sometimes with no namespace at all. Matching on
   the local tag name handles every variant; a namespace-bound parser does not.
3. **Correct CRS by construction.** OGC KML is defined to use WGS84
   longitude/latitude/altitude. There is no projection to guess at, so the
   source CRS is always EPSG:4326.

Supported geometry: Point, LineString, LinearRing, Polygon (with inner
boundaries), and MultiGeometry. ``gx:Track`` and ``gx:MultiTrack`` are
recognised and reported as unsupported rather than silently dropped.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from app.core.exceptions import InvalidArchiveError, InvalidGeospatialFileError
from app.core.logging import get_logger
from app.services.parsers.base import GeospatialParser, ParsedDataset, ParsedFeature

logger = get_logger(__name__)

GEOMETRY_TAGS = {
    "Point",
    "LineString",
    "LinearRing",
    "Polygon",
    "MultiGeometry",
    "Model",
    "Track",
    "MultiTrack",
}
UNSUPPORTED_GEOMETRY_TAGS = {"Model", "Track", "MultiTrack"}

# A LinearRing needs 4 positions (first == last); a LineString needs 2.
MIN_RING_POSITIONS = 4
MIN_LINE_POSITIONS = 2


class KmlParser(GeospatialParser):
    format_name = "KML"
    extensions = (".kml", ".kmz")

    def parse(self, path: Path) -> ParsedDataset:
        xml_bytes = (
            self._read_kmz(path)
            if path.suffix.lower() == ".kmz"
            else path.read_bytes()
        )

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise InvalidGeospatialFileError(
                f"File is not well-formed XML: {exc}"
            ) from exc

        warnings: list[str] = []
        features: list[ParsedFeature] = []

        placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
        if not placemarks:
            warnings.append("No <Placemark> elements found in the KML document.")

        for index, placemark in enumerate(placemarks):
            features.append(self._build_feature(index, placemark))

        return ParsedDataset(
            features=features,
            crs=CRS.from_epsg(4326),  # fixed by the OGC KML specification
            layer_name=self._document_name(root) or path.stem,
            warnings=warnings,
        )

    # --- KMZ --------------------------------------------------------------

    @staticmethod
    def _read_kmz(path: Path) -> bytes:
        try:
            with zipfile.ZipFile(path) as zf:
                names = [
                    n for n in zf.namelist()
                    if n.lower().endswith(".kml") and not n.startswith("__MACOSX")
                ]
                if not names:
                    raise InvalidArchiveError("KMZ archive contains no .kml file.")
                # doc.kml is the conventional entry point.
                chosen = next(
                    (n for n in names if Path(n).name.lower() == "doc.kml"), names[0]
                )
                return zf.read(chosen)
        except zipfile.BadZipFile as exc:
            raise InvalidArchiveError("KMZ file is not a readable ZIP archive.") from exc

    # --- Feature construction --------------------------------------------

    def _build_feature(self, index: int, placemark: ET.Element) -> ParsedFeature:
        properties = self._extract_properties(placemark)
        source_id = placemark.get("id") or properties.get("name") or None

        geometry_element = self._first_geometry(placemark)
        if geometry_element is None:
            return ParsedFeature(
                index=index,
                geometry=None,
                geometry_type="None",
                properties=properties,
                source_id=source_id,
                parse_error="Placemark contains no geometry.",
            )

        tag = _local(geometry_element.tag)
        if tag in UNSUPPORTED_GEOMETRY_TAGS:
            return ParsedFeature(
                index=index,
                geometry=None,
                geometry_type=tag,
                properties=properties,
                source_id=source_id,
                parse_error=f"KML <{tag}> geometry is not supported for measurement.",
            )

        try:
            geometry = self._to_shapely(geometry_element)
            if geometry is None:
                raise ValueError("geometry produced no coordinates")
            return ParsedFeature(
                index=index,
                geometry=geometry,
                geometry_type=geometry.geom_type,
                properties=properties,
                source_id=source_id,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad placemark
            logger.warning("Placemark %d is unreadable: %s", index, exc)
            return ParsedFeature(
                index=index,
                geometry=None,
                geometry_type=tag,
                properties=properties,
                source_id=source_id,
                parse_error=f"Unreadable geometry: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _first_geometry(placemark: ET.Element) -> ET.Element | None:
        for child in placemark:
            if _local(child.tag) in GEOMETRY_TAGS:
                return child
        return None

    def _to_shapely(self, element: ET.Element) -> BaseGeometry | None:
        """Recursively convert a KML geometry element into a Shapely object."""
        tag = _local(element.tag)

        if tag == "Point":
            coords = self._coords_of(element)
            return Point(coords[0]) if coords else None

        if tag in ("LineString", "LinearRing"):
            coords = self._coords_of(element)
            minimum = MIN_RING_POSITIONS if tag == "LinearRing" else MIN_LINE_POSITIONS
            if len(coords) < minimum:
                raise ValueError(
                    f"<{tag}> needs at least {minimum} positions, found {len(coords)}"
                )
            return LineString(coords)

        if tag == "Polygon":
            return self._build_polygon(element)

        if tag == "MultiGeometry":
            return self._build_multi(element)

        raise ValueError(f"Unhandled KML geometry element <{tag}>")

    def _build_polygon(self, element: ET.Element) -> Polygon:
        outer: list[tuple[float, float]] | None = None
        inners: list[list[tuple[float, float]]] = []

        for child in element:
            name = _local(child.tag)
            if name == "outerBoundaryIs":
                outer = self._ring_coords(child)
            elif name == "innerBoundaryIs":
                ring = self._ring_coords(child)
                if ring:
                    inners.append(ring)

        if not outer or len(outer) < MIN_RING_POSITIONS:
            raise ValueError("<Polygon> has no usable <outerBoundaryIs> ring")

        return Polygon(outer, inners)

    def _ring_coords(self, boundary: ET.Element) -> list[tuple[float, float]]:
        for child in boundary:
            if _local(child.tag) == "LinearRing":
                return self._coords_of(child)
        return []

    def _build_multi(self, element: ET.Element) -> BaseGeometry | None:
        parts: list[BaseGeometry] = []
        for child in element:
            if _local(child.tag) not in GEOMETRY_TAGS:
                continue
            if _local(child.tag) in UNSUPPORTED_GEOMETRY_TAGS:
                continue
            try:
                part = self._to_shapely(child)
            except ValueError:
                continue
            if part is not None and not part.is_empty:
                parts.append(part)

        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]

        kinds = {p.geom_type for p in parts}
        if kinds == {"Polygon"}:
            return MultiPolygon(parts)
        if kinds == {"LineString"}:
            return MultiLineString(parts)
        if kinds == {"Point"}:
            return MultiPoint(parts)
        # Genuinely heterogeneous -- the measurement engine aggregates these.
        return GeometryCollection(parts)

    @staticmethod
    def _coords_of(element: ET.Element) -> list[tuple[float, float]]:
        """Parse the first descendant ``<coordinates>`` element.

        KML coordinates are whitespace-separated ``lon,lat[,alt]`` tuples.
        Altitude is dropped: these are planimetric measurements.
        """
        node = next(
            (e for e in element.iter() if _local(e.tag) == "coordinates"), None
        )
        if node is None or not node.text:
            return []

        positions: list[tuple[float, float]] = []
        for token in node.text.split():
            parts = token.split(",")
            if len(parts) < 2:
                continue
            try:
                positions.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
        return positions

    # --- Attributes -------------------------------------------------------

    @staticmethod
    def _extract_properties(placemark: ET.Element) -> dict[str, Any]:
        """Collect <name>, <description>, <address> and all <ExtendedData>."""
        properties: dict[str, Any] = {}

        for child in placemark:
            name = _local(child.tag)
            if name in ("name", "description", "address", "phoneNumber"):
                text = (child.text or "").strip()
                if text:
                    properties[name] = text

        for extended in placemark.iter():
            if _local(extended.tag) != "ExtendedData":
                continue
            for node in extended.iter():
                local = _local(node.tag)
                # <Data name="k"><value>v</value></Data>
                if local == "Data":
                    key = node.get("name")
                    value_node = next(
                        (c for c in node if _local(c.tag) == "value"), None
                    )
                    if key and value_node is not None:
                        properties[key] = (value_node.text or "").strip()
                # <SchemaData><SimpleData name="k">v</SimpleData></SchemaData>
                elif local == "SimpleData":
                    key = node.get("name")
                    if key:
                        properties[key] = (node.text or "").strip()

        return properties

    @staticmethod
    def _document_name(root: ET.Element) -> str | None:
        for element in root.iter():
            if _local(element.tag) == "Document":
                for child in element:
                    if _local(child.tag) == "name" and child.text:
                        return child.text.strip()
                break
        return None


def _local(tag: str) -> str:
    """Strip the XML namespace: ``{http://...}Placemark`` -> ``Placemark``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
