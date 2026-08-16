"""HTML → Markdown conversion for the html2md plugin (stdlib only).

A small streaming converter built on ``html.parser.HTMLParser`` — no external
dependencies. Structure is preserved (headings, paragraphs, nested lists, GFM
tables, fenced code blocks, block quotes, links, images); inline emphasis,
strikethrough and code map to Markdown syntax. ``<script>``/``<style>``
content is dropped.

Output is CommonMark/GFM friendly (ATX headings, fenced code blocks, GFM
tables), so it renders on GitHub / Typora / most Markdown engines.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

# Inline emphasis tags → (prefix, suffix).
_INLINE_MARKS: Dict[str, Tuple[str, str]] = {
    "strong": ("**", "**"),
    "b": ("**", "**"),
    "em": ("*", "*"),
    "i": ("*", "*"),
    "del": ("~~", "~~"),
    "s": ("~~", "~~"),
    "u": ("_", "_"),
    "code": ("`", "`"),
    "kbd": ("`", "`"),
}

# Transparent block-level structure (flush the current line on open/close).
# ``p`` is handled separately (blank-line semantics + list-item inlining).
_BLOCK_TAGS = {
    "div", "section", "article", "main", "header", "footer", "nav",
    "aside", "dl", "dt", "dd", "form", "figure", "details", "summary",
    "address", "thead", "tbody", "tfoot",
}

_HEADING_RE = re.compile(r"^h([1-6])$")
_CODE_CLASS_RE = re.compile(r"(?:language|lang|highlight)[-:]?([\w+.-]+)", re.I)
_SPACE_RE = re.compile(r"[ \t\r\n\f]+")
# Scheme whitelist for hrefs/srcs that carry a scheme (relative URLs are kept).
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_ALLOWED_SCHEMES = {"http", "https", "mailto", "tel", "ftp"}


def _attr(attrs, name: str, default: str = "") -> str:
    for k, v in attrs:
        if k.lower() == name:
            return v or ""
    return default


def _safe_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    m = _SCHEME_RE.match(u)
    if m and m.group(1).lower() not in _ALLOWED_SCHEMES:
        # Block javascript:, vbscript:, data:, file: ... — including
        # data:image/svg+xml which can smuggle an XSS payload into the output.
        return ""
    return u


class HtmlToMarkdown(HTMLParser):
    """Streaming HTML → Markdown converter (feed / close / get_markdown)."""

    def __init__(self, *, tables: bool = True, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.tables = tables
        self.base_url = (base_url or "").strip()

        self.lines: List[str] = []
        self.stats: Dict[str, int] = {
            "headings": 0,
            "lists": 0,
            "list_items": 0,
            "code_blocks": 0,
            "tables": 0,
            "images": 0,
            "links": 0,
            "quotes": 0,
            "hr": 0,
        }

        self._line = ""
        # Inline marks stack: [tag, prefix, suffix, emitted] (list for mutation).
        self._marks: List[list] = []
        # Block stack: "pre", "quote", "list", "li", "heading", "p", "hr".
        self._blocks: List[str] = []
        # (ordered, next_index) per nesting level.
        self._list_stack: List[Tuple[bool, int]] = []
        self._pre = False
        self._pre_buf = ""
        self._pre_lang = ""
        self._skip = 0  # depth inside <script>/<style>
        # Link text tracking: [(href or "", len(_line) after "[")].
        self._link_stack: List[Tuple[str, int]] = []
        # Table collection.
        self._in_table = False
        self._tbl_rows: List[List[str]] = []
        self._tbl_row: List[str] = []
        self._tbl_cell = ""

    # ------------------------------------------------------------------ util

    def _quote_depth(self) -> int:
        return self._blocks.count("quote")

    def flush(self) -> None:
        """Append the current line (rstripped) and reset it."""
        s = self._line.rstrip()
        self._line = ""
        if not s:
            return
        q = self._quote_depth()
        if q:
            s = "> " * q + s
        self.lines.append(s)

    def _ensure_gap(self) -> None:
        """Ensure a blank line between top-level blocks."""
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def _push_block(self, kind: str) -> None:
        self._blocks.append(kind)

    def _pop_block(self, kind: str) -> None:
        if self._blocks and self._blocks[-1] == kind:
            self._blocks.pop()

    def _emit_hr(self) -> None:
        self.flush()
        self._ensure_gap()
        self.lines.append("---")
        self._ensure_gap()
        self.stats["hr"] += 1

    def _emit_image(self, attrs) -> None:
        src = _safe_url(_attr(attrs, "src"))
        if not src:
            return
        if self.base_url:
            src = urljoin(self.base_url, src)
        alt = _attr(attrs, "alt")
        if self._line and not self._line[-1].isspace() and self._line[-1] != "[":
            self._line += " "
        self._line += f"![{alt}]({src})"
        self.stats["images"] += 1

    def _emit_pre(self) -> None:
        buf = self._pre_buf.strip("\r\n")
        if buf:
            self._ensure_gap()
            q = self._quote_depth()
            prefix = "> " * q
            self.lines.append(prefix + "```" + self._pre_lang)
            for ln in buf.splitlines():
                self.lines.append(prefix + ln)
            self.lines.append(prefix + "```")
            self._ensure_gap()
            self.stats["code_blocks"] += 1
        self._pre_lang = ""

    # ------------------------------------------------------------- table fmt

    def _flush_tbl_cell(self) -> None:
        cell = self._tbl_cell.strip()
        if cell:
            self._tbl_row.append(cell)
        self._tbl_cell = ""

    def _flush_tbl_row(self) -> None:
        if self._tbl_row or self._tbl_cell.strip():
            self._flush_tbl_cell()
            self._tbl_rows.append(self._tbl_row)
        self._tbl_row = []
        self._tbl_cell = ""

    def _emit_table(self) -> None:
        self._flush_tbl_row()
        if not self._tbl_rows:
            return
        rows = [
            [c.replace("|", "\\|") for c in r]
            for r in self._tbl_rows
            if any(c.strip() for c in r)
        ]
        if not rows:
            return
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        self._ensure_gap()
        self.lines.append("| " + " | ".join(padded[0]) + " |")
        self.lines.append("| " + " | ".join(["---"] * width) + " |")
        for r in padded[1:]:
            self.lines.append("| " + " | ".join(r) + " |")
        self._ensure_gap()
        self.stats["tables"] += 1

    # -------------------------------------------------------------- HTMLParser

    def handle_data(self, text: str) -> None:
        if self._skip:
            return
        if self._pre:
            self._pre_buf += text
            return
        if self._in_table:
            self._tbl_cell += _SPACE_RE.sub(" ", text)
            return
        t = _SPACE_RE.sub(" ", text)
        if not t:
            return
        if t.isspace():
            if self._line and not self._line[-1].isspace():
                self._line += " "
            return
        if self._line and self._line[-1].isspace():
            t = t.lstrip()
        for m in self._marks:
            if not m[3]:
                self._line += m[1]
                m[3] = True
        self._line += t

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in ("script", "style"):
                self._skip += 1
            return
        if tag in ("script", "style"):
            self._skip = 1
            return
        if self._in_table:
            if tag == "tr":
                self._flush_tbl_row()
            elif tag in ("td", "th"):
                self._flush_tbl_cell()
            return
        if tag == "pre":
            self.flush()
            self._ensure_gap()
            self._push_block("pre")
            self._pre = True
            self._pre_buf = ""
            self._pre_lang = ""
            return
        if tag == "code" and self._pre:
            m = _CODE_CLASS_RE.search(_attr(attrs, "class"))
            if m:
                self._pre_lang = m.group(1).lower()
            return
        if tag == "blockquote":
            self.flush()
            self._ensure_gap()
            self._push_block("quote")
            self.stats["quotes"] += 1
            return
        if tag in ("ul", "ol"):
            self.flush()
            # No blank line between a parent list item and a nested list.
            if not self._blocks or self._blocks[-1] != "li":
                self._ensure_gap()
            self._push_block("list")
            self._list_stack.append((tag == "ol", 1))
            self.stats["lists"] += 1
            return
        if tag == "li":
            self.flush()
            depth = max(0, len(self._list_stack) - 1)
            indent = "  " * depth
            ordered, idx = self._list_stack[-1] if self._list_stack else (False, 0)
            if ordered:
                self._line += f"{indent}{idx}. "
                if self._list_stack:
                    self._list_stack[-1] = (True, idx + 1)
            else:
                self._line += f"{indent}- "
            self._push_block("li")
            self.stats["list_items"] += 1
            return
        m = _HEADING_RE.match(tag)
        if m:
            self.flush()
            self._ensure_gap()
            level = int(m.group(1))
            self._line += "#" * level + " "
            self._push_block("heading")
            self.stats["headings"] += 1
            return
        if tag == "table":
            self.flush()
            if self.tables:
                self._ensure_gap()
                self._in_table = True
                self._tbl_rows = []
                self._tbl_row = []
                self._tbl_cell = ""
            return
        if tag == "hr":
            self._emit_hr()
            self._push_block("hr")
            return
        if tag == "a":
            href = _safe_url(_attr(attrs, "href"))
            if href and self.base_url:
                href = urljoin(self.base_url, href)
            if href:
                self._line += "["
            self._link_stack.append((href, len(self._line)))
            return
        if tag == "p":
            if self._blocks and self._blocks[-1] == "li":
                return  # keep the paragraph text on the list-item line
            self.flush()
            if self._blocks and self._blocks[-1] == "quote":
                return
            self._ensure_gap()
            return
        if tag == "img":
            self._emit_image(attrs)
            return
        if tag == "br":
            self.flush()
            return
        if tag in _INLINE_MARKS:
            pre, suf = _INLINE_MARKS[tag]
            self._marks.append([tag, pre, suf, False])
            return
        if tag in _BLOCK_TAGS:
            self.flush()
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag in ("script", "style"):
                self._skip -= 1
            return
        if self._in_table:
            if tag in ("td", "th"):
                self._flush_tbl_cell()
            elif tag == "tr":
                self._flush_tbl_row()
            elif tag == "table":
                self._flush_tbl_row()
                self._emit_table()
                self._in_table = False
            return
        if tag == "pre":
            self._pre = False
            self._emit_pre()
            self._pop_block("pre")
            return
        if tag == "blockquote":
            self.flush()
            self._pop_block("quote")
            return
        if tag in ("ul", "ol"):
            self.flush()
            if self._list_stack:
                self._list_stack.pop()
            self._pop_block("list")
            return
        if tag == "li":
            self.flush()
            self._pop_block("li")
            return
        if tag == "hr":
            self.flush()
            self._pop_block("hr")
            return
        m = _HEADING_RE.match(tag)
        if m:
            self.flush()
            self._pop_block("heading")
            return
        if tag == "a":
            href, start_len = self._link_stack.pop() if self._link_stack else ("", 0)
            if not href:
                # Dangerous/empty href: drop the link wrapper, keep the text.
                return
            if len(self._line) == start_len:
                # No link text (e.g. <a href="..."></a>) → show the URL itself.
                self._line = self._line[:-1]
                self._line += href
            else:
                self._line += f"]({href})"
            self.stats["links"] += 1
            return
        if tag == "p":
            if self._blocks and self._blocks[-1] == "li":
                return
            self.flush()
            return
        for i in range(len(self._marks) - 1, -1, -1):
            if self._marks[i][0] == tag:
                _, pre, suf, emitted = self._marks.pop(i)
                if emitted:
                    self._line += suf
                return
        if tag in _BLOCK_TAGS:
            self.flush()
            return

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip:
            return
        if tag == "img":
            self._emit_image(attrs)
        elif tag == "hr":
            self._emit_hr()
        elif tag == "br":
            self.flush()
        elif tag == "input":
            return

    # --------------------------------------------------------------- output

    def get_markdown(self) -> str:
        self.flush()
        out: List[str] = []
        blank = 0
        for line in self.lines:
            s = line.rstrip()
            if not s:
                blank += 1
                if blank <= 1:
                    out.append("")
                continue
            blank = 0
            out.append(s)
        while out and not out[0]:
            out.pop(0)
        while out and not out[-1]:
            out.pop()
        return "\n".join(out)


def convert_html(
    html_text: Optional[str],
    *,
    tables: bool = True,
    base_url: str = "",
) -> Dict[str, object]:
    """Convert an HTML snippet / document to Markdown text.

    Returns ``{"result", "input_chars", "output_chars", "blocks"}`` where
    ``blocks`` is a dict of structural element counts for the UI.
    """
    source = html_text or ""
    p = HtmlToMarkdown(tables=tables, base_url=base_url)
    p.feed(source)
    p.close()
    result = p.get_markdown()
    return {
        "result": result,
        "input_chars": len(source),
        "output_chars": len(result),
        "blocks": dict(p.stats),
    }
