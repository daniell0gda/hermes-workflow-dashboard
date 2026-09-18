"""Renders run documents from raw markdown.

Raw HTML in a report is escaped rather than passed through, so a report can
never inject markup. Image references resolve against that run's own published
artifacts; an unresolvable reference renders as its alt text instead of a broken
image or a hotlink.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any, Callable

from markdown_it import MarkdownIt

# Body headings start below the page's own h1/h2 so the outline stays sane.
HEADING_OFFSET = 2

MEDIA_RESOLVER_KEY = "hfcd_resolve_media"

_SAFE_HREF = re.compile(r"^(https?://|mailto:|#)", re.IGNORECASE)
_EXTERNAL = re.compile(r"^https?://", re.IGNORECASE)

MediaResolver = Callable[[str], str | None]


class MarkdownRenderer:
    def __init__(self) -> None:
        self._markdown = MarkdownIt(
            "default",
            {"html": False, "linkify": False, "typographer": False},
        )
        self._markdown.enable(["table", "strikethrough"])
        self._markdown.renderer.rules["image"] = self._render_image
        self._markdown.renderer.rules["link_open"] = self._render_link_open

    def render(self, text: str, resolve_media: MediaResolver | None = None) -> str:
        if not text or not text.strip():
            return ""

        env: dict[str, Any] = {MEDIA_RESOLVER_KEY: resolve_media}
        tokens = self._markdown.parse(text, env)
        _shift_headings(tokens)

        return self._markdown.renderer.render(tokens, self._markdown.options, env)

    def _render_image(self, tokens, index, options, env) -> str:  # noqa: ANN001
        token = tokens[index]
        alt = token.content or ""
        source = _resolve(token.attrGet("src") or "", env)

        if source is None:
            return (
                '<span class="hfcd-missing-media" '
                'title="Artifact not published with this run">'
                f'{escape(alt or "image")}</span>'
            )

        return (
            f'<img src="{escape(source, quote=True)}" alt="{escape(alt, quote=True)}" '
            'loading="lazy" decoding="async">'
        )

    def _render_link_open(self, tokens, index, options, env) -> str:  # noqa: ANN001
        href = tokens[index].attrGet("href") or ""

        if _SAFE_HREF.match(href):
            rel = ' rel="noopener noreferrer"' if _EXTERNAL.match(href) else ""
            return f'<a href="{escape(href, quote=True)}"{rel}>'

        resolved = _resolve(href, env)
        if resolved is not None:
            return f'<a href="{escape(resolved, quote=True)}">'

        # An anchor with no href stays balanced with the </a> from link_close
        # and simply reads as text.
        return "<a>"


def _resolve(reference: str, env: dict[str, Any]) -> str | None:
    reference = reference.strip()
    if not reference:
        return None
    if _EXTERNAL.match(reference):
        return reference

    resolver: MediaResolver | None = env.get(MEDIA_RESOLVER_KEY)
    if resolver is None:
        return None

    resolved = resolver(reference)

    return resolved or None


def _shift_headings(tokens) -> None:  # noqa: ANN001
    for token in tokens:
        if token.type not in ("heading_open", "heading_close"):
            continue
        level = int(token.tag[1:])
        token.tag = f"h{min(6, level + HEADING_OFFSET)}"


renderer = MarkdownRenderer()
