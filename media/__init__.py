"""媒体处理工具核心逻辑。"""

from .image_compress import (
    CompressError,
    compress_image,
    detect_format,
    supported_formats,
)
from .image_convert import (
    ConvertError,
    convert_image,
    detect_format as convert_detect_format,
    input_formats,
    output_formats,
)
from .image_to_pdf import (
    ImageToPdfError,
    images_to_pdf,
    input_formats as image_to_pdf_input_formats,
    max_images as image_to_pdf_max_images,
    orientations as image_to_pdf_orientations,
    page_modes as image_to_pdf_page_modes,
)
from .image_grid import (
    ImageGridError,
    build_grid_preview,
    max_dim,
    split_image,
    supported_formats as image_grid_supported_formats,
)

__all__ = [
    "compress_image",
    "detect_format",
    "supported_formats",
    "CompressError",
    "convert_image",
    "convert_detect_format",
    "input_formats",
    "output_formats",
    "ConvertError",
    "images_to_pdf",
    "ImageToPdfError",
    "image_to_pdf_input_formats",
    "image_to_pdf_max_images",
    "image_to_pdf_orientations",
    "image_to_pdf_page_modes",
    "ImageGridError",
    "build_grid_preview",
    "max_dim",
    "split_image",
    "image_grid_supported_formats",
]
