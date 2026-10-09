"""Build-time subset of the Phosphor icon font and its CSS.

``static/css/phosphor/regular.css`` declares every icon in the set (1530 rules,
~82 KB) and ``static/fonts/Phosphor.woff2`` carries all 1545 glyphs (~147 KB),
but the site uses only the icons that actually appear in the templates. This
script emits a slim pair next to the originals:

- ``static/fonts/Phosphor.min.woff2`` — only the glyphs the templates reference
- ``static/css/phosphor/regular.min.css`` — the ``@font-face`` plus those rules

``static_url`` rewrites ``regular.css`` → ``regular.min.css`` when
``USE_MIN_ASSETS=1`` (set in the Docker image), so templates keep referencing
the source filename. Without that flag the full font is served and nothing
breaks.

The class set is discovered from the templates rather than hard-coded: every
``ph-*`` token under ``templates/`` and ``plugins/*/templates/`` that has a rule
in the source CSS is kept. Tokens with no matching rule are printed as a warning
on stderr — usually a typo, or a template that never loads the stylesheet — but
they do not fail the build, because a stray ``ph-`` string is not proof of a
broken icon.

Requires ``fonttools`` and ``brotli`` — build-time only, never at runtime::

    python scripts/subset_phosphor.py            # generate
    python scripts/subset_phosphor.py --check    # exit 1 if missing or stale
    python scripts/subset_phosphor.py --clean    # remove generated files

Run alongside ``scripts/minify_static.py`` in the Docker build. That script
skips ``css/phosphor/regular.css`` (see its ``SKIP_REL``) so the slim
``@font-face`` url survives — if only the minifier ran you would get no
``regular.min.css`` at all, and the full font would be served instead.
"""

from __future__ import annotations

import argparse
import re
import sys
from io import BytesIO
from pathlib import Path

# Reuse the project's CSS minifier so the emitted file matches the rest of
# static/ (it lives beside this script; import is only needed for generation).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from minify_static import minify_css  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_CSS = BASE_DIR / "static" / "css" / "phosphor" / "regular.css"
OUT_CSS = BASE_DIR / "static" / "css" / "phosphor" / "regular.min.css"
SRC_FONT = BASE_DIR / "static" / "fonts" / "Phosphor.woff2"
OUT_FONT = BASE_DIR / "static" / "fonts" / "Phosphor.min.woff2"

# Where icon classes may be referenced. The source CSS itself is excluded —
# it defines every class and would otherwise pin the whole set.
SOURCE_DIRS = [BASE_DIR / "templates"]
SOURCE_GLOBS = ["plugins/*/templates"]

_GLYPH_RE = re.compile(
    r"\.ph\.(?P<cls>ph-[a-z0-9-]+):before\s*\{\s*"
    r"content:\s*\"\\(?P<cp>[0-9a-fA-F]{4,6})\"\s*;\s*\}",
    re.S,
)
_CLASS_RE = re.compile(r"\bph-[a-z0-9-]+\b")
_FONT_SRC_RE = re.compile(r"url\(\s*\"([^\"]+)\"\s*\)")
_HEAD_RE = re.compile(r"\A.*?(?=^\.ph\.ph-)", re.S | re.M)


def _scan_candidates() -> list[Path]:
    out: list[Path] = []
    for d in SOURCE_DIRS:
        if d.is_dir():
            out.extend(sorted(d.rglob("*.html")))
    for pattern in SOURCE_GLOBS:
        out.extend(sorted(BASE_DIR.glob(pattern + "/**/*.html")))
    seen: set[Path] = set()
    uniq = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def used_classes() -> tuple[set[str], set[str]]:
    """Return ``(known, unknown)`` icon class tokens referenced by templates."""
    tokens: set[str] = set()
    for path in _scan_candidates():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(f"cannot read {path}: {exc}") from exc
        tokens.update(_CLASS_RE.findall(text))
    known, unknown = set(), set()
    for tok in tokens:
        (known if tok in _glyph_map() else unknown).add(tok)
    return known, unknown


_glyph_cache: dict[str, str] | None = None


def _glyph_map() -> dict[str, str]:
    """``class name -> codepoint hex`` parsed from the source CSS."""
    global _glyph_cache
    if _glyph_cache is None:
        src = SRC_CSS.read_text(encoding="utf-8")
        _glyph_cache = {
            m.group("cls"): m.group("cp").lower() for m in _GLYPH_RE.finditer(src)
        }
    return _glyph_cache


