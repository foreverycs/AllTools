"""Word (.docx / .doc) → PDF conversion."""

from .converter import (
    convert_to_pdf,
    engine_info,
    available_engines,
    ConversionError,
)

__all__ = [
    "convert_to_pdf",
    "engine_info",
    "available_engines",
    "ConversionError",
]
