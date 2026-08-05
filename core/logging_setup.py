"""Minimal logging setup for the toolkit process."""

from __future__ import annotations

import logging
import os
import sys


_configured = False


def configure_logging() -> None:
    """Idempotent root logging config (env ``LOG_LEVEL``, default INFO)."""
    global _configured
    if _configured:
        return
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy third parties unless debugging.
    if level > logging.DEBUG:
        logging.getLogger("pdfminer").setLevel(logging.WARNING)
        logging.getLogger("PIL").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str = "toolkit") -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
