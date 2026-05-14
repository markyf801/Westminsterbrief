"""
HTML sanitiser for Written Question answer content from Parliament WQ API.

Parliament's API returns answer HTML that may contain structural markup
(paragraphs, tables, lists). We preserve structural tags but strip dangerous
elements and all non-allowlisted attributes.

Usage:
    from hansard_archive.html_sanitizer import sanitize_answer_html, to_plain_text

    # For storage — render in templates with {{ answer_text | safe }}:
    safe_html = sanitize_answer_html(raw_html)

    # For Word/CSV exports — strip all tags to readable plain text:
    plain = to_plain_text(safe_html)

XSS posture: Parliament's WQ API is the sole source. Bleach sanitises at
ingestion time. Templates render sanitised-at-ingest content with | safe.
This is the only location in the codebase where | safe is used on
externally-sourced content; keep it that way.
"""

import re

import bleach

# Tags that carry structural meaning in Parliament answer HTML.
# <a> excluded initially — the Parliament.uk source link is always on the page.
# Add back after reviewing link examples if needed.
ALLOWED_TAGS = [
    "p", "br", "div",
    "table", "thead", "tbody", "tr", "th", "td",
    "ul", "ol", "li",
    "strong", "em", "b", "i",
    "h1", "h2", "h3", "h4", "h5", "h6",
]

# Minimal attribute allowlist — accessibility/structural only.
ALLOWED_ATTRIBUTES = {
    "th": ["scope"],
    "table": ["summary"],
}


def sanitize_answer_html(html: str | None) -> str | None:
    """
    Sanitise Parliament answer HTML for safe storage and rendering with | safe.

    Preserves structural tags (tables, paragraphs, lists).
    Strips <script>, <style>, event handlers, and all other dangerous content.
    Strips <a> hrefs — Parliament.uk source link is always present on the page.

    Returns None if input is empty or whitespace-only after sanitisation.
    """
    if not html:
        return None
    cleaned = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
    cleaned = cleaned.strip()
    return cleaned or None


def to_plain_text(html: str | None) -> str:
    """
    Convert sanitised answer HTML to plain text for Word/CSV exports.

    Replaces block-level tags with newlines to preserve paragraph structure
    in the exported document.
    """
    if not html:
        return ""
    text = re.sub(r"<(?:p|br|div|li|tr)[^>]*>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
