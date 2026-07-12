"""CLI: convert Word to PDF.

Usage:
  python -m word2pdf input.docx
  python -m word2pdf input.docx -o out.pdf
  python -m word2pdf input.docx --engine libreoffice
"""
from __future__ import annotations

import argparse
import sys

from .converter import ConversionError, convert_to_pdf, engine_info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m word2pdf",
        description="Convert a Word document (.docx / .doc) to PDF.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Input .docx or .doc path",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output .pdf path (default: <input>.pdf)",
    )
    parser.add_argument(
        "--engine",
        choices=("libreoffice", "msword"),
        default=None,
        help="Force a conversion backend (default: auto)",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print available engines and exit",
    )
    args = parser.parse_args(argv)

    if args.info:
        info = engine_info()
        print(f"ready={info['ready']}")
        print(f"engines={','.join(info['engines']) or '(none)'}")
        print(f"preferred={info['preferred'] or '(none)'}")
        print(f"libreoffice_path={info['libreoffice_path'] or '(not found)'}")
        return 0 if info["ready"] else 1

    if not args.input:
        parser.error("the following arguments are required: input")

    try:
        pdf_path, engine = convert_to_pdf(
            args.input,
            args.output,
            engine=args.engine,
        )
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: conversion failed: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {pdf_path}  (engine={engine})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
