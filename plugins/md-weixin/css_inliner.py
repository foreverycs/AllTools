"""CSS → inline ``style`` attributes for WeChat-friendly HTML (stdlib + tinycss2).

微信公众号后台不支持 ``<style>`` 标签与 CSS 变量，因此需要把主题 CSS 展开并
内联到每个元素的 ``style`` 属性。本模块实现一个小型、有界的 CSS 内联引擎：

- 用 tinycss2 解析主题 CSS（只支持本工具模板用到的选择器语法）；
- 用 ``html.parser`` 把已净化的 HTML 解析成树；
- 按（specificity, 源码顺序）级联合并规则；
- 可选：把 ``<pre><code>`` 代码块交给 Pygments 高亮（``noclasses=True``
  直接输出内联颜色，无需额外 CSS）；未安装 Pygments 时静默跳过。

支持的选择器：``tag``、``.class``、``*``、``:nth-child(odd/even/n/An+B)``，
组合符空格（后代）与 ``>``（子元素），以及逗号分隔的选择器列表。
其余语法（属性选择器、``~``、``+`` 等）会被忽略而不是报错。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

import tinycss2

VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_SIMPLE_NTH = re.compile(r"([+-]?\d+)")
_AN_NTH = re.compile(r"([+-]?\d*)n(?:\s*([+-])\s*(\d+))?")
_IDENT_RE = re.compile(r"[\w-]+")


# ---------------------------------------------------------------------------
# HTML tree (lightweight DOM)
# ---------------------------------------------------------------------------

class _Text:
    __slots__ = ("data",)

    def __init__(self, data: str) -> None:
        self.data = data


class _Elem:
    __slots__ = ("tag", "attrs", "children", "parent", "index")

    def __init__(self, tag: str, attrs: Dict[str, str]) -> None:
        self.tag = tag.lower()
        self.attrs = dict(attrs)
        self.children: List[Any] = []
        self.parent: Optional[_Elem] = None
        # 1-based position among element siblings (0 = unknown, recomputed
        # lazily). Assigned once during tree build → O(1) nth-child lookups.
        self.index = 0


class _TreeBuilder(HTMLParser):
    """Build a minimal DOM tree (entities decoded, like browsers)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Elem("#root", {})
        self.stack = [self.root]
        # id(parent) -> next element-sibling index (O(1) nth-child lookups).
        self._counters: Dict[int, int] = {}

    def _append_element(self, tag: str, attrs) -> None:
        node = _Elem(tag, {k: (v or "") for k, v in attrs})
        parent = self.stack[-1]
        node.parent = parent
        pid = id(parent)
        idx = self._counters.get(pid, 1)
        node.index = idx
        self._counters[pid] = idx + 1
        parent.children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs) -> None:
        node = self._append_element(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._append_element(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(_Text(data))


def _parse_html(html: str) -> _Elem:
    builder = _TreeBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _serialize(node: Any, out: List[str]) -> None:
    if isinstance(node, _Text):
        out.append(_escape_text(node.data))
        return
    parts = ["<", node.tag]
    for k, v in node.attrs.items():
        parts.append(f' {k}="{_escape_attr(v)}"')
    parts.append(">")
    out.append("".join(parts))
    for child in node.children:
        _serialize(child, out)
    if node.tag not in VOID_TAGS:
        out.append(f"</{node.tag}>")


def _serialize_nodes(nodes: List[Any]) -> str:
    out: List[str] = []
    for node in nodes:
        _serialize(node, out)
    return "".join(out)


def _iter_elems(node: _Elem):
    for child in node.children:
        if isinstance(child, _Elem):
            yield child
            yield from _iter_elems(child)


# ---------------------------------------------------------------------------
# Pygments code highlighting (optional, lazy import)
# ---------------------------------------------------------------------------

def _highlight_code(code: _Elem, style_name: str) -> None:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import TextLexer, get_lexer_by_name

    # Only highlight pure-text code blocks; raw HTML inside <code> (allowed
    # through sanitization) is left untouched instead of being dropped.
    if any(isinstance(c, _Elem) for c in code.children):
        return

    text = "".join(c.data for c in code.children if isinstance(c, _Text))
    lang = None
    class_attr = code.attrs.get("class", "")
    m = re.search(r"(?:language|lang)[-:]([\w.+-]+)", class_attr)
    if m:
        lang = m.group(1)
    lexer = None
    if lang:
        try:
            lexer = get_lexer_by_name(lang)
        except Exception:
            lexer = None
    if lexer is None:
        lexer = TextLexer()
    formatter = HtmlFormatter(nowrap=True, noclasses=True, style=style_name)
    try:
        hl = highlight(text, lexer, formatter)
    except Exception:
        return
    builder = _TreeBuilder()
    builder.feed(hl)
    builder.close()
    code.children = []
    for child in builder.root.children:
        if isinstance(child, _Elem):
            child.parent = code
        code.children.append(child)


def _highlight_code_blocks(root: _Elem, style_name: str) -> None:
    if not _pygments_available():
        return
    for node in _iter_elems(root):
        if node.tag != "code":
            continue
        ancestor = node.parent
        while ancestor is not None and ancestor.tag not in ("#root", "pre"):
            ancestor = ancestor.parent
        if ancestor is not None and ancestor.tag == "pre":
            _highlight_code(node, style_name)


_pygments_checked = False
_pygments_ok = False


def _pygments_available() -> bool:
    global _pygments_checked, _pygments_ok
    if not _pygments_checked:
        _pygments_checked = True
        try:
            import pygments  # noqa: F401

            _pygments_ok = True
        except Exception:
            _pygments_ok = False
    return _pygments_ok


# ---------------------------------------------------------------------------
# CSS parsing (tinycss2)
# ---------------------------------------------------------------------------

# Compound: (tag|None, frozenset(classes), nth-matcher|None)
_Compound = Tuple[Optional[str], frozenset, Optional[Callable[[int], bool]]]
# Selector: list of (combinator|None, compound)
_Selector = List[Tuple[Optional[str], _Compound]]
# Rule: (selector, declarations, source_index)
_Rule = Tuple[_Selector, Dict[str, str], int]


def _parse_nth(expr: str) -> Callable[[int], bool]:
    e = (expr or "").strip().lower()
    if e == "even":
        return lambda i: i % 2 == 0
    if e == "odd":
        return lambda i: i % 2 == 1
    m = _SIMPLE_NTH.fullmatch(e)
    if m:
        n = int(m.group(1))
        return lambda i: i == n
    m = _AN_NTH.fullmatch(e)
    if m:
        a_str = m.group(1) or "+1"
        a = int(a_str) if a_str not in ("+", "-") else int(a_str + "1")
        b = int((m.group(2) or "+") + (m.group(3) or "0"))
        return lambda i: (i - b) * a >= 0 and (i - b) % abs(a) == 0
    return lambda i: False


def _parse_compound(s: str) -> Optional[_Compound]:
    tag = None
    classes: set = set()
    nth = None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == ".":
            m = re.match(r"\.([\w-]+)", s[i:])
            if not m:
                return None
            classes.add(m.group(1))
            i += m.end()
        elif ch == ":":
            m = re.match(r":nth-child\(([^)]*)\)", s[i:])
            if m:
                nth = _parse_nth(m.group(1))
                i += m.end()
            else:
                return None  # unsupported pseudo-class → drop the rule
        elif ch == "*":
            tag = "*"
            i += 1
        elif ch.isalnum() or ch in "_-":
            m = _IDENT_RE.match(s, i)
            if not m:
                return None
            tag = m.group(0)
            i = m.end()
        else:
            return None
    return (tag, frozenset(classes), nth)


def _parse_selector(text: str) -> Optional[_Selector]:
    parts: List[Tuple[Optional[str], _Compound]] = []
    i, n = 0, len(text)
    comb: Optional[str] = None
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            if parts:
                comb = " "
            i += 1
            continue
        if ch == ">":
            comb = ">"
            i += 1
            continue
        if ch in "+~":
            return None  # unsupported combinator
        j = i
        while j < n and text[j] not in " \t\r\n>+~":
            j += 1
        compound = _parse_compound(text[i:j])
        if compound is None:
            return None
        parts.append((comb, compound))
        comb = None
        i = j
    if not parts or parts[0][0] is not None:
        return None
    return parts


def _split_selector_list(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _parse_declarations(tokens) -> Dict[str, str]:
    decls: Dict[str, str] = {}
    try:
        nodes = tinycss2.parse_declaration_list(
            tokens, skip_comments=True, skip_whitespace=True
        )
    except Exception:
        return decls
    for d in nodes:
        if d.type != "declaration" or not d.value:
            continue
        value = tinycss2.serialize(d.value).strip()
        if value:
            decls[d.lower_name] = value
    return decls


def _specificity(selector: _Selector) -> Tuple[int, int]:
    a = sum(1 for _, c in selector if c[1] or c[2])
    b = sum(1 for _, c in selector if c[0] and c[0] != "*")
    return (a, b)


def _parse_css_rules(css: str) -> List[_Rule]:
    rules: List[_Rule] = []
    try:
        sheet = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    except Exception:
        return rules
    for rule in sheet:
        if rule.type != "qualified-rule":
            continue
        prelude = tinycss2.serialize(rule.prelude)
        declarations = _parse_declarations(rule.content)
        if not declarations:
            continue
        for sel_text in _split_selector_list(prelude):
            selector = _parse_selector(sel_text)
            if selector is None:
                continue
            rules.append((selector, dict(declarations), len(rules)))
    rules.sort(key=lambda r: (_specificity(r[0]), r[2]))
    return rules


# ---------------------------------------------------------------------------
# Matching & inlining
# ---------------------------------------------------------------------------

def _element_index(node: _Elem) -> int:
    if node.index > 0:
        return node.index
    # Fallback for elements rebuilt after tree build (e.g. highlighted spans).
    parent = node.parent
    if parent is None:
        return 1
    index = 1
    for child in parent.children:
        if child is node:
            return index
        if isinstance(child, _Elem):
            index += 1
    return 1


def _match_compound(node: _Elem, compound: _Compound) -> bool:
    tag, classes, nth = compound
    if tag is not None and tag != "*" and tag.lower() != node.tag:
        return False
    if classes:
        node_classes = node.attrs.get("class", "").split()
        if not classes.issubset(node_classes):
            return False
    if nth is not None and not nth(_element_index(node)):
        return False
    return True


def _ancestor_matching(start: Optional[_Elem], compound: _Compound) -> Optional[_Elem]:
    cur = start
    while cur is not None and cur.tag != "#root":
        if _match_compound(cur, compound):
            return cur
        cur = cur.parent
    return None


def _match_selector(node: _Elem, selector: _Selector) -> bool:
    if not _match_compound(node, selector[-1][1]):
        return False
    cur = node
    # Each entry selector[i] = (combinator, compound_i): the combinator links
    # compound_{i-1} to compound_i. Walk from the element (innermost) outward,
    # resolving the ancestor compound of every combinators.
    for i in range(len(selector) - 1, 0, -1):
        comb = selector[i][0]
        prev_compound = selector[i - 1][1]
        if comb == " ":
            cur = _ancestor_matching(cur.parent, prev_compound)
            if cur is None:
                return False
        elif comb == ">":
            cur = cur.parent
            if cur is None or cur.tag == "#root" or not _match_compound(cur, prev_compound):
                return False
        else:
            return False
    return True


def _existing_style(attrs: Dict[str, str]) -> Dict[str, str]:
    style = attrs.get("style", "")
    out: Dict[str, str] = {}
    for chunk in style.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        prop, _, value = chunk.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        if prop and value:
            out[prop] = value
    return out


def inline_css(
    html: str,
    css: str,
    *,
    highlight_code: bool = True,
    pygments_style: str = "default",
) -> str:
    """Inline the given CSS into ``html`` and return the result.

    ``highlight_code`` runs Pygments over ``<pre><code>`` blocks (inline
    colors) before inlining; missing Pygments is silently tolerated.
    """
    root = _parse_html(html)
    if highlight_code:
        _highlight_code_blocks(root, pygments_style)
    rules = _parse_css_rules(css)
    for node in _iter_elems(root):
        style: Dict[str, str] = {}
        for selector, declarations, _index in rules:
            if _match_selector(node, selector):
                style.update(declarations)
        # Existing inline style (survived sanitization) wins over rules,
        # matching CSS cascade order.
        style.update(_existing_style(node.attrs))
        if style:
            node.attrs["style"] = (
                "; ".join(f"{k}: {v}" for k, v in style.items()) + ";"
            )
    return _serialize_nodes(root.children)
