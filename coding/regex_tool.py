"""正则表达式测试工具 — 匹配 / 捕获 / 替换核心逻辑。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

FLAG_MAP: Dict[str, int] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
    "u": re.UNICODE,
    "a": re.ASCII,
}

FLAG_NAMES: Dict[int, str] = {
    re.IGNORECASE: "IGNORECASE",
    re.MULTILINE: "MULTILINE",
    re.DOTALL: "DOTALL",
    re.VERBOSE: "VERBOSE",
    re.UNICODE: "UNICODE",
    re.ASCII: "ASCII",
}


class RegexError(ValueError):
    """Invalid pattern / replacement / flags."""


def parse_flags(raw: Optional[str]) -> int:
    """Parse a flag string like ``"i,m,s"`` or ``"imsx"`` into an int."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    value = 0
    for part in re.split(r"[\s,]+", raw):
        for ch in part:
            code = FLAG_MAP.get(ch.lower())
            if code is None:
                raise RegexError(f"未知的正则标志：{ch!r}（可用 i m s x u a）")
            value |= code
    return value


def flags_to_names(flags: int) -> List[str]:
    return [name for code, name in FLAG_NAMES.items() if flags & code]


def _compile(pattern: str, flags: int) -> "re.Pattern[str]":
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise RegexError(f"正则表达式有误：{exc}") from exc


def _match_info(index: int, match: "re.Match[str]") -> Dict[str, Any]:
    """Serialize one match including its capture groups."""
    num_to_name: Dict[int, Optional[str]] = {}
    for name, num in match.re.groupindex.items():
        num_to_name[num] = name
    groups: List[Dict[str, Any]] = []
    for i in range(match.re.groups + 1):
        try:
            start, end = match.span(i)
            groups.append(
                {
                    "index": i,
                    "name": num_to_name.get(i),
                    "span": [start, end],
                    "text": match.group(i) if start != -1 else None,
                }
            )
        except (IndexError, ValueError):
            groups.append({"index": i, "name": None, "span": [-1, -1], "text": None})
    return {
        "index": index,
        "span": [match.start(), match.end()],
        "text": match.group(0),
        "groups": groups,
    }


def test_regex(
    pattern: str,
    text: str,
    *,
    flags_raw: Optional[str] = None,
    count: int = 0,
) -> Dict[str, Any]:
    """Run ``pattern`` against ``text`` and report all matches.

    ``count`` (0 = all) limits how many matches to report.
    """
    flags = parse_flags(flags_raw)
    rx = _compile(pattern, flags)

    limit = max(0, int(count or 0))
    matches: List[Dict[str, Any]] = []
    for index, match in enumerate(rx.finditer(text)):
        if limit and index >= limit:
            break
        matches.append(_match_info(index, match))

    capture_names = list(rx.groupindex.keys())
    return {
        "ok": True,
        "pattern": pattern,
        "flags": flags_to_names(flags),
        "match_count": len(matches),
        "matches": matches,
        "group_count": rx.groups,
        "capture_names": capture_names,
    }


_JS_REPL_RE = re.compile(r"\$(\d+)|\$\{([^}]+)\}")


def _js_repl_to_py(replacement: str) -> str:
    """Translate JS-style backrefs (``$1`` / ``${name}``) to Python ``\\g<n>``."""

    def _sub(m: "re.Match[str]") -> str:
        num = m.group(1)
        name = m.group(2)
        if num:
            return f"\\g<{num}>"
        if name:
            return f"\\g<{name}>"
        return m.group(0)

    return _JS_REPL_RE.sub(_sub, replacement)


def _char_kind(ch: str) -> str:
    """Classify one character into a token family for pattern synthesis."""
    if ch.isdigit():
        return "digit"
    if ch.isspace():
        return "space"
    if ch.isalnum() or ch == "_":
        return "word"
    return "punct"


def _tokenize(text: str) -> List[Dict[str, Any]]:
    """Split ``text`` into consecutive same-kind tokens.

    Returns ``[{kind, start, end, text}, ...]``. ``end`` is exclusive.
    """
    tokens: List[Dict[str, Any]] = []
    i = 0
    n = len(text)
    while i < n:
        kind = _char_kind(text[i])
        j = i + 1
        while j < n and _char_kind(text[j]) == kind:
            j += 1
        tokens.append({"kind": kind, "start": i, "end": j, "text": text[i:j]})
        i = j
    return tokens


def _token_pattern(kind: str, segment: str) -> str:
    """One token family → its generalized pattern."""
    if kind == "digit":
        return r"\d{%d}" % len(segment)
    if kind == "word":
        return r"\w+"
    if kind == "space":
        return r"\s+"
    return re.escape(segment)


_ANCHOR_MODES = ("auto", "contains", "start", "end", "line")


