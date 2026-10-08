"""Parser interface.

Adding a new format (GeoJSON, GPX, GeoPackage) means writing one subclass and
registering it. Nothing in the API layer, the measurement engine or the
processor changes -- they only ever see :class:`ParsedDataset`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


@dataclass
class ParsedFeature:
    """A single feature as read from the source file, before measurement."""

    index: int
    geometry: BaseGeometry | None
    geometry_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    parse_error: str | None = None


@dataclass
class ParsedDataset:
    """Everything a parser extracts from one file."""

    features: list[ParsedFeature]
    crs: CRS
    layer_name: str | None = None
    warnings: list[str] = field(default_factory=list)


class GeospatialParser(ABC):
    """Base class for all format parsers."""

    format_name: ClassVar[str]
    extensions: ClassVar[tuple[str, ...]]

    @abstractmethod
    def parse(self, path: Path) -> ParsedDataset:
        """Read ``path`` and return its features and CRS.

        Implementations must raise :class:`~app.core.exceptions.AppError`
        subclasses for malformed input, and must record per-feature problems
        in ``ParsedFeature.parse_error`` rather than aborting the whole file.
        """

    @classmethod
    def handles(cls, filename: str) -> bool:
        lowered = filename.lower()
        return any(lowered.endswith(ext) for ext in cls.extensions)
