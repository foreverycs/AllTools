"""Word (.docx / .doc) → PDF conversion."""

from core.errors import (
    ConversionError,
    EngineNotFoundError,
    UnsupportedFormatError,
    ValidationError,
)

from .converter import (
    available_engines,
    convert_to_pdf,
    engine_info,
)

__all__ = [
    "convert_to_pdf",
    "engine_info",
    "available_engines",
    "ConversionError",
    "EngineNotFoundError",
    "UnsupportedFormatError",
    "ValidationError",
]
