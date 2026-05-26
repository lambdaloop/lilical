"""Shared helpers for sanitising and rendering event description/notes text."""

from __future__ import annotations

import re
from html import escape, unescape

_URL_RE = re.compile(r"(https?://[^\s<>\"{}|\\^`\[\]]+)", re.IGNORECASE)
_HTML_TAG_RE = re.compile(
    r"<(?:html|body|head|meta|style|p|br|div|span|a|b|i|em|strong|ul|ol|li|"
    r"h[1-6]|table|tr|td|pre|img|font|o:p)"
    r"[\s/>]",
    re.IGNORECASE,
)
_HTML_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]{2,8}|#\d{1,5});")
_STYLE_ATTR_RE = re.compile(
    r'(style\s*=\s*)(["' + r"'])(.*?)\2", re.IGNORECASE | re.DOTALL
)
_STYLE_COLOR_DECL_RE = re.compile(
    r"(?:(?:^|(?<=;))\s*(?:color|background-color|background)\s*:[^;]*)",
    re.IGNORECASE,
)
_FONT_OPEN_TAG_RE = re.compile(r"<font\b([^>]*)>", re.IGNORECASE)
_FONT_COLOR_ATTR_RE = re.compile(
    r"""\s+color\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
_BGCOLOR_ATTR_RE = re.compile(
    r"""\s+bgcolor\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
_EXCHANGE_PLAINTEXT_RE = re.compile(
    r'<div\s+class\s*=\s*["\']?PlainText["\']?[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def is_html(text: str) -> bool:
    """Return True if *text* appears to contain HTML markup."""
    stripped = text.lstrip()
    if stripped.startswith(("<!DOCTYPE", "<!--[if", "<![if")):
        return True
    return bool(_HTML_TAG_RE.search(text) or _HTML_ENTITY_RE.search(text))


def unwrap_exchange_plaintext(html: str) -> str | None:
    """Unwrap an Exchange "converted from text" HTML body.

    Exchange wraps plain-text invite bodies by escaping < / > as entities,
    converting newlines to <br>, and placing everything in
    <div class="PlainText">. Returns the decoded inner content, or None if
    the input is not such a wrapper.
    """
    if "converted from text" not in html:
        return None
    m = _EXCHANGE_PLAINTEXT_RE.search(html)
    if not m:
        return None
    return unescape(_BR_RE.sub("\n", m.group(1)))


def strip_colors(html: str) -> str:
    """Strip inline color/background-color so the system palette wins."""

    def _scrub_style(m: re.Match[str]) -> str:
        cleaned = _STYLE_COLOR_DECL_RE.sub("", m.group(3)).strip(" ;")
        if not cleaned:
            return ""
        return f"{m.group(1)}{m.group(2)}{cleaned}{m.group(2)}"

    def _scrub_font(m: re.Match[str]) -> str:
        return f"<font{_FONT_COLOR_ATTR_RE.sub('', m.group(1))}>"

    html = _STYLE_ATTR_RE.sub(_scrub_style, html)
    html = _FONT_OPEN_TAG_RE.sub(_scrub_font, html)
    return _BGCOLOR_ATTR_RE.sub("", html)


def linkify(text: str) -> str:
    """Escape plain text for HTML, wrapping http(s) URLs in clickable links."""
    parts = _URL_RE.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(f'<a href="{escape(part)}">{escape(part)}</a>')
        else:
            out.append(escape(part).replace("\n", "<br>"))
    return "".join(out)


def format_notes_html(text: str) -> str:
    """Full pipeline: Exchange-unwrap → strip-colors (HTML) or linkify (plain).

    Returns HTML suitable for a RichText QLabel.
    """
    src = unwrap_exchange_plaintext(text) or text
    return strip_colors(src) if is_html(src) else linkify(src)
