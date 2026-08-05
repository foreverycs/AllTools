"""编码 / 编解码工具核心逻辑。"""

from .base64_codec import (
    DecodeError,
    decode_base64,
    encode_base64,
    probe_base64,
)
from .code_format import (
    FormatError,
    JsonError,
    format_code,
    format_json,
    list_languages,
    sample_for,
    validate_code,
    validate_json,
)
from .markdown_render import (
    MarkdownError,
    render_markdown,
    sample_markdown,
)
from .unicode_codec import (
    UnicodeCodecError,
    decode_unicode,
    encode_unicode,
    probe_unicode,
)
from .timestamp_conv import (
    TimestampError,
    convert,
    now_snapshot,
)
from .regex_tool import (
    RegexError,
    flags_to_names,
    generate_regex,
    parse_flags,
    replace_regex,
    test_regex,
)

__all__ = [
    "encode_base64",
    "decode_base64",
    "probe_base64",
    "DecodeError",
    "format_code",
    "format_json",
    "validate_code",
    "validate_json",
    "list_languages",
    "sample_for",
    "FormatError",
    "JsonError",
    "render_markdown",
    "sample_markdown",
    "MarkdownError",
    "decode_unicode",
    "encode_unicode",
    "probe_unicode",
    "UnicodeCodecError",
    "convert",
    "now_snapshot",
    "TimestampError",
    "test_regex",
    "replace_regex",
    "generate_regex",
    "parse_flags",
    "flags_to_names",
    "RegexError",
]
