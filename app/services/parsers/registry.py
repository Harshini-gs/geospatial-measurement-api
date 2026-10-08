"""Parser registry.

Maps an uploaded filename to the parser that can read it. Registering a new
format is a one-line change here plus the parser class itself -- no edits to
routes, services or models.
"""

from __future__ import annotations

from pathlib import Path

from app.core.exceptions import UnsupportedFileTypeError
from app.models import FileType
from app.services.parsers.base import GeospatialParser
from app.services.parsers.kml_parser import KmlParser
from app.services.parsers.shapefile_parser import ShapefileParser

_PARSERS: tuple[type[GeospatialParser], ...] = (
    ShapefileParser,
    KmlParser,
)

_FILE_TYPE_BY_FORMAT = {
    ShapefileParser.format_name: FileType.SHAPEFILE,
    KmlParser.format_name: FileType.KML,
}


def supported_extensions() -> list[str]:
    extensions: list[str] = []
    for parser in _PARSERS:
        extensions.extend(parser.extensions)
    return sorted(set(extensions))


def detect_file_type(filename: str) -> FileType:
    """Resolve an upload's filename to a :class:`FileType`."""
    for parser in _PARSERS:
        if parser.handles(filename):
            return _FILE_TYPE_BY_FORMAT[parser.format_name]

    raise UnsupportedFileTypeError(
        "Unsupported file type.",
        detail={
            "filename": filename,
            "supported_extensions": supported_extensions(),
            "hint": "Upload a .zip containing a Shapefile, or a .kml/.kmz file.",
        },
    )


def get_parser(file_type: FileType) -> GeospatialParser:
    for parser in _PARSERS:
        if _FILE_TYPE_BY_FORMAT[parser.format_name] is file_type:
            return parser()
    raise UnsupportedFileTypeError(f"No parser registered for {file_type}.")


def get_parser_for_filename(filename: str) -> GeospatialParser:
    return get_parser(detect_file_type(Path(filename).name))
