"""
Scraper for Parliament committee evidence pages.

Fetches each oral/written evidence page and extracts:
  - A short excerpt (3-5 sentences, 60-120 words) from the opening paragraph
  - Word count of the full submission (for context only — text is discarded)
  - External URLs mentioned in the submission (non-Parliament, non-gov.uk)

Full submission text is NEVER stored. Approach: excerpt + attribution +
Parliament source link, defensible under CDPA s.30(1A) fair dealing for
quotation. See locked decision in plan file (16 May 2026).

Results are cached in CommitteeEvidenceContent (sd_evidence_content table).
Rows are never overwritten — re-runs skip already-cached URLs.
Blocked URLs (HTTP 403) are stored with fetch_status='blocked' and skipped
on subsequent runs.

Admin trigger: POST /admin action=fetch_evidence_content
"""
import json
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)
_URL_RE = re.compile(r'https?://[^\s\)\]>,"\'<]+|www\.[^\s\)\]>,"\'<]+')
_SKIP_DOMAINS = (
    'parliament.uk', 'gov.uk', 'twitter.com', 'facebook.com',
    'linkedin.com', 'youtube.com', 'instagram.com',
)
_IP_RE = re.compile(r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}')
_PORT_RE = re.compile(r'https?://[^/]+:\d{2,5}/')
_SHORTENER_DOMAINS = ('bit.ly', 'tinyurl.com', 't.co', 'ow.ly', 'goo.gl')
_BATCH_LIMIT = 100      # max URLs per admin trigger click
_SLEEP_BETWEEN = 0.5    # seconds between requests


def _is_safe_url(url: str) -> bool:
    if _IP_RE.match(url):
        return False
    if _PORT_RE.match(url):
        return False
    if any(s in url for s in _SHORTENER_DOMAINS):
        return False
    return True


def _extract_excerpt(text: str) -> tuple[str, str]:
    """
    Extract a 3-5 sentence excerpt using a consistent position rule:
    first substantive paragraph after any preamble.

    Returns (excerpt_text, excerpt_source). Excerpt source is a brief
    position note (e.g. "opening paragraph").

    Rule is positional, not editorial — we take the first qualifying text
    every time, never the "most interesting" part.
    """
    # Split into sentences at ./?/! followed by whitespace
    raw_sentences = re.split(r'(?<=[.!?])\s+', text)

    # A substantive sentence: 8+ words, not a header, not just a number/label
    def is_substantive(s: str) -> bool:
        s = s.strip()
        words = s.split()
        if len(words) < 8:
            return False
        if s.endswith(':'):
            return False
        if re.match(r'^\d+\.?\s', s):  # numbered list item
            return False
        return True

    substantive = [s.strip() for s in raw_sentences if is_substantive(s)]

    if not substantive:
        return ('', 'no substantive content found')

    # Build excerpt: aim for 3-5 sentences, 60-120 words
    excerpt_parts: list[str] = []
    word_count = 0
    for s in substantive[:8]:  # scan up to 8 sentences
        s_words = len(s.split())
        # Stop if we already have 3+ sentences and adding this would exceed 150 words
        if word_count + s_words > 150 and len(excerpt_parts) >= 3:
            break
        excerpt_parts.append(s)
        word_count += s_words
        # Stop at 5 sentences or once we're comfortably over 60 words with 3+
        if len(excerpt_parts) >= 5:
            break
        if word_count >= 60 and len(excerpt_parts) >= 3:
            break

    return (' '.join(excerpt_parts), 'opening paragraph')


def fetch_one(source_url: str) -> dict:
    """Fetch a single Parliament evidence page and extract excerpt + metadata."""
    try:
        r = requests.get(source_url, headers={'User-Agent': _UA}, timeout=20)
    except Exception as e:
        return {'status': 'failed', 'error': str(e)}

    if r.status_code == 403:
        return {'status': 'blocked'}
    if r.status_code != 200:
        return {'status': 'failed', 'error': f'HTTP {r.status_code}'}

    soup = BeautifulSoup(r.text, 'html.parser')
    for tag in soup(['nav', 'header', 'footer', 'script', 'style']):
        tag.decompose()
    full_text = soup.get_text(separator=' ', strip=True)

    # Word count of full submission — stored for context, text itself is discarded
    word_count_original = len(full_text.split())

    excerpt_text, excerpt_source = _extract_excerpt(full_text)

    raw_urls = set(_URL_RE.findall(full_text))
    org_urls = sorted(
        u for u in raw_urls
        if not any(d in u for d in _SKIP_DOMAINS)
        and _is_safe_url(u)
    )[:20]

    # Full text is discarded here — never stored or written to disk
    return {
        'status': 'fetched',
        'excerpt_text': excerpt_text,
        'excerpt_source': excerpt_source,
        'word_count_original': word_count_original,
        'external_urls': json.dumps(org_urls),
    }


def run_fetch(app, org_id=None):
    """
    Fetch and cache evidence excerpts for unfetched source URLs.

    Capped at _BATCH_LIMIT URLs per run. Sleeps _SLEEP_BETWEEN seconds between
    requests. Designed to run in a background daemon thread.
    """
    from extensions import db
    from stakeholder_directory.models import Engagement, CommitteeEvidenceContent

    with app.app_context():
        q = (
            db.session.query(Engagement.source_url)
            .filter(Engagement.source_type.in_(
                ['oral_evidence_committee', 'written_evidence_committee']
            ))
            .distinct()
        )
        if org_id:
            q = q.filter(Engagement.organisation_id == org_id)

        already = {
            r[0] for r in
            db.session.query(CommitteeEvidenceContent.source_url).all()
        }
        to_fetch = [
            r[0] for r in q.all()
            if r[0] and r[0] not in already
        ][:_BATCH_LIMIT]

        fetched = blocked = failed = 0
        for url in to_fetch:
            result = fetch_one(url)
            db.session.add(CommitteeEvidenceContent(
                source_url=url,
                fetched_at=datetime.utcnow(),
                fetch_status=result['status'],
                excerpt_text=result.get('excerpt_text'),
                excerpt_source=result.get('excerpt_source'),
                word_count_original=result.get('word_count_original'),
                external_urls=result.get('external_urls'),
            ))
            db.session.commit()

            if result['status'] == 'fetched':
                fetched += 1
            elif result['status'] == 'blocked':
                blocked += 1
            else:
                failed += 1

            time.sleep(_SLEEP_BETWEEN)

        remaining = len([r[0] for r in q.all() if r[0] and r[0] not in already]) - len(to_fetch)
        app.logger.info(
            f'evidence_content fetch done: {fetched} fetched, '
            f'{blocked} blocked, {failed} failed. '
            f'Remaining unfetched: {remaining}'
        )
