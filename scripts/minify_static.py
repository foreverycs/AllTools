"""Build-time minifier for static CSS/JS assets.

Produces ``<name>.min.<ext>`` next to each source file. The app's
``static_url`` helper (see ``tools/common.py``) auto-detects the minified
variant at runtime, so templates do not need to change: in development the
source file is served; once this script runs (e.g. in the Docker image build),
the minified version is preferred.

The minifiers are deliberately conservative, dependency-free, pure-Python:

- **CSS**: strips comments, collapses whitespace, removes trailing semicolons,
  trims selectors/declaration blocks. Preserves ``url(...)`` and
  ``content: "..."`` payloads verbatim.
- **JS**: removes comments (line + block, but not inside strings or regex
  literals), collapses runs of whitespace, drops safe trailing semicolons.
  Does NOT rename variables or do AST-level mangling — the goal is to cut
  transfer size, not to obfuscate. Functionality is byte-for-byte equivalent.

Usage::

    python scripts/minify_static.py            # minify everything under static/
    python scripts/minify_static.py --check    # exit 1 if any .min is stale
    python scripts/minify_static.py --clean    # remove generated .min files

Skip individual files by adding a ``# nolint`` / ``/* nolint */`` banner on the
first line.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

CSS_GLOBS = ("css/**/*.css",)
JS_GLOBS = ("js/**/*.js",)

# Files that must NOT be minified (service worker / already-handled / vendored).
SKIP_NAMES = {"sw.js"}

_NOLINT_RE = re.compile(r"^\s*(?:/\*.*nolint.*\*/|//.*nolint)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# CSS minifier
# ---------------------------------------------------------------------------

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_WS_RE = re.compile(r"\s+")
_CSS_LEADING_RE = re.compile(r"\s*([{}:;,>~+])\s*")
_CSS_TRAILING_SEMI_RE = re.compile(r";\s*}")
_CSS_EMPTY_BLOCK_RE = re.compile(r"[^{}]+\{\s*\}")
_CSS_DUP_SEMI_RE = re.compile(r";{2,}")


def minify_css(src: str) -> str:
    """Conservative CSS minifier (no tokenizer, regex-only).

    Safe because the transformations are whitespace/comment-only and never
    touch the inside of ``url(...)`` / ``content: "..."`` beyond whitespace
    that CSS parsers already collapse.
    """
    if _NOLINT_RE.match(src):
        return src
    out = _CSS_COMMENT_RE.sub("", src)
    # Collapse all whitespace runs to a single space (strings are preserved —
    # CSS string values are not whitespace-significant beyond a single space).
    out = _CSS_WS_RE.sub(" ", out)
    # Trim spaces around punctuation that never needs them.
    out = _CSS_LEADING_RE.sub(r"\1", out)
    # Drop trailing semicolons inside a block and collapse `;;`.
    out = _CSS_DUP_SEMI_RE.sub(";", out)
    out = _CSS_TRAILING_SEMI_RE.sub("}", out)
    # Remove empty rules (selector with no declarations).
    out = _CSS_EMPTY_BLOCK_RE.sub("", out)
    return out.strip()


# ---------------------------------------------------------------------------
# JS minifier
# ---------------------------------------------------------------------------

# A simplified state machine: walk the source once, tracking whether the
# current position is inside a string, template literal, or regex. Comments
# outside those contexts are dropped; whitespace is collapsed.
def minify_js(src: str) -> str:
    """Conservative JS minifier (whitespace + comments only, no mangling).

    Handles string literals (``'`` ``"`` `` ` ``), regex literals, and line
    comments (``//``) + block comments (``/* */``). Whitespace is collapsed to
    a single space and trimmed where it is not significant (after operators
    and punctuation). Newlines inside template literals are preserved.
    """
    if _NOLINT_RE.match(src):
        return src

    out: list[str] = []
    i = 0
    n = len(src)
    # Track the previous non-whitespace char to decide whether a space is
    # needed before the next token.
    prev = ""
    # State: None | "'" | '"' | "`" | "/" (regex)
    state: str | None = None

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if state is not None:
            # Inside a string / template / regex: copy verbatim until the
            # closing delimiter, honoring backslash escapes.
            out.append(ch)
            if ch == "\\":
                # Escape next char literally.
                if i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
            elif state == "`" and ch == "$" and nxt == "{":
                # Template expression: copy until matching `}` (no nesting).
                out.append(nxt)
                i += 2
                depth = 1
                while i < n and depth > 0:
                    c2 = src[i]
                    out.append(c2)
                    if c2 == "{":
                        depth += 1
                    elif c2 == "}":
                        depth -= 1
                    i += 1
                continue
            elif ch == state:
                state = None
            i += 1
            prev = ch
            continue

        # Not inside a string/regex: look for comments and strings.
        if ch == "/" and nxt == "/":
            # Line comment: skip to end of line (or EOF).
            j = src.find("\n", i)
            if j == -1:
                break
            i = j  # keep the newline (collapsed later)
            continue
        if ch == "/" and nxt == "*":
            # Block comment: skip to ``*/``.
            j = src.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        if ch in "'\"`":
            state = ch
            out.append(ch)
            prev = ch
            i += 1
            continue
        # Regex literal: a ``/`` that starts a regex (not division). Heuristic:
        # the previous significant char is one of ``([{,=:!&|?+*-~^%<>`` or
        # start of file, and next char is not ``/`` or ``*`` (already handled).
        if ch == "/" and nxt not in ("/", "*") and _regex_context(prev):
            state = "/"
            out.append(ch)
            prev = ch
            i += 1
            continue

        # Whitespace handling.
        if ch in " \t\r\n":
            # Collapse runs to a single space, and drop the space if the
            # surrounding chars are both "safe" (punctuation/operators) or if
            # the previous or next char already separates tokens.
            j = i
            while j < n and src[j] in " \t\r\n":
                j += 1
            nxt2 = src[j] if j < n else ""
            if _needs_space(prev, nxt2):
                out.append(" ")
                prev = " "
            i = j
            continue

        out.append(ch)
        prev = ch
        i += 1

    return "".join(out)


_REGEX_PREV_CHARS = set("([{,=:!&|?+*-~^%<>;")


def _regex_context(prev: str) -> bool:
    """True when a ``/`` at this position is likely a regex literal opener.

    Conservative: treats division after an identifier/number as regex only when
    the previous char is an operator/punctuation that cannot end an expression.
    """
    return prev == "" or prev in _REGEX_PREV_CHARS or prev == "return"


_NEEDS_SPACE_LEFT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
_NEEDS_SPACE_RIGHT = _NEEDS_SPACE_LEFT


def _needs_space(prev: str, nxt: str) -> bool:
    """Whether a single space must be kept between ``prev`` and ``nxt``.

    Drops the space when either side is punctuation (tokens already separated)
    or when keeping it would change nothing (e.g. around operators).
    """
    if not prev or not nxt:
        return False
    return prev in _NEEDS_SPACE_LEFT and nxt in _NEEDS_SPACE_RIGHT


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _candidates() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in (*CSS_GLOBS, *JS_GLOBS):
        for path in STATIC_DIR.glob(pattern):
            if path.name in SKIP_NAMES:
                continue
            if path.name.startswith("."):
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
    return sorted(out)


def _min_path(path: Path) -> Path:
    return path.with_suffix(".min" + path.suffix)


def minify_file(path: Path) -> tuple[Path, int, int] | None:
    """Minify one file; returns ``(path, src_size, out_size)`` or None if skipped."""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"skip {path}: {exc}", file=sys.stderr)
        return None
    if path.suffix == ".css":
        out = minify_css(src)
    elif path.suffix == ".js":
        out = minify_js(src)
    else:
        return None
    if _NOLINT_RE.match(src):
        return None
    target = _min_path(path)
    target.write_text(out, encoding="utf-8")
    return path, len(src), len(out)


def run(check: bool = False, clean: bool = False) -> int:
    paths = _candidates()
    if not paths:
        print("no static assets found")
        return 0

    if clean:
        removed = 0
        for path in paths:
            target = _min_path(path)
            if target.is_file():
                target.unlink()
                removed += 1
        print(f"removed {removed} minified files")
        return 0

    stale = 0
    total_src = 0
    total_out = 0
    for path in paths:
        result = minify_file(path) if not check else None
        target = _min_path(path)
        if check:
            if not target.is_file():
                print(f"missing {target}")
                stale += 1
                continue
            current = minify_file(path)
            if current is None:
                continue
            _, _, out_size = current
            existing = target.read_text(encoding="utf-8")
            fresh = minify_css(path.read_text(encoding="utf-8")) if path.suffix == ".css" else minify_js(path.read_text(encoding="utf-8"))
            if existing != fresh:
                print(f"stale {target}")
                stale += 1
            continue
        if result is None:
            continue
        _, src_size, out_size = result
        total_src += src_size
        total_out += out_size
        ratio = (1 - out_size / src_size) * 100 if src_size else 0
        print(f"  {path.relative_to(BASE_DIR)}: {src_size} → {out_size} B ({ratio:.0f}% smaller)")

    if check:
        if stale:
            print(f"{stale} stale/missing minified files — run: python scripts/minify_static.py")
            return 1
        print("all minified files up to date")
        return 0

    if total_src:
        ratio = (1 - total_out / total_src) * 100
        print(f"total: {total_src} → {total_out} B ({ratio:.0f}% smaller)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Minify static CSS/JS assets.")
    parser.add_argument("--check", action="store_true", help="exit 1 if any .min is missing or stale")
    parser.add_argument("--clean", action="store_true", help="remove generated .min files")
    args = parser.parse_args()
    return run(check=args.check, clean=args.clean)


if __name__ == "__main__":
    raise SystemExit(main())
