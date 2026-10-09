"""Phosphor icon-font subsetting stays in sync with the templates.

The generated ``regular.min.css`` / ``Phosphor.min.woff2`` are gitignored build
outputs, so what has to hold in the repo is the input side: every ``ph-*`` class
a template references must exist in the source stylesheet, and the generator
must emit a font whose cmap covers exactly those classes.
"""

from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))

import subset_phosphor as phosphor  # noqa: E402


def test_every_ph_token_has_a_css_rule():
    """A typo'd icon class renders an empty box — fail it here instead."""
    known, unknown = phosphor.used_classes()
    assert known, "templates should reference at least one ph-* icon"
    assert not unknown, (
        "ph-* tokens with no rule in css/phosphor/regular.css: "
        + ", ".join(sorted(unknown))
    )


def test_generated_font_and_css_cover_the_used_icons():
    pytest.importorskip("fontTools", reason="build-time dependency (requirements-dev)")
    pytest.importorskip("brotli", reason="build-time dependency (requirements-dev)")

    font_bytes, css_text, n_icons = phosphor.build()
    known, _ = phosphor.used_classes()
    assert n_icons == len(known)

    # The @font-face must point at the subset font, or the browser loads the
    # full one and the glyphs could silently be missing.
    assert 'url("../../fonts/Phosphor.min.woff2")' in css_text
    for cls in known:
        assert f".ph.{cls}:before" in css_text, cls

    from fontTools.ttLib import TTFont

    cmap = set(TTFont(BytesIO(font_bytes)).getBestCmap())
    missing = sorted(c for c in known if int(phosphor._glyph_map()[c], 16) not in cmap)
    assert not missing, f"glyphs missing from the subset font: {missing}"
