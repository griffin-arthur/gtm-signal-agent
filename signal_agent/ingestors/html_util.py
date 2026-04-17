"""HTML → plain text utility used by ingestors.

Uses `html.parser` for robust stripping (handles self-closing tags, attributes
with quoted < or > characters, multi-line tags) rather than a naive regex.
Also collapses whitespace and decodes entities.

Kept in its own module so news/SEC/job ingestors share the same cleaning.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_WS_RE = re.compile(r"\s+")
# Block-level tags that should insert a newline when closed so paragraphs stay
# readable in Slack and in the LLM prompt.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote",
}
# Tags whose contents we drop entirely.
_DROP_TAGS = {"script", "style", "noscript"}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth += 1
        elif tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._drop_depth > 0:
            self._drop_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0:
            self._parts.append(data)

    def value(self) -> str:
        raw = "".join(self._parts)
        # Collapse consecutive whitespace *within* a line, but preserve line breaks.
        lines = [_WS_RE.sub(" ", ln).strip() for ln in raw.splitlines()]
        # Drop empty lines, join single-spaced.
        return "\n".join(ln for ln in lines if ln).strip()


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    parser = _Stripper()
    try:
        parser.feed(s)
        parser.close()
    except Exception:
        # Malformed HTML — fall back to a naive regex strip rather than crashing.
        return _WS_RE.sub(" ", re.sub(r"<[^>]+>", " ", s)).strip()
    return parser.value()
