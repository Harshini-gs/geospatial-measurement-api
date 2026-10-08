"""Safe ZIP extraction.

An uploaded archive is untrusted input. Two classic attacks apply:

* **Zip-slip** -- a member named ``../../etc/cron.d/x`` escapes the extraction
  directory and overwrites arbitrary files.
* **Zip-bomb** -- a few kilobytes that expand to gigabytes and exhaust the disk.

Both are checked before a single byte is written.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.config import settings
from app.core.exceptions import InvalidArchiveError


def safe_extract(archive_path: Path, destination: Path) -> list[Path]:
    """Extract ``archive_path`` into ``destination``, returning written files."""
    destination.mkdir(parents=True, exist_ok=True)
    dest_root = destination.resolve()

    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise InvalidArchiveError("Uploaded file is not a readable ZIP archive.") from exc

    written: list[Path] = []
    with zf:
        total_uncompressed = 0

        # Pass 1: validate every member before extracting anything.
        for info in zf.infolist():
            if info.is_dir():
                continue

            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise InvalidArchiveError(
                    "Archive contains an unsafe path.", detail={"member": name}
                )

            target = (destination / name).resolve()
            if not _is_within(target, dest_root):
                raise InvalidArchiveError(
                    "Archive member escapes the extraction directory.",
                    detail={"member": name},
                )

            total_uncompressed += info.file_size
            if total_uncompressed > settings.MAX_UNCOMPRESSED_BYTES:
                raise InvalidArchiveError(
                    "Archive expands beyond the permitted uncompressed size.",
                    detail={
                        "limit_bytes": settings.MAX_UNCOMPRESSED_BYTES,
                        "exceeded_at_member": name,
                    },
                )

            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > settings.MAX_COMPRESSION_RATIO:
                    raise InvalidArchiveError(
                        "Archive member has a suspicious compression ratio.",
                        detail={
                            "member": name,
                            "ratio": round(ratio, 1),
                            "limit": settings.MAX_COMPRESSION_RATIO,
                        },
                    )

        # Pass 2: extract.
        for info in zf.infolist():
            if info.is_dir():
                continue
            extracted = zf.extract(info, path=destination)
            written.append(Path(extracted))

    if not written:
        raise InvalidArchiveError("Archive is empty.")

    return written


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def find_by_extension(root: Path, extension: str) -> list[Path]:
    """Recursively find files with ``extension`` (e.g. ``'.shp'``).

    Recursive because real-world zips very often wrap the shapefile in a
    folder, and macOS adds a ``__MACOSX`` sidecar directory which we skip.
    """
    ext = extension.lower()
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() == ext
        and "__MACOSX" not in p.parts
        and not p.name.startswith("._")
    )
