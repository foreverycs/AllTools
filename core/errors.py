"""Unified error hierarchy for the toolkit.

Raise these from any layer; the global exception handler in app.py maps them
to the correct HTTP status code automatically.
"""

from __future__ import annotations


class ToolkitError(Exception):
    """Base for all application errors (HTTP 500)."""

    status_code: int = 500
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None, *, status_code: int | None = None):
        self.detail = detail or self.__class__.detail
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.detail)


class ValidationError(ToolkitError):
    """Bad input from the client (HTTP 400)."""

    status_code = 400
    detail = "Invalid input"


class FileTooLargeError(ValidationError):
    """Uploaded file exceeds size limit (HTTP 413)."""

    status_code = 413
    detail = "File too large"


class UnsupportedFormatError(ValidationError):
    """File type not supported (HTTP 400)."""

    detail = "Unsupported file format"


class PDFParseError(ToolkitError):
    """PDF could not be read or parsed (HTTP 422)."""

    status_code = 422
    detail = "Failed to parse PDF"


class ConversionError(ToolkitError):
    """Document conversion failed (HTTP 500)."""

    detail = "Conversion failed"


class EngineNotFoundError(ToolkitError):
    """Required engine (LibreOffice/Tesseract) not available (HTTP 503)."""

    status_code = 503
    detail = "Conversion engine not available"


class RateLimitError(ToolkitError):
    """Too many requests (HTTP 429)."""

    status_code = 429
    detail = "Too many requests, please try again later"