def generate_regex(
    sample_text: str,
    target: str,
    *,
    flags_raw: Optional[str] = None,
    anchor: str = "auto",
) -> Dict[str, Any]:
    """Synthesize a regex to extract ``target`` from ``sample_text``.

    ``anchor`` controls how the match is positioned:
      - ``contains``  match anywhere it appears
      - ``start``     only at the start of a line
      - ``end``       only at the end of a line
      - ``line``      only on a whole line
      - ``auto``      pick ``start`` when the target sits at a line start,
                      otherwise ``contains``

    The target is tokenized (digits, words, spaces, punctuation) and each family
    is generalized (e.g. ``123`` → ``\\d{3}``), while identifier-style prefixes
    shared with other words are preserved (e.g. ``_Init8435`` → ``_Init\\w*``).
    The result wraps the match in a single capturing group.
    """
    flags = parse_flags(flags_raw)
    if target is None or str(target).strip() == "":
        raise RegexError("请先填写要提取的内容示例")

    # Locate the target (honor case-insensitive flag when set).
    if flags & re.IGNORECASE:
        probe = re.compile(re.escape(target), flags)
        m = probe.search(sample_text)
        idx = m.start() if m else -1
    else:
        idx = sample_text.find(target)

    if idx < 0:
        raise RegexError("在示例文本中找不到要提取的内容，请检查是否一致")

    # Generalize the target itself into a capturing-group pattern.
    target_tokens = _tokenize(target)
    siblings = set(re.findall(r"\w+", sample_text))

    # Identifier-style extraction: when the target's leading word is also a
    # prefix of *longer* sibling words (e.g. `_Init8435` next to `_InitECana`),
    # keep that literal prefix and generalize only the tail → `(_Init\w*)`.
    # This captures "everything starting with `_Init`" instead of a bare `\w+`.
    prefix_style = False
    if target_tokens and target_tokens[0]["kind"] == "word":
        lead = target_tokens[0]["text"]
        if any(w != target and len(w) > len(lead) and w.startswith(lead) for w in siblings):
            prefix_style = True

    if prefix_style:
        group = "(" + re.escape(target_tokens[0]["text"]) + r"\w*" + ")"
    elif len(target_tokens) == 1 and target_tokens[0]["kind"] == "word":
        # A lone identifier (e.g. `Init`) → keep it literal so `Init` matches
        # only the exact substring, not every word (avoids `\w+` over-matching).
        group = "(" + re.escape(target) + ")"
    else:
        parts: List[str] = []
        for t in target_tokens:
            parts.append(_token_pattern(t["kind"], t["text"]))
        group = "(" + "".join(parts) + ")"

    # Resolve the effective anchor.
    line_start = sample_text.rfind("\n", 0, idx) + 1
    at_line_start = idx == line_start
    mode = (anchor or "auto").strip().lower()
    if mode not in _ANCHOR_MODES:
        raise RegexError(f"未知的定位方式：{mode!r}（可用 auto contains start end line）")
    if mode == "auto":
        mode = "start" if at_line_start else "contains"

    lead_anchor = "^" if mode in ("start", "line") else ""
    tail_anchor = "$" if mode in ("end", "line") else ""

    suggest_flags: List[str] = []
    if mode in ("start", "line"):
        suggest_flags.append("m")
    pattern = lead_anchor + group + tail_anchor

    # Nearby literal context (one stable token each side) for optional anchors.
    tokens = _tokenize(sample_text)
    prefix: str = ""
    suffix: str = ""
    before = [t for t in tokens if t["end"] <= idx]
    if before and before[-1]["kind"] in ("word", "space", "punct"):
        prefix = before[-1]["text"]
    after = [t for t in tokens if t["start"] >= idx + len(target)]
    if after and after[0]["kind"] in ("word", "space", "punct"):
        suffix = after[0]["text"]

    return {
        "ok": True,
        "pattern": pattern,
        "plain": pattern,
        "prefix": prefix,
        "suffix": suffix,
        "target": target,
        "target_count": sample_text.count(target),
        "flags": flags_to_names(flags),
        "suggest_flags": suggest_flags,
        "line_anchored": at_line_start,
        "anchor": mode,
    }


def replace_regex(
    pattern: str,
    text: str,
    replacement: str,
    *,
    flags_raw: Optional[str] = None,
    count: int = 0,
) -> Dict[str, Any]:
    """Replace ``pattern`` occurrences in ``text`` with ``replacement``."""
    flags = parse_flags(flags_raw)
    rx = _compile(pattern, flags)
    repl = _js_repl_to_py(replacement)
    limit = max(0, int(count or 0))

    if count <= 0:
        new_text, n = rx.subn(repl, text)
    else:
        new_text, n = rx.subn(repl, text, count=limit)

    return {
        "ok": True,
        "result": new_text,
        "replace_count": n,
        "flags": flags_to_names(flags),
    }


__all__ = [
    "RegexError",
    "FLAG_MAP",
    "FLAG_NAMES",
    "parse_flags",
    "flags_to_names",
    "test_regex",
    "replace_regex",
    "generate_regex",
]