def build() -> tuple[bytes, str, int]:
    """Return ``(font_bytes, css_text, icon_count)`` for the current templates."""
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError as exc:  # pragma: no cover - build-time dependency
        raise SystemExit(
            "fonttools/brotli not installed — run `pip install fonttools brotli` "
            "(build-time only; the app never imports them)"
        ) from exc

    known, unknown = used_classes()
    if not known:
        raise SystemExit(
            "no ph-* icon classes found in templates/ or plugins/*/templates/"
        )
    if unknown:
        # Not fatal: an unmatched token is not an icon class. Surfaced because
        # it is usually a typo that would silently render an empty box.
        print(
            "warning: tokens with no matching CSS rule: " + ", ".join(sorted(unknown)),
            file=sys.stderr,
        )

    cmap = _glyph_map()
    codepoints = sorted(int(cmap[c], 16) for c in known)

    font = TTFont(str(SRC_FONT))
    # TTFont.recalcTimestamp defaults to True, so `save()` stamps head.modified
    # with the wall clock — every run would emit different bytes and `--check`
    # could never pass. Keep the source timestamps for a reproducible build.
    font.recalcTimestamp = False
    options = subset.Options()
    options.flavor = "woff2"
    options.notdef_glyph = True
    options.notdef_outline = True
    options.recommended_glyphs = True
    # `::before { content: "\xxxx" }` never forms a ligature, but the base `.ph`
    # class enables `font-feature-settings: "liga"` — keeping the default
    # feature set avoids a silent shape change if a future rule needs it.
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    # FontForge's build timestamp — not subsettable, useless at runtime.
    options.drop_tables = list(options.drop_tables) + ["FFTM"]
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=codepoints)
    subsetter.subset(font)
    font.flavor = "woff2"

    buf = BytesIO()
    font.save(buf)
    font_bytes = buf.getvalue()

    src = SRC_CSS.read_text(encoding="utf-8")
    head = _HEAD_RE.search(src)
    if head is None:
        raise SystemExit(f"{SRC_CSS}: could not locate the .ph base rules")
    head_text = head.group(0)
    head_text, n = _FONT_SRC_RE.subn(
        'url("../../fonts/Phosphor.min.woff2")', head_text, count=1
    )
    if n != 1:
        raise SystemExit(f"{SRC_CSS}: @font-face src url not found")

    rules = []
    for cls in sorted(known):
        rules.append(f'.ph.{cls}:before{{content:"\\{cmap[cls]}";}}')
    css_text = minify_css(head_text + "\n" + "\n".join(rules) + "\n")
    return font_bytes, css_text, len(known)


def _read_out() -> tuple[bytes | None, str | None]:
    fb = OUT_FONT.read_bytes() if OUT_FONT.is_file() else None
    cs = OUT_CSS.read_text(encoding="utf-8") if OUT_CSS.is_file() else None
    return fb, cs


def run(check: bool = False, clean: bool = False) -> int:
    if clean:
        removed = 0
        for path in (OUT_FONT, OUT_CSS):
            if path.is_file():
                path.unlink()
                removed += 1
        print(f"removed {removed} file(s)")
        return 0

    font_bytes, css_text, n_icons = build()

    if check:
        cur_font, cur_css = _read_out()
        missing = [
            str(p) for p, v in ((OUT_FONT, cur_font), (OUT_CSS, cur_css)) if v is None
        ]
        if missing:
            print(
                "missing generated icon assets: " + ", ".join(missing), file=sys.stderr
            )
            return 1
        if cur_font != font_bytes or cur_css != css_text:
            print(
                f"{OUT_FONT.name} / {OUT_CSS.name} are stale — rerun "
                "scripts/subset_phosphor.py",
                file=sys.stderr,
            )
            return 1
        print(f"subsetted icon assets up to date ({n_icons} icons)")
        return 0

    OUT_FONT.write_bytes(font_bytes)
    OUT_CSS.write_text(css_text, encoding="utf-8")
    src_font, src_css = SRC_FONT.stat().st_size, SRC_CSS.stat().st_size
    print(
        f"icons: {n_icons} used\n"
        f"  font {src_font} -> {len(font_bytes)} B\n"
        f"  css  {src_css} -> {len(css_text)} B"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Subset the Phosphor icon font.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if generated assets are missing or stale",
    )
    parser.add_argument("--clean", action="store_true", help="remove generated assets")
    args = parser.parse_args()
    return run(check=args.check, clean=args.clean)


if __name__ == "__main__":
    raise SystemExit(main())
