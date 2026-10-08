"""Application exception hierarchy.

Every error the API returns derives from :class:`AppError`, which carries the
HTTP status and a stable machine-readable ``code``. Clients can branch on
``code`` without string-matching human-readable messages.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected (non-bug) application errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return {"error": payload}


# --- 4xx -------------------------------------------------------------------


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "unsupported_file_type"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class EmptyFileError(AppError):
    status_code = 400
    code = "empty_file"


class InvalidArchiveError(AppError):
    """Archive is corrupt, unsafe (zip-slip / zip-bomb) or missing members."""

    status_code = 400
    code = "invalid_archive"


class InvalidGeospatialFileError(AppError):
    """File is well-formed as an archive but not valid geospatial data."""

    status_code = 422
    code = "invalid_geospatial_file"


class ResourceNotFoundError(AppError):
    status_code = 404
    code = "not_found"


class FileNotReadyError(AppError):
    """Processing has not finished, so measurements are not available yet."""

    status_code = 409
    code = "file_not_ready"


# --- 5xx -------------------------------------------------------------------


class ProcessingError(AppError):
    status_code = 500
    code = "processing_failed"
