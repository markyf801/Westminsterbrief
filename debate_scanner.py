import requests, os, json, re, io, concurrent.futures, time
from flask import Blueprint, render_template, request, send_file, copy_current_request_context, jsonify
from flask_login import current_user, login_required
from datetime import datetime
from cache_models import CachedSearch, StakeholderOrg
from extensions import limiter
from ministers import display_name as _display_name, normalise_name as _normalise_name
try:
    import feedparser
except ImportError:
    feedparser = None

# Import Word Document libraries
try:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
except ImportError:
    Document = None
    OxmlElement = None
    qn = None

def _add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    c = OxmlElement('w:color'); c.set(qn('w:val'), '0000FF'); rPr.append(c)
    u = OxmlElement('w:u'); u.set(qn('w:val'), 'single'); rPr.append(u)
    new_run.append(rPr)
    t = OxmlElement('w:t'); t.text = text; new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)

debate_scanner_bp = Blueprint('debates', __name__)


@debate_scanner_bp.app_template_filter('debate_url')
def debate_url_filter(url):
    """Return a debate URL for a template link. Rows carry full Hansard URLs or
    internal paths (e.g. /archive/debate/...), both of which work as-is."""
    return url or ''


SITE_URL = "https://westminsterbrief.co.uk"


def _full_url(url):
    """Absolute URL for Word exports: internal paths get our own domain."""
    if not url:
        return ''
    if url.startswith('http'):
        return url
    return SITE_URL + url if url.startswith('/') else ''


def _make_hansard_slug(title):
    """Derive URL path segment for hansard.parliament.uk from debate title.
    Hansard expects PascalCase with no spaces or special chars, e.g. 'StudentLoans'."""
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', title or '')
    return ''.join(w.capitalize() for w in clean.split())

# OUTPUT RULE COMPLIANCE
# This is a free toolkit feature. Per CLAUDE.md output rules, AI calls in this
# module must use Gemini only. Claude (Anthropic) must not be invoked from this
# code path under any circumstances, including failure or fallback scenarios.
# Phase 2 paid-product modules may call Claude — those will be in a separate
# paid/ namespace with explicit model binding enforced at the abstraction layer.
# When Gemini fails: retry once with backoff, then degrade gracefully.
# Never swap to a different provider silently.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
HANSARD_API_BASE = "https://hansard-api.parliament.uk"
MINISTER_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'minister_cache.json')
MINISTER_CACHE_TTL = 30 * 24 * 3600  # 30 days — reshuffles are infrequent
MINISTER_CACHE_VERSION = 3  # increment when display_name logic changes to bust stale files

# Verified Parliament member ids (MNIS ids, = ha_contribution.member_id) for
# ministers whose GOV.UK name has been hard to match by name. Verified 2026-04.
KNOWN_MINISTER_MEMBER_IDS = {
    'Baroness Smith of Malvern': {'parliament_id': 269, 'is_lord': True},
    'Josh MacAlister': {'parliament_id': 5033, 'is_lord': False},
    'Georgia Gould': {'parliament_id': 5305, 'is_lord': False},
}

RESEARCH_DEPARTMENTS = [
    "Attorney General's Office",
    "Cabinet Office",
    "Department for Business and Trade",
    "Department for Culture, Media and Sport",
    "Department for Education",
    "Department for Energy Security and Net Zero",
    "Department for Environment, Food and Rural Affairs",
    "Department for Science, Innovation and Technology",
    "Department for Transport",
    "Department for Work and Pensions",
    "Department of Health and Social Care",
    "Foreign, Commonwealth and Development Office",
    "HM Revenue and Customs",
    "HM Treasury",
    "Home Office",
    "Ministry of Defence",
    "Ministry of Housing, Communities and Local Government",
    "Ministry of Justice",
    "Northern Ireland Office",
    "Office of the Leader of the House of Commons",
    "Office of the Leader of the House of Lords",
    "Scotland Office",
    "Wales Office",
]

PARLIAMENT_WQ_API = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
PARLIAMENT_WMS_API = "https://questions-statements-api.parliament.uk/api/writtenstatements/statements"
PARLIAMENT_DEPT_IDS = {
    "Department for Education": 60, "Department of Health and Social Care": 17,
    "HM Treasury": 14, "Home Office": 1, "Ministry of Defence": 11,
    "Ministry of Justice": 54, "Department for Science, Innovation and Technology": 216,
    "Cabinet Office": 53,
}
PARTY_COLOURS_RESEARCH = {
    'Labour': '#E4003B', 'Conservative': '#0087DC', 'Liberal Democrat': '#FAA61A',
    'Scottish National Party': '#FDF38E', 'Green Party': '#02A95B', 'Reform UK': '#12B6CF',
    'Plaid Cymru': '#005B54', 'Democratic Unionist Party': '#D46A4C',
}

_MODEL_CACHE = {}
_VERIFY_CACHE = {}


def _gemini_headers(api_key):
    # Key in a header, not the URL, so it can't leak into exception text or logs.
    return {"x-goog-api-key": api_key}


def research_ai_summary_enabled() -> bool:
    """The Research Tool's AI summary is off unless RESEARCH_AI_SUMMARY=on.

    Switched off on 22 Oct 2026: a scenario test found it inventing facts
    (wrong date range, unsupported party positions, current titles for past
    roles). It stays off until the prompt is fixed and a model passes the
    accuracy comparison (docs/plans/backlog.md).
    """
    return os.environ.get('RESEARCH_AI_SUMMARY', '').strip().lower() in ('on', '1', 'true', 'yes')


def get_working_model(api_key):
    if api_key in _MODEL_CACHE:
        return _MODEL_CACHE[api_key]
    result = 'models/gemini-2.5-flash-lite'
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        resp = requests.get(url, headers=_gemini_headers(api_key), timeout=5)
        if resp.status_code == 200:
            available = [m['name'] for m in resp.json().get('models', [])
                         if 'generateContent' in m.get('supportedGenerationMethods', [])]
            for prefix in ['models/gemini-2.5-flash-lite', 'models/gemini-2.5-flash',
                           'models/gemini-flash-latest']:
                match = next((m for m in available if m.startswith(prefix)), None)
                if match:
                    result = match
                    break
            else:
                if available:
                    result = available[0]
    except Exception:
        pass
    _MODEL_CACHE[api_key] = result
    return result

def format_date_range(start_str, end_str):
    def parse_date(d_str):
        if not d_str: return ""
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d_str): return d_str.replace('-', '')
        if '/' in d_str or '-' in d_str:
            parts = d_str.replace('/', '-').split('-')
            if len(parts) == 3 and len(parts[2]) == 4:
                return f"{parts[2]}{parts[1].zfill(2)}{parts[0].zfill(2)}"
        return d_str.replace('-', '').replace('/', '')

    s = parse_date(start_str)
    e = parse_date(end_str)

    if s and e: return f"{s}..{e}"
    elif s: return f"{s}..{datetime.now().strftime('%Y%m%d')}"
    elif e: return f"19000101..{e}"
    return ""

def _get_user_pref():
    """Return the current user's UserPreference or None."""
    try:
        from flask_app import UserPreference
        if current_user.is_authenticated:
            return UserPreference.query.filter_by(user_id=current_user.id).first()
    except Exception:
        pass
    return None

def get_debate_type(title, source=None):
    t = title.lower()
    if source == 'wms': return '📜 Ministerial Statement'
    if source == 'westminsterhall': return '🏛️ Westminster Hall'
    if 'written answers' in t or 'written answer' in t: return '✍️ Written Answer'
    if 'urgent question' in t: return '❗ Urgent Question'
    if 'oral answers' in t or 'question time' in t: return '🗣️ Oral Question'
    if 'prime minister' in t and 'question' in t: return '🗣️ Oral Question'
    if source == 'commons' and any(t.startswith(p + ':') for p in (
        'education', 'health', 'treasury', 'home office', 'defence', 'justice',
        'transport', 'environment', 'science', 'work and pensions', 'cabinet office',
        'foreign', 'culture', 'housing', 'digital', 'northern ireland', 'business',
    )): return '🗣️ Oral Question'
    if 'statement' in t: return '📜 Ministerial Statement'
    if ('statutory instrument' in t or 'affirmative' in t or 'delegated legislation' in t
            or ('draft' in t and ('regulation' in t or 'order' in t))):
        return '⚖️ Statutory Instrument'
    if 'bill' in t or 'reading' in t or 'amendment' in t: return '⚖️ Legislation'
    if 'motion' in t: return '📝 Motion'
    return '💬 General Debate'

def clean_body_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _parse_ai_json(text):
    """Robustly parse JSON from an AI response.
    Handles markdown fences, trailing commas, and other common AI formatting issues."""
    if not text:
        return None
    # Strip markdown fences
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Remove trailing commas before } or ] (valid JS, invalid JSON)
    cleaned = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Try to extract just the outermost JSON object or array
    m = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            cleaned2 = re.sub(r',\s*([}\]])', r'\1', m.group())
            try:
                return json.loads(cleaned2)
            except json.JSONDecodeError:
                pass
    return None

def get_source_label(source):
    return {'commons': 'Commons', 'westminsterhall': 'Westminster Hall',
            'lords': 'Lords', 'wrans': 'Written Answer',
            'wms': 'Ministerial Statement'}.get(source, source.title())

# Sources return a mix of abbreviated and full party names — normalise to canonical
# full names so set membership checks work consistently everywhere in the pipeline.
_PARTY_NORM = {
    'snp':          'Scottish National Party',
    'ld':           'Liberal Democrat',
    'lib dem':      'Liberal Democrat',
    'con':          'Conservative',
    'lab':          'Labour',
    'green':        'Green Party',
    'dup':          'Democratic Unionist Party',
    'uup':          'Ulster Unionist Party',
    'sdlp':         'Social Democratic and Labour Party',
    'pc':           'Plaid Cymru',
    'reform':       'Reform UK',
    'ind':          'Independent',
    'alba':         'Alba Party',
    'sf':           'Sinn Féin',
    'alliance':     'Alliance Party of Northern Ireland',
    'crossbench':   'Crossbench',
}

def _normalise_party(raw):
    """Normalise party strings to canonical full names."""
    if not raw:
        return ''
    return _PARTY_NORM.get(raw.strip().lower(), raw.strip())


def _extract_title_queries(search_term):
    """Return a list of shorter search terms suitable for /search/debates.json title matching.
    The Hansard debates endpoint matches debate titles, so we need concise phrases.
    Tries: (1) quoted phrases extracted from an AI-expanded query, (2) first 2-3 words of term."""
    queries = []
    # Extract quoted phrases from Boolean-expanded queries like '"student loan" OR "tuition fees"'
    quoted = re.findall(r'"([^"]+)"', search_term)
    queries.extend(quoted)
    # Also add first 2 words of the raw term (covers simple non-expanded terms)
    words = search_term.replace('"', '').split()
    if len(words) >= 2:
        short = ' '.join(words[:2])
        if short not in queries:
            queries.append(short)
    # Add the full term as a fallback (works when term is already short)
    if search_term not in queries:
        queries.insert(0, search_term)
    return queries[:6]  # cap to avoid excess API calls


def fetch_hansard_topic(search_term, source_type, date_range, num=50):
    """Hansard Parliament API keyword/title search.

    Uses two mechanisms:
      /search/debates.json — title matching (finds sessions titled about the topic)
      /search.json         — speech-level matching (supplementary, ~4 results)

    Both return DebateSectionExtId, which session expansion then expands to all speeches.
    WMS (source_type='wms') is handled by fetch_parliament_wms(); this function returns [] for it.
    """
    import logging as _htl

    if source_type == 'wms':
        return []  # WMS uses fetch_parliament_wms() via debates_topic dispatcher

    # Parse YYYYMMDD..YYYYMMDD → ISO dates
    start_date_api = end_date_api = ''
    if date_range and '..' in date_range:
        parts = date_range.split('..')
        def _fmt(d): return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        start_date_api = _fmt(parts[0].strip()) if parts[0].strip() else ''
        end_date_api = _fmt(parts[1].strip()) if parts[1].strip() else ''

    cache_key = f"hansard:topic:{source_type}:{search_term}:{date_range}".strip()
    cached = CachedSearch.get(cache_key, source_type, ttl_hours=6)
    if cached is not None:
        _htl.warning(f"[hansard_topic] {source_type} from_cache rows={len(cached)}")
        return cached

    house = 'Lords' if source_type == 'lords' else 'Commons'
    base_params = {'queryParameters.house': house}
    if start_date_api:
        base_params['queryParameters.startDate'] = start_date_api
    if end_date_api:
        base_params['queryParameters.endDate'] = end_date_api

    # --- Step 1: /search/debates.json — title-based session discovery ---
    ext_ids_meta = {}  # ext_id -> {date, title, house}
    title_queries = _extract_title_queries(search_term)
    _htl.warning(f"[hansard_topic_title] source={source_type} title_queries={title_queries}")
    for tq in title_queries:
        try:
            resp = requests.get(
                f"{HANSARD_API_BASE}/search/debates.json",
                params={**base_params, 'queryParameters.searchTerm': tq},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            d = resp.json()
            for r in (d.get('Results') or []):
                ext_id = r.get('DebateSectionExtId', '')
                if not ext_id or ext_id in ext_ids_meta:
                    continue
                section_lower = (r.get('DebateSection', '') or '').lower()
                if source_type == 'westminsterhall' and 'westminster hall' not in section_lower:
                    continue
                if source_type == 'commons' and 'westminster hall' in section_lower:
                    continue
                sitting = r.get('SittingDate', '') or ''
                ext_ids_meta[ext_id] = {
                    'date': sitting[:10],
                    'title': r.get('Title', ''),
                    'house': r.get('House', house),
                    'rank': r.get('Rank', 0),
                }
            _htl.warning(f"[hansard_topic_title] q={tq!r} total={d.get('TotalResultCount',0)} new_ids={len(ext_ids_meta)}")
        except Exception as e:
            _htl.warning(f"[hansard_topic_title] q={tq!r} error={e}")

    # --- Step 2: /search.json — speech-level keyword matches (supplementary, ~4 per call) ---
    contribution_rows = []
    try:
        resp = requests.get(
            f"{HANSARD_API_BASE}/search.json",
            params={**base_params, 'queryParameters.searchTerm': search_term},
            timeout=25,
        )
        if resp.status_code == 200:
            data = resp.json()
            contribs = data.get('Contributions', [])
            _htl.warning(f"[hansard_topic_contrib] source={source_type} total={data.get('TotalContributions','?')} returned={len(contribs)}")
            for r in contribs:
                h = r.get('House', 'Commons')
                section = r.get('Section', '')
                src = _hansard_section_to_source(h, section)
                if src != source_type:
                    continue
                ext_id = r.get('DebateSectionExtId', '') or ''
                body_raw = r.get('ContributionTextFull', '') or r.get('ContributionText', '') or ''
                body_text = clean_body_text(body_raw)
                sitting = r.get('SittingDate', '')
                hdate = sitting[:10] if sitting else ''
                house_path = 'lords' if h.lower() == 'lords' else 'commons'
                debate_title = r.get('DebateSection', '') or ''
                slug = _make_hansard_slug(debate_title)
                listurl = (f"https://hansard.parliament.uk/{house_path}/{hdate}/debates/{ext_id}/{slug}"
                           if ext_id and hdate else '')
                dtype = get_debate_type(debate_title, source=src)
                contribution_rows.append({
                    'listurl': listurl,
                    'body_clean': body_text[:500],
                    'body_export': body_text[:3000],
                    'body_word_count': len(body_text.split()),
                    'speaker_name': r.get('MemberName', '') or '',
                    'speaker_party': _parse_hansard_party(r.get('AttributedTo', '')),
                    'hdate': hdate,
                    'debate_title': debate_title,
                    'source': src,
                    'source_label': get_source_label(src),
                    'relevance': r.get('Rank', 0),
                    'debate_type': dtype,
                    'debate_section_ext_id': ext_id,
                })
                if ext_id and ext_id not in ext_ids_meta:
                    ext_ids_meta[ext_id] = {'date': hdate, 'title': debate_title, 'house': h, 'rank': r.get('Rank', 0)}
    except Exception as e:
        _htl.warning(f"[hansard_topic_contrib] error={e}")

    # --- Build final row list: real contribution rows + stub rows for title-only sessions ---
    contrib_ext_ids = {r.get('debate_section_ext_id', '') for r in contribution_rows}
    all_rows = list(contribution_rows)
    for ext_id, meta in ext_ids_meta.items():
        if ext_id in contrib_ext_ids:
            continue
        hdate = meta.get('date', '')
        title = meta.get('title', '')
        h = meta.get('house', house)
        house_path = 'lords' if h.lower() == 'lords' else 'commons'
        slug = _make_hansard_slug(title)
        listurl = (f"https://hansard.parliament.uk/{house_path}/{hdate}/debates/{ext_id}/{slug}"
                   if ext_id and hdate else '')
        dtype = get_debate_type(title, source=source_type)
        all_rows.append({
            'listurl': listurl,
            'body_clean': '',
            'body_export': '',
            'body_word_count': 0,
            'speaker_name': '',
            'speaker_party': '',
            'hdate': hdate,
            'debate_title': title,
            'source': source_type,
            'source_label': get_source_label(source_type),
            'relevance': meta.get('rank', 0),
            'debate_type': dtype,
            'debate_section_ext_id': ext_id,
        })

    _htl.warning(f"[hansard_topic] source={source_type} contribution_rows={len(contribution_rows)} "
                 f"title_stubs={len(all_rows)-len(contribution_rows)} total={len(all_rows)} "
                 f"unique_sessions={len(ext_ids_meta)}")

    if all_rows:
        CachedSearch.store(cache_key, source_type, all_rows)
    return all_rows


def deduplicate_by_listurl(rows):
    seen = set()
    out = []
    for r in rows:
        url = r.get('listurl', '')
        if not url:
            out.append(r)  # no URL — always keep, never dedup
            continue
        if url.startswith('https://hansard.parliament.uk'):
            # Hansard uses session-level URLs shared by all speeches in a session.
            # Include speaker to keep every contribution rather than collapsing to one.
            key = (url, r.get('speaker_name', ''))
        else:
            key = url
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out

def format_briefing_as_text(briefing_dict, topic):
    """Converts structured AI briefing dict to a markdown string for Word export."""
    lines = [f"## PARLIAMENTARY BRIEFING: {topic.upper()}\n"]
    lines.append(f"## 1. TOPIC SUMMARY\n{briefing_dict.get('topic_summary', '')}\n")
    lines.append(f"## 2. GOVERNMENT POSITION\n{briefing_dict.get('government_position', '')}\n")
    lines.append(f"## 3. OPPOSITION POSITION\n{briefing_dict.get('opposition_position', '')}\n")

    govt_speakers = briefing_dict.get('government_speakers', [])
    if govt_speakers:
        lines.append("## 4. GOVERNMENT SPEAKERS")
        for s in govt_speakers:
            lines.append(f"- {s.get('name', '')} ({s.get('role', '')}): {s.get('stance', '')}")
        lines.append("")

    non_govt_speakers = briefing_dict.get('non_government_speakers', [])
    if non_govt_speakers:
        lines.append("## 5. NON-GOVERNMENT SPEAKERS (Opposition / Backbench)")
        for s in non_govt_speakers:
            lines.append(f"- {s.get('name', '')} ({s.get('role_or_party', '')}): {s.get('stance', '')}")
        lines.append("")

    questions = briefing_dict.get('key_questions', [])
    if questions:
        lines.append("## 6. KEY OPPOSITION QUESTIONS")
        for q in questions:
            lines.append(f"- {q.get('speaker', '')} ({q.get('role_or_party', '')}, {q.get('date', '')}): {q.get('question', '')}")
        lines.append("")

    anticipated = briefing_dict.get('anticipated_questions', [])
    if anticipated:
        lines.append("## 7. ANTICIPATED QUESTIONS (AI-GENERATED)")
        for q in anticipated:
            lines.append(f"- {q.get('question', '')}")
            if q.get('rationale'):
                lines.append(f"  [Why: {q.get('rationale', '')}]")
        lines.append("")

    key_statements = briefing_dict.get('key_ministerial_statements', [])
    if key_statements:
        lines.append("## 8. KEY MINISTERIAL STATEMENTS (ON-RECORD)")
        for s in key_statements:
            speaker_line = f"{s.get('speaker', '')} ({s.get('role', '')}), {s.get('date', '')}"
            lines.append(f"- {speaker_line}: \"{s.get('statement', '')}\"")
            if s.get('listurl'):
                lines.append(f"  Source: {s.get('listurl', '')}")
        lines.append("")

    lines.append(f"## 9. NEXT STEPS\n{briefing_dict.get('next_steps', '')}\n")
    lines.append(f"## 10. COVERAGE NOTE\n{briefing_dict.get('coverage_note', '')}\n")
    return "\n".join(lines)

def expand_search_query(topic, api_key):
    """Use Gemini to expand a topic into related parliamentary search terms."""
    # Policy vocabulary is stable — cache for 7 days to skip Gemini on repeat searches
    try:
        cached = CachedSearch.get(topic, '_expand', ttl_hours=168)
        if cached is not None:
            return cached[0] if cached else f'"{topic}"'
    except Exception:
        pass
    try:
        model_path = get_working_model(api_key)
        ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent"
        prompt = (
            f"You are a UK Parliamentary researcher. A user wants to search Hansard for: \"{topic}\"\n"
            "Give up to 5 additional search phrases that refer to THE SAME SPECIFIC POLICY OR TOPIC "
            "using vocabulary MPs and Lords actually use in Parliament.\n"
            "STRICT RULES:\n"
            "- Only include terms that directly refer to this specific policy — not broader related topics\n"
            "- Include the policy acronym if one exists (e.g. 'LLE', 'SEND', 'HTQ', 'ICR')\n"
            "- Include any previous official name for the policy if it was renamed\n"
            "- Include the specific legislation name if relevant (e.g. 'Skills and Post-16 Education Act')\n"
            "- Do NOT include generic financial, legal, or policy terms that could match unrelated debates\n"
            "- Do NOT include synonyms that are broader than the original topic\n"
            "Return ONLY a JSON array of strings, no explanation, no markdown.\n"
            "Examples:\n"
            "'student loan repayments' → [\"repayment threshold\", \"Plan 2 loans\", \"income-contingent repayment\", \"ICR\", \"loan write-off\"]\n"
            "'lifelong learning entitlement' → [\"LLE\", \"lifelong loan entitlement\", \"Skills and Post-16 Education Act\", \"modular learning\"]"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"responseMimeType": "application/json"}}
        resp = requests.post(ai_url, headers=_gemini_headers(api_key), json=payload, timeout=15)
        raw = None
        if resp.status_code == 200:
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            import logging as _eqlog
            _eqlog.warning('[expand_search_query] Gemini returned %s — using original query', resp.status_code)
        if raw:
            terms = _parse_ai_json(raw)
            if isinstance(terms, list) and terms:
                # Only include the original topic phrase if it's short enough to
                # appear verbatim in debates. Long policy descriptions (> 4 words)
                # are never said word-for-word and return 0 results from a phrase search.
                if len(topic.split()) <= 4:
                    all_terms = [topic] + [str(t) for t in terms[:6]]
                else:
                    all_terms = [str(t) for t in terms[:6]]
                if not all_terms:
                    return f'"{topic}"'
                result = '(' + ' OR '.join(f'"{t}"' for t in all_terms) + ')'
                try:
                    CachedSearch.store(topic, '_expand', [result])
                except Exception:
                    pass
                return result
    except Exception:
        pass
    return f'"{topic}"'


def _group_by_debate(rows):
    """Group speeches by debate (date + title). Within each group, ministers first.
    Returns list of (debate_key_dict, [rows]) sorted: minister-debates first, then most recent."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        key = (r.get('hdate', ''), r.get('debate_title', ''))
        groups[key].append(r)
    for key in groups:
        groups[key].sort(key=lambda x: (not x.get('is_minister', False), -x.get('relevance', 0)))
    # Minister-containing debates first (1 > 0), then most recent date first (reverse string sort)
    group_list = sorted(
        groups.items(),
        key=lambda kv: (1 if any(r.get('is_minister') for r in kv[1]) else 0, kv[0][0]),
        reverse=True
    )
    result = []
    for k, v in group_list:
        matched = sum(1 for r in v if r.get('relevance', 0) > 0)
        if matched >= 3:
            rel_level = 'high'
        elif matched >= 1:
            rel_level = 'medium'
        else:
            rel_level = 'low'
        result.append({
            'date': k[0], 'title': k[1], 'speeches': v,
            'has_minister': any(r.get('is_minister') and not r.get('minister_unverified') for r in v),
            'source_label': v[0].get('source_label', '') if v else '',
            'source': v[0].get('source', '') if v else '',
            'matched_count': matched,
            'relevance_level': rel_level,
        })
    return result


def _classify_group(grp):
    """Classify a debate group into a section.
    Uses title patterns first (definitive), then word-count heuristic.
    A group where no speech exceeds 300 words is likely Oral Questions —
    the minister's prepared answer is ~150 words, follow-ups shorter still.
    A 10-minute debate speech runs ~1300 words."""
    source = grp.get('source', '')
    title = grp.get('title', '').lower()
    speeches = grp.get('speeches', [])

    # Normalise dashes so em-dash/en-dash variants match consistently
    title_norm = re.sub(r'[–—]', '-', title)

    if source == 'wms':
        return 'statement'
    # Written answers must never reach _group_by_debate — filtered out upstream.
    # If any slip through, return 'wq' as a sentinel so they don't appear in Debates.
    if 'written answers' in title or 'written answer' in title:
        return 'wq'
    if (re.search(r'\bbill\b', title) or 'second reading' in title or 'third reading' in title
            or 'committee stage' in title or 'report stage' in title
            or 'lords amendments' in title or 'royal assent' in title):
        return 'legislation'
    if 'urgent question' in title:
        return 'urgent'
    # Commons oral statements: titled "Statement on X" (source=commons, not wms)
    if source == 'commons' and 'statement' in title:
        return 'statement'
    if 'oral answers' in title or 'question time' in title:
        return 'oral'
    if 'prime minister' in title and 'question' in title:
        return 'oral'
    # Lords oral questions — normalised title so em-dash/hyphen variants both match
    if source == 'lords' and (title_norm.rstrip().endswith('- question') or
                              '- oral question' in title_norm or
                              '- question' in title_norm):
        return 'oral'
    # Commons oral questions: "Department: Topic" format
    _OQ_DEPT_PREFIXES = (
        'education', 'health', 'treasury', 'home office', 'defence', 'justice',
        'transport', 'environment', 'science', 'work and pensions', 'cabinet office',
        'foreign', 'culture', 'housing', 'digital', 'northern ireland', 'business',
        'wales', 'scotland', 'attorney general', 'leader of the house',
        'women and equalities', 'energy security', 'levelling up',
        'international trade', 'international development',
    )
    if source == 'commons' and any(title.startswith(p + ':') for p in _OQ_DEPT_PREFIXES):
        return 'oral'
    if 'topical question' in title:
        return 'oral'
    # Word-count heuristic: short speeches are likely Oral PQ follow-ups.
    # Applied to both commons and lords — threshold 400 words.
    if speeches:
        max_words = max((r.get('body_word_count', 0) for r in speeches), default=0)
        if source in ('commons', 'lords') and 0 < max_words < 400:
            return 'oral'
    return 'debate'


def _fetch_topic_wqs(topic, start_date, end_date, selected_depts, limit=800):
    """Fetch WQs matching topic from Parliament API. Returns (list_of_dicts, total_count).
    Deliberately omits answeringBodies because this function supports multi-dept selection
    and the research tool needs to apply its own relevance scoring across dept results.
    Note: answeringBodies does work reliably when combined with a date anchor — the earlier
    belief that it causes timeouts unconditionally was incorrect (see CLAUDE.md WQ API
    constraints). The client-side filter here is intentional, not a workaround."""
    import logging
    try:
        params = {'searchTerm': topic, 'take': limit, 'skip': 0}
        if start_date:
            params['tabledWhenFrom'] = start_date
        if end_date:
            params['tabledWhenTo'] = end_date
        resp = requests.get(PARLIAMENT_WQ_API, params=params, timeout=30)
        if resp.status_code != 200:
            logging.warning(f"WQ fetch failed: HTTP {resp.status_code} body={resp.text[:200]}")
            return [], -1  # surface as wq_error so user sees a message
        data = resp.json()
        total = data.get('totalResults', 0)
        api_returned = len(data.get('results', []))
        logging.warning(
            f"[wq_diag] topic={topic!r} api_total={total} api_returned={api_returned} "
            f"dept={list(selected_depts)} stems={[(w[:-1] if w.endswith('s') and len(w) > 4 else w) for w in [x.lower() for x in topic.split() if len(x) > 3]]}"
        )
        # Client-side dept filter — match answeringBodyName against selected dept names
        dept_names = set(selected_depts)
        # Relevance stems: strip trailing 's' so "repayments" matches "repayment",
        # "loans" matches "loan", "students" matches "student".
        # Parliament WQ API does tokenised OR search — client-side filter enforces relevance.
        raw_words = [w.lower() for w in topic.split() if len(w) > 3]
        relevance_stems = [(w[:-1] if w.endswith('s') and len(w) > 4 else w) for w in raw_words]
        results = []
        seen_uins = set()
        for item in data.get('results', []):
            val = item.get('value', {})
            uin = str(val.get('uin', ''))
            if uin and uin in seen_uins:
                continue  # deduplicate — API can return same WQ if search matches both Q and A
            if uin:
                seen_uins.add(uin)
            # Client-side dept filter
            answering_body = val.get('answeringBodyName', '')
            if dept_names and answering_body not in dept_names:
                continue
            # Client-side relevance filter — require 2+ stems to match for multi-word topics.
            # "student" alone hits almost every DfE question; 2-of-3 for "student loan repayment"
            # keeps genuine WQs while removing SEND/environment/dyslexia noise.
            if relevance_stems:
                q_raw = (val.get('questionText') or '').lower()
                heading_raw = (val.get('heading') or '').lower()
                combined = q_raw + ' ' + heading_raw
                match_count = sum(1 for stem in relevance_stems if stem in combined)
                min_matches = 2 if len(relevance_stems) >= 2 else 1
                if match_count < min_matches:
                    continue
            raw_date = (val.get('dateTabled') or '').split('T')[0]
            date_ans = (val.get('dateAnswered') or '').split('T')[0]
            answer_raw = val.get('answerText') or ''
            answer_clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', answer_raw)).strip()
            # Use cleaned answer text (not raw HTML) to determine answered status;
            # raw HTML can contain only tags/whitespace, giving a false positive.
            is_answered = bool(answer_clean or date_ans)
            is_withdrawn = bool(val.get('isWithdrawn'))
            is_holding = (is_answered and not is_withdrawn and
                          'i will write' in answer_clean.lower()[:200])
            q_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', val.get('questionText') or '')).strip()
            asking = val.get('askingMember') or {}
            party = asking.get('party', '')
            membership = asking.get('latestHouseMembership') or {}
            house = 'Lords' if membership.get('house') == 2 else 'Commons'
            constituency = membership.get('membershipFrom', '')
            role = 'Life Peer' if house == 'Lords' else (f'MP for {constituency}' if constituency else 'MP')
            minister = val.get('answeringMember') or {}
            results.append({
                'uin': uin,
                'dept': answering_body,
                'question_text': q_text,
                'answer_text': answer_clean[:1500],
                'answering_minister': minister.get('name', ''),
                'asking_mp': asking.get('name') or asking.get('listAs', ''),
                'role': role,
                'party': party,
                'party_colour': PARTY_COLOURS_RESEARCH.get(party, '#888'),
                'date_tabled': raw_date,
                'date_answered': date_ans,
                'is_answered': is_answered,
                'is_holding': is_holding,
                'is_withdrawn': is_withdrawn,
                'heading': val.get('heading', ''),
                'url': f"https://questions-statements.parliament.uk/written-questions/detail/{raw_date}/{uin}",
            })
        logging.warning(f"[wq_diag] after_filters={len(results)} (from api_returned={api_returned} api_total={total})")
        return results, total
    except Exception as e:
        import logging
        logging.error(f"WQ fetch exception: {type(e).__name__}: {e}")
        return [], -1  # -1 signals fetch error (timeout etc.) vs genuine zero results


def _fetch_govuk_dept_ministers(dept_base_path, dept_name):
    """Fetch ordered_ministers for one GOV.UK department page.
    Returns list of (normalised_name, role_string, display_name, dept_name) tuples."""
    results = []
    try:
        r = requests.get(f'https://www.gov.uk/api/content{dept_base_path}', timeout=8)
        if r.status_code == 200:
            for minister in r.json().get('links', {}).get('ordered_ministers', []):
                title = minister.get('title', '').strip()
                norm = _normalise_name(title)
                display = _display_name(title)
                if norm and display:
                    results.append((norm, f'Minister ({dept_name})', display, dept_name))
    except Exception:
        pass
    return results


def get_minister_list():
    """All current government ministers: {by_norm, by_id, by_dept, member_ids}.

    Read from our gov_minister table (refreshed daily by
    scripts/refresh_ministers.py; see ministers.py). Only while that table is
    empty or missing (before the migration and first run) does this fall back
    to the old file-cached GOV.UK fetch, so deploy order can't break the site."""
    from ministers import load_minister_list
    data = load_minister_list()
    if data:
        return data
    return _get_minister_list_live()


def _get_minister_list_live():
    """Legacy fallback: fetch ALL current government ministers from GOV.UK content API.
    Returns dict with 'by_norm' {normalised_name: role} keyed by _normalise_name().
    File-cached for 30 days. Includes junior ministers, not just Cabinet.
    Supplemented by Parliament GovernmentPosts. Remove once gov_minister is live."""
    cache = {}
    try:
        if os.path.exists(MINISTER_CACHE_FILE):
            with open(MINISTER_CACHE_FILE) as f:
                cache = json.load(f)
        if (cache.get('_ts') and time.time() - cache['_ts'] < MINISTER_CACHE_TTL
                and cache.get('_source') == 'govuk'
                and cache.get('_version') == MINISTER_CACHE_VERSION
                and cache.get('by_norm') and cache.get('by_dept')):
            return cache
    except Exception:
        pass

    by_norm = {}   # normalised_name → role
    by_id = {}     # parliament_member_id → role
    by_dept = {}   # dept_name → [{"name": display_name}]

    try:
        # Step 1: GOV.UK ministers page — Cabinet + dept links
        r = requests.get('https://www.gov.uk/api/content/government/ministers', timeout=10)
        if r.status_code == 200:
            links = r.json().get('links', {})

            # Cabinet ministers (direct list on the page)
            for section in ['ordered_cabinet_ministers', 'ordered_also_attends_cabinet']:
                for m in links.get(section, []):
                    norm = _normalise_name(m.get('title', ''))
                    if norm:
                        by_norm[norm] = 'Cabinet Minister'

            # All departments — fetch in parallel
            depts = links.get('ordered_ministerial_departments', [])
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futures = {
                    ex.submit(_fetch_govuk_dept_ministers, d['base_path'], d.get('title', 'Government')): d
                    for d in depts if d.get('base_path')
                }
                for future in concurrent.futures.as_completed(futures):
                    for norm, role, display, dept in future.result():
                        if norm not in by_norm:
                            by_norm[norm] = role
                        by_dept.setdefault(dept, [])
                        if display and not any(m['name'] == display for m in by_dept[dept]):
                            by_dept[dept].append({'name': display})
            for dept in by_dept:
                by_dept[dept].sort(key=lambda m: m['name'])
    except Exception:
        pass

    # Fallback / supplement: Parliament GovernmentPosts (Cabinet only — populates by_id)
    try:
        r = requests.get(
            'https://members-api.parliament.uk/api/Posts/GovernmentPosts', timeout=10
        )
        if r.status_code == 200:
            for post in r.json():
                v = post.get('value', {})
                role = v.get('name', '')
                for holder in v.get('postHolders', []):
                    if holder.get('endDate'):
                        continue  # skip past holders
                    m = (holder.get('member') or {}).get('value', {})
                    name = m.get('nameDisplayAs', '')
                    member_id = m.get('id')
                    norm = _normalise_name(name)
                    if norm and norm not in by_norm:
                        by_norm[norm] = role
                    if member_id and role:
                        by_id[str(member_id)] = role
    except Exception:
        pass

    # Keep resolved member ids across a minister list refresh — a member's
    # Parliament id never changes. Failed lookups are dropped so they are retried.
    member_ids = {name: entry for name, entry in cache.get('member_ids', {}).items()
                  if entry.get('parliament_id')}
    data = {'_ts': time.time(), '_source': 'govuk', '_version': MINISTER_CACHE_VERSION,
            'by_norm': by_norm, 'by_id': by_id, 'by_dept': by_dept, 'member_ids': member_ids}
    try:
        with open(MINISTER_CACHE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

    return data


def flag_minister_rows(rows):
    """Set is_minister / minister_role on speech rows from the post each speaker
    held on the speech date (ministers.post_on_date: our member_profile history
    plus gov_minister), never their current post projected back.

    A speaker with no dated record in our DB (no member id, or no stored
    Biography) who is on the current minister list is still flagged, so their
    speeches sort with the ministers', but is marked minister_unverified (no
    "Minister" label) unless gov_minister shows their current post had begun
    by the speech date.
    """
    from ministers import post_histories, post_on_date
    by_norm = get_minister_list().get('by_norm', {})
    histories = post_histories({r.get('member_id') for r in rows if r.get('member_id')})
    for row in rows:
        member_id = row.get('member_id')
        role = post_on_date(member_id, row.get('hdate'), row.get('dept') or None,
                            histories=histories) if member_id else None
        if member_id and int(member_id) in histories:
            row['is_minister'] = bool(role)
        else:
            norm_spk = _normalise_name(row.get('speaker_name', ''))
            row['is_minister'] = bool(role or (norm_spk and by_norm.get(norm_spk)))
        row['minister_role'] = role or ''
        row['minister_unverified'] = row['is_minister'] and not role


def verify_government_speaker(name):
    """Confirm a speaker holds a current government post.
    Returns dict with confirmed (bool) and role (str).

    Three-tier approach:
    1. Normalised name match against GOV.UK minister cache (covers all ministers)
    2. Parliament Members Search → check each result ID against Cabinet id cache
    3. Parliament Members Biography → governmentPosts with no endDate (gets precise role)

    When the list comes from our gov_minister table (every current post holder,
    with member ids), tiers 2-3 are replaced by a lookup in our own member
    tables: no API calls on page view, and no process-lifetime cache.
    """
    norm_name = _normalise_name(name)
    minister_data = get_minister_list()
    by_norm = minister_data.get('by_norm', {})
    by_id = minister_data.get('by_id', {})

    if minister_data.get('_source') == 'db':
        if norm_name and norm_name in by_norm:
            return {'confirmed': True, 'role': by_norm[norm_name]}
        member_id, _is_lord = _local_member_lookup(name)
        if member_id and str(member_id) in by_id:
            return {'confirmed': True, 'role': by_id[str(member_id)]}
        return {'confirmed': False, 'role': ''}

    if norm_name and norm_name in _VERIFY_CACHE:
        return _VERIFY_CACHE[norm_name]

    # 1. Normalised match against GOV.UK full minister list
    if norm_name and norm_name in by_norm:
        result = {'confirmed': True, 'role': by_norm[norm_name]}
        if norm_name:
            _VERIFY_CACHE[norm_name] = result
        return result

    # 2. Parliament Members Search → ID → Cabinet id cache
    try:
        s_resp = requests.get(
            'https://members-api.parliament.uk/api/Members/Search',
            params={'Name': name, 'IsCurrentMember': 'true', 'take': 5},
            timeout=5
        )
        if s_resp.status_code == 200:
            items = s_resp.json().get('items', [])
            for item in items:
                member_id = str(item['value']['id'])
                if member_id in by_id:
                    result = {'confirmed': True, 'role': by_id[member_id]}
                    if norm_name:
                        _VERIFY_CACHE[norm_name] = result
                    return result

            # 3. Biography endpoint — has full government post history with endDate
            if items:
                member_id = str(items[0]['value']['id'])
                bio_resp = requests.get(
                    f'https://members-api.parliament.uk/api/Members/{member_id}/Biography',
                    timeout=5
                )
                if bio_resp.status_code == 200:
                    govt_posts = bio_resp.json().get('value', {}).get('governmentPosts', [])
                    current_posts = [p for p in govt_posts if not p.get('endDate')]
                    if current_posts:
                        result = {'confirmed': True, 'role': current_posts[0].get('name', '')}
                        if norm_name:
                            _VERIFY_CACHE[norm_name] = result
                        return result
    except Exception:
        pass

    result = {'confirmed': False, 'role': ''}
    if norm_name:
        _VERIFY_CACHE[norm_name] = result
    return result


# --- Hansard API helpers (Phase 1: minister search) ---

_HANSARD_PARTY_ABBREV = {
    'lab': 'Labour', 'con': 'Conservative', 'ld': 'Liberal Democrat',
    'lib dem': 'Liberal Democrat', 'snp': 'Scottish National Party',
    'green': 'Green Party', 'reform': 'Reform UK', 'pc': 'Plaid Cymru',
    'dup': 'Democratic Unionist Party', 'ind': 'Independent',
    'crossbench': 'Crossbench',
}


def _parse_hansard_party(attributed_to):
    """Extract full party name from AttributedTo string.
    Handles: 'Lord Sikka (Lab)', 'Baroness Barran (Con)',
    'Baroness in Waiting/Government Whip (Baroness Blake of Leeds) (Lab)'
    Party is always the last (...) group."""
    m = re.search(r'\(([^)]+)\)\s*$', attributed_to or '')
    if m:
        abbrev = m.group(1).strip().lower()
        return _HANSARD_PARTY_ABBREV.get(abbrev, m.group(1).strip())
    return ''


def _hansard_section_to_source(house, section):
    """Map Hansard API House + Section fields to our source codes."""
    section_lower = (section or '').lower()
    if 'written statement' in section_lower:
        return 'wms'
    if 'written answer' in section_lower:
        return 'wrans'
    if 'westminster hall' in section_lower:
        return 'westminsterhall'
    if (house or '').lower() == 'lords':
        return 'lords'
    return 'commons'


def fetch_hansard_minister_topic(parliament_id, topic, date_range, sources, num=50, is_lord=False):
    """Fetch minister speeches via Hansard API using Parliament member ID directly.
    Returns the normalised row schema used by the grouping and display code."""
    if not parliament_id:
        return []

    # Parse YYYYMMDD..YYYYMMDD → ISO dates for the Hansard API
    start_date_api = ''
    end_date_api = ''
    if date_range and '..' in date_range:
        parts = date_range.split('..')
        def _fmt(d): return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        start_date_api = _fmt(parts[0].strip()) if parts[0].strip() else ''
        end_date_api = _fmt(parts[1].strip()) if parts[1].strip() else ''

    import logging as _ml
    cache_key_query = f"hansard:minister:{parliament_id}:{topic} {date_range}".strip()
    cached = CachedSearch.get(cache_key_query, 'hansard_minister', ttl_hours=6)
    if cached is not None:
        _ml.warning(f"[minister_search] parliament_id={parliament_id} from_cache=True rows={len(cached)}")
        return cached

    params = {'queryParameters.memberId': parliament_id, 'take': num, 'skip': 0}
    params['queryParameters.house'] = 'Lords' if is_lord else 'Commons'
    if topic:
        # Hansard /search.json rejects boolean OR syntax — extract first quoted phrase
        simple_topic = topic
        if topic.startswith('(') and '"' in topic:
            m = re.search(r'"([^"]+)"', topic)
            if m:
                simple_topic = m.group(1)
        params['queryParameters.searchTerm'] = simple_topic
    if start_date_api:
        params['queryParameters.startDate'] = start_date_api
    if end_date_api:
        params['queryParameters.endDate'] = end_date_api

    try:
        import logging as _ml
        resp = requests.get(f"{HANSARD_API_BASE}/search.json", params=params, timeout=25)
        if resp.status_code != 200:
            _ml.warning(f"[minister_search] parliament_id={parliament_id} status={resp.status_code}")
            return []
        data = resp.json()
        results = data.get('Contributions', [])
        _ml.warning(f"[minister_search] parliament_id={parliament_id} contributions={len(results)} total={data.get('TotalContributions','?')}")

        rows = []
        for r in results:
            house = r.get('House', 'Commons')
            section = r.get('Section', '')
            source = _hansard_section_to_source(house, section)
            if source not in sources:
                continue

            body_raw = r.get('ContributionTextFull', '') or ''
            body_text = clean_body_text(body_raw)
            debate_title = r.get('DebateSection', '') or ''

            sitting_date = r.get('SittingDate', '')
            hdate = sitting_date[:10] if sitting_date else ''

            ext_id = r.get('DebateSectionExtId', '') or ''
            house_path = 'Lords' if house.lower() == 'lords' else 'Commons'
            slug = _make_hansard_slug(debate_title)
            listurl = (f"https://hansard.parliament.uk/{house_path}/{hdate}/debates/{ext_id}/{slug}"
                       if ext_id and hdate else '')

            dtype = get_debate_type(debate_title, source=source)
            rows.append({
                'listurl': listurl,
                'body_clean': body_text[:500],
                'body_export': body_text[:3000],
                'body_word_count': len(body_text.split()),
                'speaker_name': r.get('MemberName', '') or '',
                'speaker_party': _parse_hansard_party(r.get('AttributedTo', '')),
                'hdate': hdate,
                'debate_title': debate_title,
                'source': source,
                'source_label': get_source_label(source),
                'relevance': r.get('Rank', 0),
                'debate_type': dtype,
                'debate_section_ext_id': ext_id,
            })

        with_ext = sum(1 for r in rows if r.get('debate_section_ext_id'))
        _ml.warning(f"[minister_search] parliament_id={parliament_id} rows={len(rows)} with_ext_id={with_ext} sources={set(r['source'] for r in rows)}")
        if rows:  # never cache empty results — empty means API miss, not "no speeches"
            CachedSearch.store(cache_key_query, 'hansard_minister', rows)
        return rows
    except Exception as e:
        import logging
        logging.warning(f"[minister_search] parliament_id={parliament_id} exception={type(e).__name__}: {e}")
        return []


_wms_member_cache: dict = {}  # memberId → (name, party_abbrev)


def _lookup_wms_member(member_id):
    """Return (name, party_abbrev) for a Parliament member ID. In-process cache."""
    if member_id in _wms_member_cache:
        return _wms_member_cache[member_id]
    result = ('', '')
    try:
        resp = requests.get(
            f"https://members-api.parliament.uk/api/Members/{member_id}",
            timeout=8
        )
        if resp.status_code == 200:
            v = resp.json().get('value', {})
            name = v.get('nameDisplayAs', '')
            party = (v.get('latestParty') or {}).get('abbreviation', '') or \
                    (v.get('latestParty') or {}).get('name', '')
            result = (name, party)
    except Exception:
        pass
    _wms_member_cache[member_id] = result
    return result


def _wms_date_range(date_range):
    """'YYYYMMDD..YYYYMMDD' (may have a query prefix) -> ('YYYY-MM-DD', 'YYYY-MM-DD'); '' when absent."""
    from_date = to_date = ''
    if date_range and '..' in date_range:
        raw = date_range.split('..')
        raw_from = re.sub(r'^\D+', '', raw[0].strip())
        raw_to   = re.sub(r'^\D+', '', raw[1].strip())
        if len(raw_from) == 8:
            from_date = f"{raw_from[:4]}-{raw_from[4:6]}-{raw_from[6:]}"
        if len(raw_to) == 8:
            to_date = f"{raw_to[:4]}-{raw_to[4:6]}-{raw_to[6:]}"
    return from_date, to_date


def _wms_query_stems(query):
    """Keyword stems for the WMS relevance post-filter (words over 3 letters, plural s dropped)."""
    stems = set()
    for w in query.lower().split():
        if len(w) > 3:
            stems.add(w[:-1] if w.endswith('s') and len(w) > 4 else w)
    return stems


def _wms_stems_match(stems, text):
    """At least half of the query stems must appear in the statement title + text."""
    if not stems:
        return True
    combined = text.lower()
    hits = sum(1 for stem in stems if stem in combined)
    return hits >= max(1, len(stems) // 2)


def fetch_db_wms(query, date_range, num=20):
    """Written ministerial statements from our own table (ha_written_statement),
    same row shape and relevance rule as fetch_parliament_wms(). Returns None when
    the table is missing or empty, so callers can fall back to the live API."""
    import written_statements as _ws
    from datetime import date as _date

    if not _ws.has_statements():
        return None
    from_date, to_date = _wms_date_range(date_range)
    try:
        start = _date.fromisoformat(from_date) if from_date else None
        end = _date.fromisoformat(to_date) if to_date else None
    except ValueError:
        start = end = None
    stems = _wms_query_stems(query)
    link_to_site = _ws.written_statements_enabled()
    matches = [ws for ws in _ws.candidates_for_words(sorted(stems), start, end)
               if _wms_stems_match(stems, (ws.title or '') + ' ' + (ws.text_plain or ''))]
    matches.sort(key=lambda ws: (ws.date_made, ws.id), reverse=True)
    rows = []
    for ws in matches[:num]:
        row = _ws.prep_row(ws, link_to_site)
        row['speaker_party'] = _normalise_party(row['speaker_party'])
        rows.append(row)
    return rows


def fetch_parliament_wms(query, date_range, dept_id=None, num=80):
    """Fetch Written Ministerial Statements from Parliament Q&S API.
    No API key required. Returns normalised row schema.

    Keyword matching: Parliament API does OR-word matching, not phrase matching.
    When dept_id is provided, returns all WMS for that dept in the date range
    and post-filters by keyword. Without dept_id, uses the first significant word
    as API filter then post-filters client-side.
    """
    import logging as _wms_log

    from_date, to_date = _wms_date_range(date_range)

    cache_key = f"parl_wms:{query}:{date_range}:{dept_id}"
    cached = CachedSearch.get(cache_key, 'wms', ttl_hours=6)
    if cached is not None:
        return cached

    params = {'take': num}
    if from_date:
        params['madeWhenFrom'] = from_date
    if to_date:
        params['madeWhenTo'] = to_date
    if dept_id:
        params['answeringBodies'] = dept_id
    else:
        # Single-word API filter to narrow the result set before post-filtering
        words = [w for w in query.lower().split() if len(w) > 3]
        if words:
            params['searchTerm'] = words[0]

    try:
        resp = requests.get(PARLIAMENT_WMS_API, params=params, timeout=15)
        if resp.status_code != 200:
            _wms_log.warning(f"[parl_wms] HTTP {resp.status_code} params={params}")
            return [{'_error': f"Parliament WMS API HTTP {resp.status_code}"}]
        raw_results = resp.json().get('results', [])
    except Exception as e:
        _wms_log.warning(f"[parl_wms] exception: {e}")
        return []

    query_stems = _wms_query_stems(query)

    # Batch member lookups (one per unique ID)
    unique_ids = {r['value']['memberId'] for r in raw_results
                  if r.get('value', {}).get('memberId')}
    for mid in unique_ids:
        _lookup_wms_member(mid)

    results = []
    for r in raw_results:
        v = r.get('value', {})
        title = v.get('title', '')
        text  = v.get('text', '')

        if not _wms_stems_match(query_stems, title + ' ' + text):
            continue

        member_id = v.get('memberId')
        speaker_name, speaker_party = _lookup_wms_member(member_id) if member_id else ('', '')

        raw_date = v.get('dateMade', '')
        hdate = raw_date[:10] if raw_date else ''
        house = v.get('house', 'Commons')
        uin   = v.get('uin', '')
        house_path = 'Lords' if house.lower() == 'lords' else 'Commons'
        listurl = (f"https://hansard.parliament.uk/{house_path}/{hdate}/writtenstatements/{uin}"
                   if hdate and uin else '')

        results.append({
            'body_clean':      text,
            'body_export':     text[:2000],
            'body_word_count': len(text.split()),
            'speaker_name':    speaker_name,
            'speaker_party':   _normalise_party(speaker_party),
            'hdate':           hdate,
            'debate_title':    title,
            'source':          'wms',
            'source_label':    'Ministerial Statement',
            'relevance':       0,
            'debate_type':     '📜 Ministerial Statement',
            'listurl':         listurl,
            'dept':            v.get('answeringBodyName', ''),
            'is_minister':     True,
            'debate_section_ext_id': '',
            'member_role':     v.get('memberRole', ''),
        })

    _wms_log.warning(
        f"[parl_wms] query={query!r} dept_id={dept_id} raw={len(raw_results)} kept={len(results)}"
    )
    CachedSearch.store(cache_key, 'wms', results)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL DB FETCH LAYER — Research Tool searches run against our own Hansard archive
# ─────────────────────────────────────────────────────────────────────────────

_DB_SOURCE_FILTER = {
    'commons':         "s.house = 'Commons' AND s.debate_type NOT IN ('westminster_hall','ministerial_statement')",
    'lords':           "s.house = 'Lords' AND s.debate_type != 'ministerial_statement'",
    'westminsterhall': "s.debate_type = 'westminster_hall'",
    'wms':             "s.debate_type = 'ministerial_statement'",
    # Lords statements (mostly repeats of Commons statements) are classified as
    # ministerial_statement too, so a one-House search needs a house-filtered bucket.
    'wms_commons':     "s.house = 'Commons' AND s.debate_type = 'ministerial_statement'",
    'wms_lords':       "s.house = 'Lords' AND s.debate_type = 'ministerial_statement'",
}


def research_sources_for_house(house_filter):
    """Local-DB source buckets for the Research Tool's house filter."""
    if house_filter == 'lords_only':
        return ['lords', 'wms_lords']
    if house_filter == 'commons_only':
        return ['commons', 'westminsterhall', 'wms_commons']
    return ['commons', 'westminsterhall', 'lords', 'wms']


def minister_sources(sources, is_lord):
    """The buckets a minister can appear in: Lords ministers speak only in the Lords."""
    lords_buckets = ('lords', 'wms_lords')
    if is_lord:
        return [s for s in sources if s in lords_buckets or s == 'wms']
    return [s for s in sources if s not in lords_buckets]


def _session_to_source(house, debate_type):
    dt = (debate_type or '').lower()
    if dt == 'westminster_hall':
        return 'westminsterhall'
    if dt == 'ministerial_statement':
        return 'wms'
    return 'lords' if (house or '').lower() == 'lords' else 'commons'


def _db_row(s_date, s_title, s_house, s_debate_type, s_hansard_url, s_ext_id,
            s_dept, c_member_name, c_party, c_speech_text, c_member_id, relevance):
    source = _session_to_source(s_house, s_debate_type)
    hdate = s_date.isoformat() if hasattr(s_date, 'isoformat') else str(s_date or '')[:10]
    text_clean = (c_speech_text or '').strip()
    return {
        'listurl':               s_hansard_url or '',
        'body_clean':            text_clean[:500],
        'body_export':           text_clean[:3000],
        'body_word_count':       len(text_clean.split()),
        'speaker_name':          c_member_name or '',
        'speaker_party':         _normalise_party(c_party or ''),
        'hdate':                 hdate,
        'debate_title':          s_title or '',
        'source':                source,
        'source_label':          get_source_label(source),
        'relevance':             float(relevance or 0),
        'debate_type':           get_debate_type(s_title or '', source=source),
        'debate_section_ext_id': s_ext_id or '',
        'member_id':             c_member_id,
        'dept':                  s_dept or '',
    }


def _db_is_postgres():
    from extensions import db as _db
    return _db.engine.url.drivername.startswith('postgresql')


def _topic_words(topic):
    """The topic's own words for websearch_to_tsquery: no quotes or operators."""
    words = re.findall(r"[^\W_][\w'’-]*", topic or '')
    return ' '.join(w for w in words if w.lower() not in ('or', 'and', 'not'))


def build_topic_query(expanded, topic, narrow_keyword='', add_topic_words=True):
    """The Research Tool's search string: the expanded phrases OR the topic's words.

    The AI expansion gives exact phrases, and when it fails the fallback is the
    whole topic as one exact phrase ("UK support for Ukraine" is rarely said
    word for word, so that found nothing). The extra branch also finds speeches
    that use every word of the topic in any order, like the archive search.
    A narrowing keyword applies to every branch (websearch_to_tsquery binds AND
    tighter than OR, so appending it once only narrowed the last phrase).
    add_topic_words=False when the user edited the terms in Preview: use theirs as given.
    """
    inner = (expanded or '').strip()
    if inner.startswith('(') and inner.endswith(')'):
        inner = inner[1:-1].strip()
    branches = [b.strip() for b in re.split(r'\s+OR\s+', inner, flags=re.I) if b.strip()]
    words = _topic_words(topic)
    one_word_phrases = {b.strip('"').lower() for b in branches if ' ' not in b.strip('"')}
    if add_topic_words and words and words.lower() not in one_word_phrases:
        branches.append(words)
    kw = (narrow_keyword or '').replace('"', ' ').strip()
    if kw:
        branches = [f'{b} "{kw}"' for b in branches]
    if not branches:
        return ''
    return branches[0] if len(branches) == 1 else '(' + ' OR '.join(branches) + ')'


def _wsq_string(fts_query):
    """Prepare fts_query for websearch_to_tsquery: strip outer parens, lowercase OR."""
    s = fts_query.strip()
    if s.startswith('(') and s.endswith(')'):
        s = s[1:-1].strip()
    return re.sub(r'\bOR\b', 'or', s)


def fetch_from_db_topic(fts_query, source_type, date_from='', date_to='', limit=25):
    """Return all speech rows from sessions matching fts_query for source_type.
    Replaces fetch_hansard_topic() — includes full session expansion in one DB query.
    On Postgres: uses speech_tsv/title_tsv GIN indexes.
    On SQLite (dev): ILIKE fallback on title.
    """
    import logging as _dbl
    from extensions import db as _db
    from sqlalchemy import text as sqla_text

    if not fts_query:
        return []
    src_filter = _DB_SOURCE_FILTER.get(source_type)
    if not src_filter:
        return []

    is_pg = _db_is_postgres()
    try:
        date_clauses = []
        params = {'lim': limit}
        if date_from:
            date_clauses.append('s.date >= :date_from')
            params['date_from'] = date_from
        if date_to:
            date_clauses.append('s.date <= :date_to')
            params['date_to'] = date_to
        where_dates = ('AND ' + ' AND '.join(date_clauses)) if date_clauses else ''

        if is_pg:
            wsq = _wsq_string(fts_query)
            params['q'] = wsq
            sql_sessions = sqla_text(f"""
                SELECT matched.session_id FROM (
                    SELECT c.session_id,
                           MAX(ts_rank(c.speech_tsv, websearch_to_tsquery('english', :q))) AS r
                    FROM ha_contribution c
                    JOIN ha_session s ON s.id = c.session_id
                    WHERE s.is_container = false AND {src_filter}
                      {where_dates}
                      AND c.speech_tsv @@ websearch_to_tsquery('english', :q)
                    GROUP BY c.session_id
                    UNION
                    SELECT s.id AS session_id,
                           ts_rank(s.title_tsv, websearch_to_tsquery('english', :q)) AS r
                    FROM ha_session s
                    WHERE s.is_container = false AND {src_filter}
                      {where_dates}
                      AND s.title_tsv @@ websearch_to_tsquery('english', :q)
                ) matched
                ORDER BY matched.r DESC
                LIMIT :lim
            """)
            sid_rows = _db.session.execute(sql_sessions, params).fetchall()
            session_ids = [r[0] for r in sid_rows]
            if not session_ids:
                _dbl.warning(f"[db_topic] {source_type} q={repr(fts_query)[:60]} 0 sessions")
                return []
            sql_contribs = sqla_text("""
                SELECT c.session_id, c.member_id, c.member_name, c.party,
                       c.speech_text, c.speech_order,
                       s.title, s.date, s.house, s.debate_type,
                       s.hansard_url, s.ext_id, s.department,
                       COALESCE(ts_rank(c.speech_tsv, websearch_to_tsquery('english', :q)), 0) AS relevance
                FROM ha_contribution c
                JOIN ha_session s ON s.id = c.session_id
                WHERE c.session_id = ANY(:ids)
                ORDER BY s.date DESC, c.session_id, c.speech_order
            """)
            contrib_rows = _db.session.execute(
                sql_contribs, {'ids': session_ids, 'q': wsq}
            ).fetchall()
        else:
            # SQLite dev fallback — ILIKE on title
            m_q = re.search(r'"([^"]+)"', fts_query)
            keyword = m_q.group(1) if m_q else fts_query.strip('()"')
            params['kw'] = f'%{keyword}%'
            sql_sessions = sqla_text(f"""
                SELECT DISTINCT s.id AS session_id
                FROM ha_session s
                LEFT JOIN ha_contribution c ON c.session_id = s.id
                WHERE s.is_container = 0 AND {src_filter}
                  {where_dates}
                  AND (s.title LIKE :kw OR c.speech_text LIKE :kw)
                LIMIT :lim
            """)
            sid_rows = _db.session.execute(sql_sessions, params).fetchall()
            session_ids = [r[0] for r in sid_rows]
            if not session_ids:
                return []
            placeholders = ','.join(f':id{i}' for i in range(len(session_ids)))
            id_params = {f'id{i}': sid for i, sid in enumerate(session_ids)}
            sql_contribs = sqla_text(f"""
                SELECT c.session_id, c.member_id, c.member_name, c.party,
                       c.speech_text, c.speech_order,
                       s.title, s.date, s.house, s.debate_type,
                       s.hansard_url, s.ext_id, s.department,
                       0.0 AS relevance
                FROM ha_contribution c
                JOIN ha_session s ON s.id = c.session_id
                WHERE c.session_id IN ({placeholders})
                ORDER BY s.date DESC, c.session_id, c.speech_order
            """)
            contrib_rows = _db.session.execute(sql_contribs, id_params).fetchall()

        result = [
            _db_row(r.date, r.title, r.house, r.debate_type, r.hansard_url,
                    r.ext_id, r.department, r.member_name, r.party,
                    r.speech_text, r.member_id, r.relevance)
            for r in contrib_rows
        ]
        _dbl.warning(f"[db_topic] {source_type} sessions={len(session_ids)} rows={len(result)}")
        return result
    except Exception as e:
        import logging
        logging.warning(f"[db_topic] {source_type} exception: {type(e).__name__}: {e}")
        return [{'_error': f"DB topic fetch failed: {e}"}]


def fetch_from_db_minister(member_id, source_types, date_from='', date_to='', limit=25):
    """Return all speech rows from sessions where this Parliament member spoke.
    Replaces fetch_hansard_minister_topic() — includes full session expansion.
    member_id: Parliament member ID (= ha_contribution.member_id).
    """
    import logging as _dbl
    from extensions import db as _db
    from sqlalchemy import text as sqla_text

    if not member_id:
        return []
    src_filters = [_DB_SOURCE_FILTER[s] for s in source_types if s in _DB_SOURCE_FILTER]
    if not src_filters:
        return []
    src_clause = '(' + ' OR '.join(f'({f})' for f in src_filters) + ')'
    container_check = 's.is_container = false' if _db_is_postgres() else 's.is_container = 0'

    date_clauses = []
    params = {'mid': member_id, 'lim': limit}
    if date_from:
        date_clauses.append('s.date >= :date_from')
        params['date_from'] = date_from
    if date_to:
        date_clauses.append('s.date <= :date_to')
        params['date_to'] = date_to
    where_dates = ('AND ' + ' AND '.join(date_clauses)) if date_clauses else ''

    is_pg = _db_is_postgres()
    try:
        sql_sessions = sqla_text(f"""
            SELECT DISTINCT c.session_id
            FROM ha_contribution c
            JOIN ha_session s ON s.id = c.session_id
            WHERE {container_check} AND {src_clause}
              {where_dates}
              AND c.member_id = :mid
            ORDER BY c.session_id DESC
            LIMIT :lim
        """)
        sid_rows = _db.session.execute(sql_sessions, params).fetchall()
        session_ids = [r[0] for r in sid_rows]
        if not session_ids:
            _dbl.warning(f"[db_minister] member_id={member_id} sources={source_types} 0 sessions")
            return []

        if is_pg:
            sql_contribs = sqla_text("""
                SELECT c.session_id, c.member_id, c.member_name, c.party,
                       c.speech_text, c.speech_order,
                       s.title, s.date, s.house, s.debate_type,
                       s.hansard_url, s.ext_id, s.department,
                       0.5 AS relevance
                FROM ha_contribution c
                JOIN ha_session s ON s.id = c.session_id
                WHERE c.session_id = ANY(:ids)
                ORDER BY s.date DESC, c.session_id, c.speech_order
            """)
            contrib_rows = _db.session.execute(sql_contribs, {'ids': session_ids}).fetchall()
        else:
            placeholders = ','.join(f':id{i}' for i in range(len(session_ids)))
            id_params = {f'id{i}': sid for i, sid in enumerate(session_ids)}
            sql_contribs = sqla_text(f"""
                SELECT c.session_id, c.member_id, c.member_name, c.party,
                       c.speech_text, c.speech_order,
                       s.title, s.date, s.house, s.debate_type,
                       s.hansard_url, s.ext_id, s.department,
                       0.5 AS relevance
                FROM ha_contribution c
                JOIN ha_session s ON s.id = c.session_id
                WHERE c.session_id IN ({placeholders})
                ORDER BY s.date DESC, c.session_id, c.speech_order
            """)
            contrib_rows = _db.session.execute(sql_contribs, id_params).fetchall()

        result = [
            _db_row(r.date, r.title, r.house, r.debate_type, r.hansard_url,
                    r.ext_id, r.department, r.member_name, r.party,
                    r.speech_text, r.member_id, r.relevance)
            for r in contrib_rows
        ]
        _dbl.warning(f"[db_minister] member_id={member_id} sources={source_types} sessions={len(session_ids)} rows={len(result)}")
        return result
    except Exception as e:
        import logging
        logging.warning(f"[db_minister] member_id={member_id} exception: {type(e).__name__}: {e}")
        return []


def fetch_wqs_from_db(topic, dept_names, date_from='', date_to='', limit=300):
    """Fetch WQs from ha_pq matching topic. Returns (rows, total).
    Replaces _fetch_topic_wqs() — queries local DB instead of Parliament API.
    Uses question_tsv FTS on Postgres; ILIKE fallback on SQLite.
    """
    import logging as _wql
    from extensions import db as _db
    from hansard_archive.models import HaPQ
    from sqlalchemy import text as sqla_text, or_
    from datetime import date as _date_type

    try:
        is_pg = _db_is_postgres()
        q = HaPQ.query

        if date_from:
            try:
                q = q.filter(HaPQ.tabled_date >= _date_type.fromisoformat(date_from))
            except ValueError:
                pass
        if date_to:
            try:
                q = q.filter(HaPQ.tabled_date <= _date_type.fromisoformat(date_to))
            except ValueError:
                pass

        dept_set = set(dept_names or [])
        if dept_set:
            q = q.filter(HaPQ.answering_body.in_(list(dept_set)))

        if topic:
            if is_pg:
                # Every word of the topic, and any "quoted phrase" in it, as the
                # archive search does. websearch_to_tsquery never raises on input.
                q = q.filter(sqla_text("question_tsv @@ websearch_to_tsquery('english', :kw)")
                             .bindparams(kw=topic))
            else:
                raw_words = [w.lower() for w in topic.split() if len(w) > 3]
                kw = raw_words[0] if raw_words else topic
                q = q.filter(or_(
                    HaPQ.heading.ilike(f'%{kw}%'),
                    HaPQ.question_text.ilike(f'%{kw}%'),
                ))

        q = q.order_by(HaPQ.tabled_date.desc())
        total = q.count()
        pq_rows = q.limit(limit).all()

        # SQLite only: the ILIKE above matches one word, so check the others here.
        # Postgres full-text search already needs every word (properly stemmed);
        # this cruder substring check dropped real matches there ("microchipping"
        # is not in "microchip", and short words such as "cat" were ignored).
        raw_words = [w.lower() for w in topic.split() if len(w) > 3]
        relevance_stems = ([] if is_pg else
                           [(w[:-1] if w.endswith('s') and len(w) > 4 else w) for w in raw_words])

        results = []
        seen_ids = set()   # by row id: UINs repeat across sessions (INC-008)
        for pq in pq_rows:
            uin = pq.uin or ''
            if pq.id in seen_ids:
                continue
            seen_ids.add(pq.id)
            # Client-side relevance filter — only when no dept filter already narrows results
            if relevance_stems and not dept_set:
                combined = ((pq.question_text or '') + ' ' + (pq.heading or '')).lower()
                match_count = sum(1 for stem in relevance_stems if stem in combined)
                min_matches = 2 if len(relevance_stems) >= 2 else 1
                if match_count < min_matches:
                    continue
            raw_date = pq.tabled_date.isoformat() if pq.tabled_date else ''
            date_ans = pq.answer_date.isoformat() if pq.answer_date else ''
            house = pq.chamber or 'Commons'
            role = 'Life Peer' if house == 'Lords' else 'MP'
            results.append({
                'uin':                uin,
                'dept':               pq.answering_body or '',
                'question_text':      pq.question_text or '',
                'answer_text':        (pq.answer_text or '')[:1500],
                'answering_minister': pq.answering_member or '',
                'asking_mp':          pq.asking_member or '',
                'role':               role,
                'party':              '',
                'party_colour':       '#888',
                'date_tabled':        raw_date,
                'date_answered':      date_ans,
                'is_answered':        bool(pq.is_answered),
                'is_holding':         bool(pq.is_holding),
                'is_withdrawn':       bool(pq.is_withdrawn),
                'heading':            pq.heading or '',
                'url': (f"https://questions-statements.parliament.uk/written-questions"
                        f"/detail/{raw_date}/{uin}"),
            })

        _wql.warning(f"[db_wqs] topic={topic!r} dept={list(dept_set)} total={total} kept={len(results)}")
        return results, total
    except Exception as e:
        import logging
        logging.error(f"[db_wqs] exception: {type(e).__name__}: {e}")
        return [], -1


def _resolve_parliament_id(display_name):
    """Look up the Parliament member ID for a minister by name.
    Returns (parliament_id, house) or (None, None)."""
    try:
        resp = requests.get(
            'https://members-api.parliament.uk/api/Members/Search',
            params={'Name': display_name, 'IsCurrentMember': 'true', 'take': 3},
            timeout=5
        )
        if resp.status_code == 200:
            items = resp.json().get('items', [])
            if items:
                v = items[0]['value']
                house_num = (v.get('latestHouseMembership') or {}).get('house', 1)
                house = 'Lords' if house_num == 2 else 'Commons'
                return v['id'], house
    except Exception:
        pass
    return None, None


def _unique_member_match(query, name_col, name):
    """The single row of `query` whose name is `name`, else the single row whose
    normalised name matches. None when there is no match or more than one."""
    exact = query.filter(name_col == name).limit(2).all()
    if exact:
        return exact[0] if len(exact) == 1 else None
    target = _normalise_name(name)
    if not target:
        return None
    matches = [r for r in query.all() if _normalise_name(r.name or '') == target]
    return matches[0] if len(matches) == 1 else None


def _local_member_lookup(display_name):
    """Resolve a minister's display name to (member_id, is_lord) from our own
    member tables: member_profile (current members) first, then cached_member
    (which also holds former members). A match is only accepted when it is
    unique. No API calls. Returns (None, False) if not found."""
    import logging
    from cache_models import CachedMember, MemberProfile
    from extensions import db
    name = (display_name or '').strip()
    if not name:
        return None, False
    sources = [
        (MemberProfile.query.filter_by(is_current=True).with_entities(
            MemberProfile.member_id, MemberProfile.name, MemberProfile.house),
         MemberProfile.name),
        (CachedMember.query.with_entities(
            CachedMember.member_id, CachedMember.name, CachedMember.house),
         CachedMember.name),
    ]
    for query, name_col in sources:
        try:
            row = _unique_member_match(query, name_col, name)
        except Exception as e:
            db.session.rollback()
            logging.warning(f"Minister member lookup failed for {name!r}: {e}")
            continue
        if row:
            return row.member_id, row.house == 'Lords'
    return None, False


def get_dept_minister_member_ids(dept_name, minister_data):
    """Resolve all ministers for a department to Parliament member ids (MNIS ids,
    = ha_contribution.member_id). Returns list of {parliament_id, name, role, is_lord}.

    Lookup chain: the id stored in gov_minister -> verified overrides ->
    minister cache file -> our own member tables -> Parliament Members API
    search (last resort, result cached). The last step and the cache file are
    only used by the legacy fallback list, never when it comes from gov_minister.
    """
    import logging
    ministers = minister_data.get('by_dept', {}).get(dept_name, [])
    from_db = minister_data.get('_source') == 'db'
    ids_cache = minister_data.setdefault('member_ids', {})
    results = []
    json_updated = False

    for m in ministers:
        display = m.get('display_name') or m.get('name', '')
        if not display:
            continue
        entry = None
        if m.get('parliament_id'):
            entry = {'parliament_id': m['parliament_id'], 'is_lord': bool(m.get('is_lord'))}
        entry = entry or KNOWN_MINISTER_MEMBER_IDS.get(display) or ids_cache.get(display)
        if entry is None and from_db:
            member_id, is_lord = _local_member_lookup(display)
            entry = {'parliament_id': member_id, 'is_lord': bool(is_lord)}
        elif entry is None:
            member_id, is_lord = _local_member_lookup(display)
            if not member_id:
                member_id, house = _resolve_parliament_id(display)
                is_lord = house == 'Lords'
            entry = {'parliament_id': member_id, 'is_lord': bool(is_lord),
                     'lookup_failed': member_id is None}
            ids_cache[display] = entry
            json_updated = True
        if entry.get('parliament_id'):
            results.append({
                'parliament_id': entry['parliament_id'],
                'name': display, 'role': m.get('role', ''),
                'is_lord': entry.get('is_lord', False),
            })
        else:
            logging.warning(f"Minister member id not found: {display!r}")

    if json_updated:
        try:
            with open(MINISTER_CACHE_FILE, 'w') as f:
                json.dump(minister_data, f)
        except Exception:
            pass
    return results


def find_hansard_mentions(keyword, since, limit=15):
    """Hansard sessions on or after `since` (a date) with a speech mentioning
    `keyword`, newest first, from our own archive. Used by the My Alerts scan.
    Returns [{title, url, snippet, speaker, date}] with internal archive URLs.
    Postgres uses the speech_tsv full-text index; other databases use LIKE."""
    import calendar
    from extensions import db
    from sqlalchemy import text as sqla_text

    keyword = (keyword or '').strip()
    if not keyword:
        return []
    if _db_is_postgres():
        match = "c.speech_tsv @@ websearch_to_tsquery('english', :q)"
        q = keyword
        not_container = 's.is_container = false'
    else:
        match = 'LOWER(c.speech_text) LIKE :q'
        q = f'%{keyword.lower()}%'
        not_container = 's.is_container = 0'
    # One row per session (its first matching speech), so a long debate with
    # many matching speeches cannot crowd other sessions out of the limit.
    sql = sqla_text(f"""
        WITH hits AS (
            SELECT c.session_id, MIN(c.speech_order) AS first_order
            FROM ha_contribution c
            JOIN ha_session s ON s.id = c.session_id
            WHERE {not_container} AND s.slug IS NOT NULL AND s.date >= :since AND {match}
            GROUP BY c.session_id
        )
        SELECT s.id, s.title, s.date, s.slug, c.member_name, c.speech_text
        FROM hits h
        JOIN ha_session s ON s.id = h.session_id
        JOIN ha_contribution c ON c.session_id = h.session_id
                              AND c.speech_order = h.first_order AND {match}
        ORDER BY s.date DESC, s.id, c.id
        LIMIT :lim
    """)
    try:
        rows = db.session.execute(sql, {'q': q, 'since': since, 'lim': limit * 2}).fetchall()
    except Exception as e:
        db.session.rollback()
        import logging
        logging.warning(f"find_hansard_mentions failed for {keyword!r}: {e}")
        return []

    results, seen = [], set()
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        d = r.date if hasattr(r.date, 'month') else datetime.strptime(str(r.date)[:10], '%Y-%m-%d').date()
        url_date = f"{d.day}-{calendar.month_name[d.month].lower()}-{d.year}"
        results.append({
            'title': r.title or 'Parliamentary debate',
            'url': f"/archive/debate/{url_date}/{r.slug}",
            'snippet': _keyword_snippet(r.speech_text, keyword),
            'speaker': r.member_name or '',
            'date': d.isoformat(),
        })
        if len(results) >= limit:
            break
    return results


def _keyword_snippet(text, keyword, width=300):
    """About `width` characters of `text` around the first mention of `keyword`
    (or of its longest word, since full-text search also matches word forms),
    else the start of the text."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    low = text.lower()
    needles = [keyword.lower().strip(' "')]
    needles += sorted(re.findall(r'\w{3,}', keyword.lower()), key=len, reverse=True)
    for needle in needles:
        pos = low.find(needle) if needle else -1
        if pos > width // 3:
            space = text.find(' ', pos - width // 3, pos)
            start = space + 1 if space >= 0 else pos - width // 3
            return '…' + text[start:start + width]
        if pos >= 0:
            break
    return text[:width]


def _debate_group_key(title: str) -> str:
    """Extract a grouping key from a debate title by stripping chamber/motion suffixes.
    'Children's Wellbeing and Schools Bill - Commons Reason and Amendments: Motion A'
    → 'Children's Wellbeing and Schools Bill'
    Single sessions pass through unchanged."""
    m = re.match(r'^(.+?)\s+[-—]\s+(?:Commons|Lords)\b', title)
    if m:
        return m.group(1).strip()
    m = re.match(r'^(.+?)\s+[-—]\s+(?:Second|Third|Report|Committee)\s+Reading', title, re.I)
    if m:
        return m.group(1).strip()
    return title


# ==========================================
# LOCAL DB minister lookup (Debate Contributions tab)
# Keyed on member_id throughout. The only name→id step is resolving the
# controlled dropdown selection against our own member cache, so recently
# created peers are found by member_id like everyone else.
# ==========================================

def _resolve_member_id(minister_name):
    """Resolve a controlled dropdown minister name to a local cached_member
    member_id. Exact match first, then a normalised match across the cache.
    Returns (member_id, matched_name, is_lord) or (None, None, False). No API."""
    from cache_models import CachedMember
    name = (minister_name or '').strip()
    if not name:
        return None, None, False
    m = CachedMember.query.filter_by(name=name).first()
    if not m:
        target = _normalise_name(name)
        if target:
            for cm in CachedMember.query.all():
                if _normalise_name(cm.name or '') == target:
                    m = cm
                    break
    if not m:
        return None, None, False
    return m.member_id, m.name, (m.house == 'Lords')


def fetch_minister_debates_local(member_id, topic, start_date, end_date, house_filter='all'):
    """Local replacement for fetch_minister_debates. Finds every Hansard session
    the member spoke in (by member_id), optionally filtered by topic / date /
    house. Returns dicts matching the template contract exactly:
    {id, title, date, url, type, contributions, group_key}. url is the INTERNAL
    archive link (keeps users on WB, per the source-link policy)."""
    import calendar
    from datetime import datetime as _dt
    from sqlalchemy import func
    from extensions import db
    from hansard_archive.models import (
        HansardSession, HansardContribution, HansardSessionTheme,
    )

    houses = None
    if house_filter == 'commons':
        houses = ['Commons']        # Westminster Hall sits under house='Commons'
    elif house_filter == 'lords':
        houses = ['Lords']

    def _parse(d):
        try:
            return _dt.strptime(d, '%Y-%m-%d').date() if d else None
        except ValueError:
            return None
    sd, ed = _parse(start_date), _parse(end_date)

    q = (db.session.query(
            HansardSession.id, HansardSession.title, HansardSession.date,
            HansardSession.debate_type, HansardSession.slug,
            func.count(HansardContribution.id).label('contribs'))
         .join(HansardContribution, HansardContribution.session_id == HansardSession.id)
         .filter(HansardContribution.member_id == member_id,
                 HansardSession.is_container.is_(False)))
    if houses:
        q = q.filter(HansardSession.house.in_(houses))
    if sd:
        q = q.filter(HansardSession.date >= sd)
    if ed:
        q = q.filter(HansardSession.date <= ed)
    q = q.group_by(HansardSession.id, HansardSession.title, HansardSession.date,
                   HansardSession.debate_type, HansardSession.slug)
    rows = q.all()

    # Topic filter (optional) — debate-level (title / theme tag / any speech in the
    # session), NOT just the minister's own words. Consistent with the Research
    # Tool principle that ministers rarely use the search keywords themselves.
    if topic:
        topic_l = topic.lower()
        ids = [r.id for r in rows]
        keep = {r.id for r in rows if topic_l in (r.title or '').lower()}
        if ids:
            for (sid,) in (db.session.query(HansardSessionTheme.session_id)
                           .filter(HansardSessionTheme.session_id.in_(ids),
                                   func.lower(HansardSessionTheme.theme).like(f"%{topic_l}%"))
                           .distinct().all()):
                keep.add(sid)
            for (sid,) in (db.session.query(HansardContribution.session_id)
                           .filter(HansardContribution.session_id.in_(ids),
                                   func.lower(HansardContribution.speech_text).like(f"%{topic_l}%"))
                           .distinct().all()):
                keep.add(sid)
        rows = [r for r in rows if r.id in keep]

    result = []
    for r in rows:
        title = r.title or ""
        url = ""
        if r.slug:
            url_date = f"{r.date.day}-{calendar.month_name[r.date.month].lower()}-{r.date.year}"
            url = f"/archive/debate/{url_date}/{r.slug}"
        result.append({
            'id': r.id,
            'title': title,
            'date': r.date.isoformat(),
            'url': url,
            'type': get_debate_type(title),
            'contributions': r.contribs,
            'group_key': _debate_group_key(title),
        })
    result.sort(key=lambda x: x['date'], reverse=True)
    return result


# ==========================================
# ROUTE 0a: EXPAND TOPIC TERMS API (used by preview-and-edit UI)
# ==========================================
@debate_scanner_bp.route('/api/expand_topic', methods=['POST'])
@limiter.limit("20 per minute; 200 per day")
def api_expand_topic():
    from flask import jsonify
    topic = (request.json or {}).get('topic', '').strip()
    if not topic:
        return jsonify({'error': 'No topic provided'}), 400
    if not GEMINI_API_KEY:
        return jsonify({'error': 'AI not configured'}), 503
    expanded = expand_search_query(topic, GEMINI_API_KEY)
    # Parse the OR terms back out for display: ("a" OR "b" OR "c") → ["a", "b", "c"]
    terms = re.findall(r'"([^"]+)"', expanded)
    return jsonify({'expanded': expanded, 'terms': terms})


# ==========================================
# ROUTE 0: MINISTERS BY DEPARTMENT API
# ==========================================
@debate_scanner_bp.route('/api/ministers_by_dept')
def api_ministers_by_dept():
    from flask import jsonify
    minister_data = get_minister_list()
    by_dept = minister_data.get('by_dept', {})
    dept = request.args.get('dept', '').strip()
    if dept:
        return jsonify(by_dept.get(dept, []))
    return jsonify(sorted(by_dept.keys()))


@debate_scanner_bp.route('/api/all_ministers')
def api_all_ministers():
    """Flattened, unique, sorted list of all current ministers across every
    department — the same source the department picker uses. Lets the minister
    field be browsable without first choosing a department (consistency with the
    Hansard Research tab). Resolution is unchanged: these are the same names the
    department flow already submits and resolves via cached_member."""
    from flask import jsonify
    by_dept = get_minister_list().get('by_dept', {})
    seen, names = set(), []
    for ministers in by_dept.values():
        for m in ministers:
            n = (m.get('name') or '').strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    names.sort()
    return jsonify([{"name": n} for n in names])


# ==========================================
# ROUTE 1: RESEARCH PAGE (GET only — searches post to the routes below)
# ==========================================
@debate_scanner_bp.route('/debates', methods=['GET'])
def scan_debates():
    # Handle stakeholder tab GET — show search form + org directory
    if request.args.get('mode') == 'stakeholder':
        from flask_app import TrackedStakeholder as _TS
        _user_sh = (_TS.query.filter_by(user_id=current_user.id).all()
                    if current_user.is_authenticated else [])
        return render_template('debate_scanner.html',
                               mode='stakeholder',
                               stakeholder_topic=request.args.get('stakeholder_topic', ''),
                               stakeholder_org=None,
                               stakeholder_org_id='',
                               user_stakeholders=_user_sh,
                               stakeholder_hansard=[], stakeholder_news=[],
                               stakeholder_publications=[], stakeholder_social=[],
                               stakeholder_briefing=None,
                               all_orgs=StakeholderOrg.all_active(),
                               orgs_by_category=StakeholderOrg.by_category(),
                               grouped_debates={}, error_message=None,
                               start_date='', end_date='',
                               topic='', topic_rows=[], topic_briefing=None,
                               topic_briefing_as_text='', house_filter='all',
                               selected_depts=[], wq_rows=[], oral_grouped=[],
                               urgent_grouped=[], statement_grouped=[],
                               debate_grouped=[], legislation_grouped=[],
                               debug_query='', user_pref=_get_user_pref(),
                               is_post=False,
                               departments=RESEARCH_DEPARTMENTS, selected_dept='All Departments',
                               selected_house='all', content_type='exclude_bills')

    return render_template('debate_scanner.html',
                           mode='dept',
                           grouped_debates={},
                           error_message=None,
                           start_date='', end_date='',
                           departments=RESEARCH_DEPARTMENTS, selected_dept='All Departments',
                           selected_house='all', content_type='exclude_bills',
                           is_post=False,
                           # Other tab defaults
                           topic='', topic_rows=[], topic_briefing=None,
                           topic_briefing_as_text='', house_filter='all',
                           stakeholder_topic='')


# ==========================================
# ROUTE 2: TOPIC SEARCH (NEW — PARALLEL API CALLS)
# ==========================================
@debate_scanner_bp.route('/debates_topic', methods=['GET', 'POST'])
@limiter.limit("10 per minute; 100 per day", methods=["POST"])
def debates_topic():
    topic_rows = []
    oral_rows = []
    statement_rows = []
    debate_rows = []
    urgent_rows = []
    oral_grouped = []
    urgent_grouped = []
    debate_grouped = []
    legislation_grouped = []
    legislation_rows = []
    statement_grouped = []
    wq_rows = []
    wq_total = 0
    wq_error = False
    topic_briefing = None
    ai_briefing_attempted = False
    result_counts = {}
    result_sources = []
    topic_briefing_as_text = ""
    opp_speaker_links = {}
    gov_speaker_links = {}
    error_message = None
    topic = ""
    narrow_keyword = ""
    start_date = ""
    end_date = ""
    house_filter = "all"
    selected_depts = []
    debug_query = ""
    user_pref = _get_user_pref()

    if request.method == 'POST':
        topic = request.form.get('topic', '').strip()
        narrow_keyword = request.form.get('narrow_keyword', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        house_filter = request.form.get('house_filter', 'all')
        selected_depts = [d for d in request.form.getlist('dept_filter') if d]
        # Pre-expanded query from the preview-and-edit UI — skip Gemini expansion if provided
        preset_expanded = request.form.get('expanded_query', '').strip()

        if not topic:
            error_message = "Please enter a topic to search."
        else:
            sources = research_sources_for_house(house_filter)

            # Use pre-expanded terms from preview UI if provided, otherwise expand with AI
            if preset_expanded:
                expanded = preset_expanded
            else:
                expanded = expand_search_query(topic, GEMINI_API_KEY) if GEMINI_API_KEY else f'"{topic}"'
            search_query = build_topic_query(expanded, topic, narrow_keyword,
                                             add_topic_words=not preset_expanded)
            debug_query = search_query

            # Resolve department ministers for minister-led DB search
            minister_people = []
            if selected_depts:
                m_data_for_ids = get_minister_list()
                for dept in selected_depts:
                    minister_people.extend(get_dept_minister_member_ids(dept, m_data_for_ids))

            # Run DB topic search, WQ search, and minister-led searches in parallel
            all_rows = []

            def _do_wq_fetch():
                return fetch_wqs_from_db(topic, selected_depts, start_date, end_date)

            source_counts = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                # Topic search: one future per source type, all via local DB
                topic_search_futs = {
                    executor.submit(
                        copy_current_request_context(fetch_from_db_topic),
                        search_query, src, start_date, end_date
                    ): src
                    for src in sources
                }
                wq_fut = executor.submit(copy_current_request_context(_do_wq_fetch))
                import logging as _mlog
                _mlog.warning(
                    f"[minister_dispatch] db_mode=True "
                    f"ministers={[(mp.get('name',''), mp.get('parliament_id')) for mp in minister_people]}"
                )
                # Minister search: one future per minister, all applicable sources in one DB call
                minister_futs = {}
                for mp in minister_people:
                    if not mp.get('parliament_id'):
                        continue
                    mp_sources = minister_sources(sources, mp.get('is_lord'))
                    if not mp_sources:
                        continue
                    fut = executor.submit(
                        copy_current_request_context(fetch_from_db_minister),
                        mp['parliament_id'], mp_sources, start_date, end_date
                    )
                    minister_futs[fut] = mp

                all_futs = list(topic_search_futs.keys()) + [wq_fut] + list(minister_futs.keys())
                for future in concurrent.futures.as_completed(all_futs):
                    if future is wq_fut:
                        try:
                            wq_rows, wq_total = future.result()
                            if wq_total == -1:
                                wq_error = True
                                wq_total = 0
                        except Exception:
                            wq_error = True
                    elif future in topic_search_futs:
                        src = topic_search_futs[future]
                        try:
                            rows = future.result()
                            errors = [r for r in rows if r.get('_error')]
                            good = [r for r in rows if not r.get('_error')]
                            source_counts[src] = f"{len(good)} results" + (f" | ERR: {errors[0]['_error']}" if errors else "")
                            all_rows.extend(rows)
                        except Exception as e:
                            source_counts[src] = f"exception: {e}"
                    else:
                        try:
                            all_rows.extend(future.result())
                        except Exception:
                            pass

            db_errors = [r['_error'] for r in all_rows if r.get('_error')]
            all_rows = [r for r in all_rows if not r.get('_error')]
            source_debug = ' | '.join(f"{s}: {c}" for s, c in sorted(source_counts.items()))
            if source_debug:
                debug_query += f" | [{source_debug}]"
            if db_errors:
                debug_query += ' | ERRORS: ' + '; '.join(set(db_errors))

            topic_rows = deduplicate_by_listurl(all_rows)
            import logging as _tlog
            _tlog.warning(f"[topic_rows] {len(topic_rows)} after dedup | source_counts={source_counts}")

            # Date safety net — DB filtering is primary; this catches any edge cases
            if start_date or end_date:
                topic_rows = [r for r in topic_rows
                              if (not start_date or r.get('hdate', '') >= start_date)
                              and (not end_date or r.get('hdate', '') <= end_date)]

            # No fetch_all_debate_sessions() needed — DB fetch already returns full sessions

            # Flag ministerial speakers and sort them to the top
            flag_minister_rows(topic_rows)

            if not topic_rows:
                error_message = f"No parliamentary contributions found for '{topic}'. Try a broader search term or wider date range."
            elif GEMINI_API_KEY and research_ai_summary_enabled():
                ai_briefing_attempted = True
                try:
                    # Build a balanced AI payload:
                    # - Ministers first (capped at 15, highest relevance)
                    # - Formal opposition parties next — ordered first within non-minister rows
                    #   so they're not crowded out by Labour backbenchers or empty-party rows
                    # - All other non-government voices: Labour backbenchers, Crossbench peers,
                    #   Independents, unknown party — included via _GOVERNMENT_PARTIES exclusion
                    #   (not in _OPPOSITION_PARTIES is intentionally broader)
                    _GOVERNMENT_PARTIES = {'Labour'}
                    _OPPOSITION_PARTIES = {
                        'Conservative', 'Liberal Democrat', 'Scottish National Party',
                        'Reform UK', 'Plaid Cymru', 'Green Party', 'Democratic Unionist Party',
                        'Ulster Unionist Party', 'Social Democratic and Labour Party',
                        'Sinn Féin', 'Alliance Party of Northern Ireland', 'Alba Party',
                    }
                    # Hard 6-month floor for the AI briefing payload — ensures key questions,
                    # ministerial statements, and opposition analysis reflect recent activity
                    # only, regardless of whether the user set a wider date range.
                    from datetime import timedelta
                    _six_months_ago = (datetime.utcnow() - timedelta(days=182)).strftime('%Y-%m-%d')
                    _briefing_cutoff = max(start_date, _six_months_ago) if start_date else _six_months_ago
                    briefing_rows = [r for r in topic_rows if r.get('hdate', '') >= _briefing_cutoff]
                    import logging as _dlog2
                    _dlog2.warning(f"[briefing_diag] topic_rows_at_briefing={len(topic_rows)} briefing_rows(6mo)={len(briefing_rows)} minister_flags={sum(1 for r in briefing_rows if r.get('is_minister'))}")
                    minister_rows = sorted(
                        [r for r in briefing_rows if r.get('is_minister')],
                        key=lambda x: -x.get('relevance', 0)
                    )[:15]
                    non_minister_rows = [r for r in briefing_rows if not r.get('is_minister')]
                    # opp_rows: formal opposition parties — prioritised in payload ordering
                    opp_rows = sorted(
                        [r for r in non_minister_rows if r.get('speaker_party', '') in _OPPOSITION_PARTIES],
                        key=lambda x: -x.get('relevance', 0)
                    )
                    # other_rows: Labour backbenchers, Crossbench peers, Independents, unknown
                    other_rows = sorted(
                        [r for r in non_minister_rows if r.get('speaker_party', '') not in _OPPOSITION_PARTIES],
                        key=lambda x: -x.get('relevance', 0)
                    )
                    if not opp_rows and non_minister_rows:
                        import logging as _log
                        _log.warning(
                            f"[briefing] opp_rows empty despite {len(non_minister_rows)} non-minister rows"
                            f" — sample parties: {list({r.get('speaker_party','') for r in non_minister_rows[:10]})}"
                        )
                    balanced = minister_rows + opp_rows[:10] + other_rows[:10]
                    # Frequency boost: speakers who appeared 2+ times across fetched sessions
                    # are significant participants — ensure their best speech is in balanced
                    # even if individual relevance scores didn't get them into the top-10.
                    from collections import Counter
                    speaker_counts = Counter(
                        r.get('speaker_name', '') for r in non_minister_rows if r.get('speaker_name')
                    )
                    already_in = {r.get('speaker_name') for r in balanced}
                    for name, count in speaker_counts.items():
                        if count >= 2 and name not in already_in:
                            candidates = [r for r in non_minister_rows if r.get('speaker_name') == name]
                            best = max(candidates, key=lambda x: x.get('relevance', 0))
                            balanced.append(best)
                            already_in.add(name)
                    # Build name → URL lookups so templates can render "View Speech" links.
                    opp_speaker_links = {}
                    for r in opp_rows + other_rows:
                        name = r.get('speaker_name', '')
                        url = r.get('listurl', '')
                        if name and url and name not in opp_speaker_links:
                            opp_speaker_links[name] = url

                    gov_speaker_links = {}
                    for r in minister_rows:
                        name = r.get('speaker_name', '')
                        url = r.get('listurl', '')
                        if name and url and name not in gov_speaker_links:
                            gov_speaker_links[name] = url
                    import logging as _dlog
                    _dlog.warning(
                        f"[briefing_diag] non_minister={len(non_minister_rows)} "
                        f"opp={len(opp_rows)} other={len(other_rows)} balanced={len(balanced)}"
                    )
                    _dlog.warning(
                        f"[briefing_diag] opp sample: {[(r.get('speaker_name',''),r.get('speaker_party','')) for r in opp_rows[:5]]}"
                    )
                    _dlog.warning(
                        f"[briefing_diag] other sample: {[(r.get('speaker_name',''),r.get('speaker_party','')) for r in other_rows[:5]]}"
                    )
                    _dlog.warning(
                        f"[briefing_diag] payload parties: {[r.get('speaker_party','') for r in balanced]}"
                    )
                    ai_payload = [
                        {'listurl': r['listurl'], 'speaker': r['speaker_name'],
                         'party': r['speaker_party'], 'date': r['hdate'],
                         'source': r['source_label'],
                         'text': (r.get('body_export') or r['body_clean'])[:1500]
                                 if r.get('is_minister')
                                 else r['body_clean'][:250],
                         'is_minister': r.get('is_minister', False),
                         'speech_count': speaker_counts.get(r.get('speaker_name', ''), 1)}
                        for r in balanced
                    ]
                    dept_context = (
                        f" The user is specifically interested in contributions related to the "
                        f"{', '.join(selected_depts)} {'portfolio' if len(selected_depts) == 1 else 'portfolios'}."
                        if selected_depts else ""
                    )
                    prompt = (
                        f"You are a senior UK civil servant writing a parliamentary briefing on: \"{topic}\"."
                        f"{dept_context}\n\n"
                        "Return ONLY a valid JSON object (no markdown fences) with these exact keys:\n\n"
                        "\"topic_summary\": 2–3 sentence overview of the parliamentary debate on this topic, "
                        "covering both government and opposition positions at a high level.\n\n"
                        "\"government_position\": Summarise the government's stated position on this topic "
                        "as expressed in Parliament — what ministers have said, any commitments made, "
                        "and the policy direction."
                        + (f" Focus specifically on {', '.join(selected_depts)} ministers." if selected_depts else "")
                        + "\n\n"
                        "\"opposition_position\": Summarise the positions of ALL opposition parties "
                        "(Conservative, Liberal Democrat, SNP, Reform UK, Plaid Cymru, and any others). "
                        "Give each party's distinct stance where they differ. "
                        "Do NOT include Labour backbenchers here — Labour is the governing party.\n\n"
                        "\"government_speakers\": Array of {\"name\", \"role\", \"stance\", \"listurl\"} — "
                        "only ministers, Secretaries of State, and PPSs who spoke on this topic. "
                        "\"listurl\" must be the exact listurl from the matching DATA entry for this speaker."
                        + (f" in the context of {', '.join(selected_depts)}" if selected_depts else "")
                        + ". Include Lords ministers (e.g. Baroness X). Up to 8 entries.\n\n"
                        "\"non_government_speakers\": Array of {\"name\", \"role_or_party\", \"stance\"} — "
                        "Include ALL non-government voices present in the DATA: opposition MPs asking "
                        "questions in Oral Questions sessions, shadow ministers, crossbench peers, "
                        "and any other non-Labour/non-government speaker. "
                        "Order: shadow frontbenchers / opposition spokespeople first (most senior first), "
                        "then opposition backbenchers, then crossbenchers. "
                        "IMPORTANT: In Oral Questions, Conservative or other opposition MPs asking "
                        "supplementary questions ARE non-government speakers — always include them. "
                        "speech_count in the DATA shows how many times each speaker contributed across "
                        "all fetched sessions — prioritise speakers with speech_count >= 2 as they are "
                        "the most active participants in this topic. "
                        "Return empty array ONLY if the DATA genuinely contains zero non-government speakers. "
                        "Up to 12 entries.\n\n"
                        "\"key_questions\": Array of {\"speaker\", \"role_or_party\", \"date\", \"source\", \"listurl\", \"question\"} — "
                        "The most significant questions raised by OPPOSITION and non-government speakers about this topic. "
                        "These should be actual interrogative challenges, probing questions, or lines of attack that a minister "
                        "would need to answer — not statements or positions. "
                        "Focus on: questions about policy gaps, accountability questions, questions challenging government figures or commitments, "
                        "questions that reveal opposition priorities or likely future lines of attack. "
                        "ONLY include questions from opposition frontbenchers, shadow ministers, opposition backbenchers, crossbenchers, "
                        "or Lords not in government. Do NOT include questions asked by government ministers or PPSs. "
                        "\"question\" should be a concise paraphrase of the actual question asked (1-2 sentences). "
                        "Use the exact listurl from the matching DATA entry. Up to 6 questions.\n\n"
                        "\"anticipated_questions\": Array of {\"question\", \"rationale\"} — "
                        "Up to 5 HARDER questions that have NOT yet been asked in Parliament but could plausibly come up "
                        "based on the debates, gaps in government answers, and opposition lines of attack identified above. "
                        "These should be the uncomfortable questions — ones that probe weaknesses in the government position, "
                        "unresolved policy tensions, or areas where the evidence base is thin. "
                        "\"rationale\" should be a brief (1 sentence) explanation of why this question is likely to arise. "
                        "These are for ministerial preparation — make them specific and genuinely challenging.\n\n"
                        "\"key_ministerial_statements\": Array of {\"speaker\", \"role\", \"date\", \"statement\", \"listurl\"} — "
                        "The 3–5 most significant on-record statements made by GOVERNMENT ministers on this topic in the DATA. "
                        "\"statement\" must be VERBATIM or a very close paraphrase of what was actually said — "
                        "quote directly from the DATA speeches, do not invent or infer. "
                        "Include the exact speaker name, their ministerial role, the date (YYYY-MM-DD), "
                        "and the listurl from the matching DATA entry. "
                        "Prioritise statements that: announce a commitment, give a specific figure or date, "
                        "directly rebut an opposition challenge, or articulate the clearest statement of government policy. "
                        "Return empty array if the DATA contains no clear ministerial statements. Up to 5 entries.\n\n"
                        "\"next_steps\": Any upcoming parliamentary business or announced policy milestones.\n\n"
                        "\"coverage_note\": Two sentences: (1) the exact date range of Hansard sources "
                        "in this synthesis (e.g. 'Sources cover January–March 2026'); "
                        "(2) the most recent ministerial statement identified — name the speaker and date explicitly "
                        "(e.g. 'Most recent government statement: Josh MacAlister MP, 15 March 2026'). "
                        "If the most recent date is not determinable from the data, state the latest date visible.\n\n"
                        f"DATA: {json.dumps(ai_payload)}"
                    )
                    model_path = get_working_model(GEMINI_API_KEY)
                    ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent"
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json"}
                    }
                    ai_resp = requests.post(ai_url, headers=_gemini_headers(GEMINI_API_KEY), json=payload, timeout=90)
                    if ai_resp.status_code == 503:
                        time.sleep(2)
                        ai_resp = requests.post(ai_url, headers=_gemini_headers(GEMINI_API_KEY), json=payload, timeout=90)
                    if ai_resp.status_code == 200:
                        raw_text = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                    else:
                        _dlog.warning('[debates_topic] Gemini briefing failed (HTTP %s) — degrading gracefully', ai_resp.status_code)
                        raw_text = None
                    _dlog.warning(f"[briefing_raw] non_govt_speakers snippet: {raw_text[raw_text.find('non_government'):raw_text.find('non_government')+300] if 'non_government' in (raw_text or '') else 'KEY NOT FOUND'}")
                    if raw_text:
                        topic_briefing = _parse_ai_json(raw_text)

                        # Verify government speakers against Parliament Members API (parallel)
                        govt_speakers = topic_briefing.get('government_speakers', [])
                        if govt_speakers:
                            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as vex:
                                vfutures = {vex.submit(copy_current_request_context(verify_government_speaker), s.get('name', '')): s for s in govt_speakers}
                                for vf in concurrent.futures.as_completed(vfutures):
                                    spk = vfutures[vf]
                                    try:
                                        v = vf.result()
                                        spk['verified'] = v['confirmed']
                                        spk['confirmed_role'] = v['role']
                                    except Exception:
                                        spk['verified'] = False
                                        spk['confirmed_role'] = ''

                        topic_briefing_as_text = format_briefing_as_text(topic_briefing, topic)
                    else:
                        debug_query += f" | AI HTTP {ai_resp.status_code} (Gemini unavailable)"
                        topic_briefing = None
                except Exception as e:
                    import logging
                    logging.error(f"AI briefing failed: {type(e).__name__}: {e}")
                    debug_query += f" | AI error: {type(e).__name__}: {str(e)[:120]}"
                    topic_briefing = None

            # Split rows into display sections, then group debates by session
            # Group ALL non-statement rows first, then classify each group.
            # Group-level classification is more reliable than per-row:
            # a group where no speech exceeds 300 words is an Oral PQ session
            # (prepared answer ~150w, follow-ups shorter) not a debate.
            # Genuine WMS: source=wms AND title doesn't look like a written answer
            statement_rows = [r for r in topic_rows
                              if r.get('source') == 'wms'
                              and 'written answer' not in r.get('debate_title', '').lower()]
            # wrans + any row whose title contains "written answer" (any source) → WQ section
            def _is_written_answer(r):
                return (r.get('source') == 'wrans'
                        or 'written answer' in r.get('debate_title', '').lower())

            # Written answers come from the written-questions search (wq_rows),
            # which carries both the question and the answer.
            wq_total = len(wq_rows)
            non_statement_rows = [r for r in topic_rows
                                  if r not in statement_rows
                                  and not _is_written_answer(r)]
            all_grouped = _group_by_debate(non_statement_rows)

            oral_grouped, urgent_grouped, debate_grouped, legislation_grouped = [], [], [], []
            for grp in all_grouped:
                section = _classify_group(grp)
                if section == 'oral':
                    oral_grouped.append(grp)
                elif section == 'urgent':
                    urgent_grouped.append(grp)
                elif section == 'legislation':
                    legislation_grouped.append(grp)
                elif section == 'wq':
                    pass  # written answer slipped through upstream filter — discard
                else:
                    debate_grouped.append(grp)

            # Group WMS by session so they render like debates, not flat speech snippets
            statement_grouped = _group_by_debate(statement_rows)

            # Filter WMS and Oral Questions to selected departments only.
            # WMS titles: "Written Ministerial Statements — [Dept]: [Topic]"
            # Oral titles: "Oral Answers to Questions — [Dept]" or "[Dept] Questions"
            if selected_depts:
                def _dept_in_title(title, depts):
                    title_lower = title.lower()
                    for d in depts:
                        # Full name match (e.g. "Department for Education")
                        if d.lower() in title_lower:
                            return True
                        # Key words only — strip generic words to get e.g. "Education", "Treasury"
                        keywords = [w for w in d.split()
                                    if w.lower() not in ('department', 'for', 'of', 'the',
                                                         'hm', 'ministry', 'and', 'office')
                                    and len(w) > 3]
                        if any(kw.lower() in title_lower for kw in keywords):
                            return True
                    return False
                statement_grouped = [g for g in statement_grouped
                                     if _dept_in_title(g['title'], selected_depts)]
                # Oral questions: only filter sessions that explicitly name a
                # DIFFERENT department (e.g. "Oral Answers to Questions — Treasury").
                # Topic-specific oral questions ("Plan 2 Student Loans: Repayment
                # Terms — Question") have no dept in the title and must not be removed.
                def _oral_from_other_dept(grp_title, depts):
                    t = grp_title.lower()
                    if 'oral answers to questions' not in t:
                        return False  # not a dept oral questions session → keep
                    return not _dept_in_title(grp_title, depts)
                oral_grouped = [g for g in oral_grouped
                                if not _oral_from_other_dept(g['title'], selected_depts)]

            # Sort WQs newest-first
            wq_rows.sort(key=lambda q: q.get('date_tabled', ''), reverse=True)

            # Flat row lists for JS download variables
            oral_rows = [r for grp in oral_grouped for r in grp['speeches']]
            urgent_rows = [r for grp in urgent_grouped for r in grp['speeches']]
            debate_rows = [r for grp in debate_grouped for r in grp['speeches']]
            legislation_rows = [r for grp in legislation_grouped for r in grp['speeches']]

            # Result count summary for transparency bar
            _all_grps = oral_grouped + urgent_grouped + statement_grouped + debate_grouped + legislation_grouped
            result_sources = list(dict.fromkeys(
                g['source_label'] for g in _all_grps if g.get('source_label')
            ))
            result_counts = {
                'oral': len(oral_grouped),
                'urgent': len(urgent_grouped),
                'statement': len(statement_grouped),
                'debate': len(debate_grouped),
                'legislation': len(legislation_grouped),
                'wq': len(wq_rows),
                'sessions': len(_all_grps),
            }

    return render_template('debate_scanner.html',
                           mode='topic',
                           topic=topic, topic_rows=topic_rows,
                           oral_rows=oral_rows, statement_rows=statement_rows,
                           urgent_rows=urgent_rows,
                           debate_rows=debate_rows,
                           oral_grouped=oral_grouped, debate_grouped=debate_grouped,
                           statement_grouped=statement_grouped,
                           urgent_grouped=urgent_grouped,
                           legislation_grouped=legislation_grouped,
                           legislation_rows=legislation_rows,
                           wq_rows=wq_rows, wq_total=wq_total, wq_error=wq_error,
                           topic_briefing=topic_briefing,
                           topic_briefing_as_text=topic_briefing_as_text,
                           ai_briefing_attempted=ai_briefing_attempted,
                           opp_speaker_links=opp_speaker_links,
                           gov_speaker_links=gov_speaker_links,
                           start_date=start_date, end_date=end_date,
                           house_filter=house_filter,
                           narrow_keyword=narrow_keyword,
                           selected_depts=selected_depts,
                           debug_query=debug_query,
                           user_pref=user_pref,
                           error_message=error_message,
                           grouped_debates={}, departments=RESEARCH_DEPARTMENTS,
                           selected_dept="All Departments", selected_house="all",
                           content_type="exclude_bills", is_post=True,
                           stakeholder_topic='',
                           result_counts=result_counts, result_sources=result_sources)


# ==========================================
# ROUTE 4: MINISTERIAL RECORD — DEBATE LIST
# ==========================================
@debate_scanner_bp.route('/debates_minister', methods=['GET', 'POST'])
def debates_minister():
    minister_debates = []
    minister_info = None
    error_message = None
    minister_name = ""
    topic = ""
    start_date = ""
    end_date = ""
    house_filter = "all"

    if request.method == 'POST':
        minister_name = request.form.get('minister_name', '').strip()
        topic = request.form.get('topic', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        house_filter = request.form.get('house_filter', 'all')

        if not minister_name:
            error_message = "Please select a minister."
        else:
            # Local resolution: controlled dropdown name → cached_member member_id.
            # Keyed on member_id, so recently created peers resolve like anyone else.
            member_id, matched_name, is_lord = _resolve_member_id(minister_name)
            if not member_id:
                error_message = f"Could not find '{minister_name}' in the member directory."
            else:
                minister_info = {'name': matched_name, 'member_id': member_id, 'is_lord': is_lord}
                minister_debates = fetch_minister_debates_local(
                    member_id, topic, start_date, end_date, house_filter)
                if not minister_debates:
                    error_message = (f"No contributions found for {matched_name} in the last "
                                     "12 months. Try a broader topic, a wider date range, or another chamber.")

    return render_template('debate_scanner.html',
                           mode='minister',
                           minister_name=minister_name, minister_info=minister_info,
                           minister_debates=minister_debates,
                           topic=topic, start_date=start_date, end_date=end_date,
                           house_filter=house_filter, error_message=error_message,
                           grouped_debates={}, departments=RESEARCH_DEPARTMENTS,
                           selected_dept="All Departments", selected_house="all",
                           content_type="exclude_bills", is_post=(request.method == 'POST'),
                           topic_rows=[], topic_briefing=None, topic_briefing_as_text='',
                           stakeholder_topic='')


# ==========================================
# ROUTE 5: MINISTERIAL RECORD — ANALYSE SELECTED
# ==========================================
@debate_scanner_bp.route('/debates_minister_analyze', methods=['POST'])
@limiter.limit("10 per minute; 100 per day")
def debates_minister_analyze():
    from hansard_archive.models import HansardContribution

    selected = request.form.getlist('selected_debates')
    minister_name = request.form.get('minister_name', 'the Minister').strip()

    if not selected:
        return "Please select at least one debate.", 400

    # Assemble transcripts from the LOCAL DB by session_id (the checkbox now
    # carries session_id||title||date). Full session text — all speakers, in
    # speech order — so Gemini has the Q&A context; it focuses on the minister
    # via the prompt. No external fetch.
    full_transcript_text = ""
    for item in selected:
        parts = item.split('||')
        try:
            session_id = int(parts[0])
        except (ValueError, IndexError):
            continue
        title = parts[1] if len(parts) > 1 else "Debate"
        date_val = parts[2] if len(parts) > 2 else ""

        contribs = (HansardContribution.query
                    .filter_by(session_id=session_id)
                    .order_by(HansardContribution.speech_order)
                    .all())
        if not contribs:
            continue
        body = "\n\n".join(
            f"{c.member_name or 'Unknown'}: {c.speech_text}" for c in contribs
        )
        full_transcript_text += f"### {title} ({date_val})\n\n{body}\n\n"

    if not full_transcript_text:
        return "Could not assemble transcripts for the selected debates.", 400

    if not GEMINI_API_KEY:
        return "Gemini API key is not configured.", 500

    prompt = (
        f"You are a senior UK Parliamentary Researcher. Analyze the following Hansard transcripts "
        f"focusing specifically on contributions by {minister_name}.\n\n"
        "Ignore any website navigation text. Focus only on debate content.\n\n"
        "IMPORTANT: In UK oral questions, the minister gives an OPENING ANSWER to the lead question "
        "before supplementary questions follow. Treat this opening answer as a speech — include it "
        "in full in Section 1 under the first exchange, as it is the minister's prepared position.\n\n"
        "Structure your response in Markdown exactly as follows:\n\n"
        f"## MINISTERIAL RECORD: {minister_name.upper()}\n\n"
        "## 1. SPEECHES & OPENING STATEMENTS\n"
        f"Include the full text of any opening statements, prepared answers, or standalone speeches "
        f"by {minister_name}. In oral questions sessions, this is the minister's first substantive "
        "answer to each topic before supplementaries begin. Quote the full text, not a summary.\n\n"
        "## 2. SUPPLEMENTARY Q&A EXCHANGES\n"
        "After the opening statement, list each follow-up (supplementary) exchange:\n"
        "**Q [{questioner name} ({party})]:** The question\n"
        f"**A [{minister_name}]:** The minister's response\n\n"
        "## 3. COMMUNICATION STYLE ANALYSIS\n"
        "Tone and register, how the minister handles difficult or hostile questions, "
        "recurring phrases or framing, approach to statistics and evidence.\n\n"
        "## 4. KEY POLICY POSITIONS STATED\n"
        f"Bullet points of the specific positions and commitments {minister_name} has expressed.\n\n"
        f"TRANSCRIPTS:\n{full_transcript_text}"
    )

    model_path = get_working_model(GEMINI_API_KEY)
    ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent"
    ai_resp = requests.post(ai_url, headers=_gemini_headers(GEMINI_API_KEY),
                            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=120)

    if ai_resp.status_code == 200:
        briefing_content = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    else:
        briefing_content = f"AI Analysis failed (Error {ai_resp.status_code})."

    return render_template('debate_briefing.html', briefing=briefing_content, selected_count=len(selected))


# ==========================================
# ROUTE 6: EXPORT BRIEFING TO WORD DOC
# ==========================================
@debate_scanner_bp.route('/download_debate_briefing', methods=['POST'])
def download_debate_briefing():
    if not Document:
        return "Word library missing.", 500

    briefing_text = request.form.get('briefing_text', '')
    if not briefing_text: return "No text found.", 400

    doc = Document()
    doc.add_heading('Parliamentary Intelligence Briefing', 0)
    
    for line in briefing_text.split('\n'):
        line = line.strip()
        if not line:
            doc.add_paragraph()
            continue
        if line.startswith('##'):
            doc.add_heading(line.replace('#', '').strip(), level=2)
        elif line.startswith('*') or line.startswith('-'):
            doc.add_paragraph(line.lstrip('*- ').strip(), style='List Bullet')
        else:
            doc.add_paragraph(line.replace('**', ''))

    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)

    return send_file(mem_doc, as_attachment=True, download_name=f"Briefing_{datetime.now().strftime('%Y%m%d')}.docx", mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


# ==========================================
# STAKEHOLDER RESEARCH — HELPERS
# ==========================================

def fetch_org_rss(rss_url, topic, limit=15):
    """Fetch RSS feed and return items loosely matching topic. Returns [] on failure."""
    if not feedparser or not rss_url:
        return []
    try:
        feed = feedparser.parse(rss_url)
        topic_words = set(re.findall(r'\b\w{4,}\b', topic.lower()))
        results = []
        for entry in feed.entries[:40]:
            title = entry.get('title', '')
            summary = entry.get('summary', '') or entry.get('description', '')
            text = (title + ' ' + summary).lower()
            # Include if any topic word appears in the entry
            if not topic_words or any(w in text for w in topic_words):
                published = ''
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        published = datetime(*entry.published_parsed[:3]).strftime('%Y-%m-%d')
                    except Exception:
                        pass
                results.append({
                    'title': title,
                    'link': entry.get('link', ''),
                    'published': published,
                    'summary': re.sub(r'<[^>]+>', ' ', summary)[:300].strip(),
                    'source_type': 'publication',
                })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def fetch_org_bluesky(handle, topic, limit=10):
    """Fetch recent Bluesky posts from a handle and filter loosely by topic."""
    try:
        from atproto import Client as BskyClient
        BSKY_HANDLE = os.environ.get('BSKY_HANDLE')
        BSKY_PASSWORD = os.environ.get('BSKY_PASSWORD')
        if not BSKY_HANDLE or not BSKY_PASSWORD:
            return []
        bsky = BskyClient()
        bsky.login(BSKY_HANDLE, BSKY_PASSWORD)
        clean_handle = handle.lstrip('@')
        feed = bsky.get_author_feed(actor=clean_handle, limit=25)
        topic_words = set(re.findall(r'\b\w{4,}\b', topic.lower()))
        results = []
        for item in feed.feed:
            post = item.post
            text = post.record.text if hasattr(post.record, 'text') else ''
            if not topic_words or any(w in text.lower() for w in topic_words):
                post_id = post.uri.split('/')[-1]
                results.append({
                    'title': text[:120],
                    'link': f'https://bsky.app/profile/{clean_handle}/post/{post_id}',
                    'published': str(post.indexed_at)[:10] if hasattr(post, 'indexed_at') else '',
                    'summary': text[:300],
                    'source_type': 'social',
                })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def fetch_stakeholder_news(org_names, topic, start_date='', end_date='', limit=30):
    """Search News API for media coverage mentioning stakeholder orgs on a topic."""
    NEWS_API_KEY = os.environ.get('NEWS_API_KEY')
    if not NEWS_API_KEY:
        return []
    try:
        from newsapi import NewsApiClient
        newsapi = NewsApiClient(api_key=NEWS_API_KEY)
        # Build query: topic AND (org1 OR org2 OR ...)
        org_clause = ' OR '.join(f'"{n}"' for n in org_names[:5])  # API has query length limits
        query = f'({topic}) AND ({org_clause})'
        kwargs = {'q': query, 'language': 'en', 'sort_by': 'relevancy', 'page_size': limit}
        if start_date:
            kwargs['from_param'] = start_date
        if end_date:
            kwargs['to'] = end_date
        resp = newsapi.get_everything(**kwargs)
        results = []
        for art in resp.get('articles', []):
            results.append({
                'title': art.get('title', ''),
                'link': art.get('url', ''),
                'published': (art.get('publishedAt') or '')[:10],
                'summary': art.get('description') or art.get('title', ''),
                'outlet': art.get('source', {}).get('name', ''),
                'source_type': 'news',
            })
        return results
    except Exception:
        return []


def generate_stakeholder_briefing(topic, org=None, hansard_rows=None, news=None,
                                  publications=None, social=None):
    """Generate an AI summary of stakeholder positions on a topic."""
    if not GEMINI_API_KEY:
        return None
    hansard_rows = hansard_rows or []
    news = news or []
    publications = publications or []
    social = social or []

    org_name = (org['name'] if isinstance(org, dict) else org.name) if org else None
    org_context = f'Organisation: {org_name}\n' if org_name else 'Multiple stakeholder organisations\n'

    # Build evidence summary (capped to avoid 503)
    evidence_lines = []
    for r in hansard_rows[:8]:
        evidence_lines.append(f'[Hansard] {r.get("speaker_name","")} ({r.get("hdate","")}): '
                               f'{clean_body_text(r.get("body",""))[:200]}')
    for r in news[:6]:
        evidence_lines.append(f'[News/{r.get("outlet","")}] {r.get("title","")} ({r.get("published","")}): '
                               f'{r.get("summary","")[:200]}')
    for r in publications[:6]:
        evidence_lines.append(f'[Publication] {r.get("title","")} ({r.get("published","")}): '
                               f'{r.get("summary","")[:200]}')
    for r in social[:4]:
        evidence_lines.append(f'[Social] {r.get("summary","")[:150]}')

    if not evidence_lines:
        return None

    evidence_text = '\n'.join(evidence_lines)
    prompt = (
        f'You are a UK civil service parliamentary researcher. Analyse the following evidence '
        f'about what stakeholders are saying on the topic: "{topic}".\n\n'
        f'{org_context}\n'
        f'EVIDENCE:\n{evidence_text}\n\n'
        f'Return a JSON object with these fields:\n'
        f'"summary": 2-3 sentence overview of the stakeholder landscape on this topic.\n'
        f'"stated_positions": Array of {{\"org\", \"position\", \"source_type\", \"source_date\"}} — '
        f'one entry per organisation visible in the evidence, summarising their stated position in 1-2 sentences. '
        f'Include the type of source (Hansard/News/Publication/Social) and date.\n'
        f'"key_asks": Array of strings — the main policy asks or demands from stakeholders (up to 5).\n'
        f'"coverage_note": One sentence on the breadth and recency of evidence found.\n'
        f'Return ONLY valid JSON, no markdown fences.'
    )

    try:
        model_path = get_working_model(GEMINI_API_KEY)
        ai_url = (f'https://generativelanguage.googleapis.com/v1beta/{model_path}'
                  ':generateContent')
        payload = {'contents': [{'parts': [{'text': prompt}]}],
                   'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 800}}
        resp = requests.post(ai_url, headers=_gemini_headers(GEMINI_API_KEY), json=payload, timeout=25)
        if resp.status_code == 503:
            time.sleep(3)
            resp = requests.post(ai_url, headers=_gemini_headers(GEMINI_API_KEY), json=payload, timeout=25)
        if resp.status_code == 200:
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            import logging as _sblog
            _sblog.warning('[generate_stakeholder_briefing] Gemini returned %s', resp.status_code)
            raw = None
        if raw:
            return _parse_ai_json(raw)
    except Exception as _sbe:
        import logging as _sblog
        _sblog.warning('[generate_stakeholder_briefing] Gemini exception: %s', _sbe)
    return None


def _discover_rss(website):
    """Try to auto-discover an RSS feed URL from a website domain."""
    if not website:
        return None
    base = website.rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    candidates = [
        base + '/feed', base + '/rss', base + '/rss.xml',
        base + '/feed.xml', base + '/news/rss', base + '/news/feed',
        base + '/blog/feed', base + '/publications/rss',
    ]
    for url in candidates:
        try:
            r = requests.get(url, timeout=6, allow_redirects=True,
                             headers={'User-Agent': 'Mozilla/5.0'})
            ct = r.headers.get('Content-Type', '')
            if r.status_code == 200 and ('xml' in ct or 'rss' in ct or 'atom' in ct
                                          or r.text.strip().startswith('<')):
                return url
        except Exception:
            pass
    return None


def _stakeholder_org_view(obj):
    """Return a uniform dict from either a StakeholderOrg or TrackedStakeholder.
    All downstream code (templates, fetch functions) uses this shape."""
    return {
        'id':                  obj.id,
        'name':                obj.name,
        'short_name':          getattr(obj, 'short_name', None) or obj.name,
        'category':            getattr(obj, 'category', 'My Stakeholders'),
        'website':             getattr(obj, 'website', None),
        'bsky_handle':         getattr(obj, 'bsky_handle', None),
        'rss_url':             getattr(obj, 'rss_url', None),
        'hansard_search_name': getattr(obj, 'hansard_search_name', None) or obj.name,
        'description':         getattr(obj, 'description', None),
        '_type':               'user' if obj.__class__.__name__ == 'TrackedStakeholder' else 'global',
    }


# ==========================================
# ROUTE 9: STAKEHOLDER SEARCH
# ==========================================
@debate_scanner_bp.route('/stakeholder_search', methods=['POST'])
@limiter.limit("10 per minute; 100 per day")
def stakeholder_search():
    from flask_app import TrackedStakeholder
    topic = request.form.get('stakeholder_topic', '').strip()
    org_id = request.form.get('stakeholder_org_id', '').strip()
    start_date = request.form.get('stakeholder_start', '')
    end_date = request.form.get('stakeholder_end', '')

    all_orgs = StakeholderOrg.all_active()
    orgs_by_category = StakeholderOrg.by_category()

    # Load user's personal stakeholders for the dropdown
    user_stakeholders = []
    if current_user.is_authenticated:
        user_stakeholders = TrackedStakeholder.query.filter_by(user_id=current_user.id).all()

    if not topic:
        return render_template('debate_scanner.html', mode='stakeholder',
                               stakeholder_topic='', stakeholder_org=None,
                               stakeholder_org_id='',
                               user_stakeholders=user_stakeholders,
                               all_orgs=all_orgs, orgs_by_category=orgs_by_category,
                               stakeholder_hansard=[], stakeholder_news=[],
                               stakeholder_publications=[], stakeholder_social=[],
                               stakeholder_briefing=None,
                               # other tab defaults
                               topic='', topic_rows=[], topic_briefing=None,
                               topic_briefing_as_text='', house_filter='all',
                               selected_depts=[], start_date='', end_date='',
                               wq_rows=[], oral_grouped=[], urgent_grouped=[],
                               statement_grouped=[], debate_grouped=[],
                               legislation_grouped=[], debug_query='',
                               user_pref=None, is_post=False)

    # Resolve selected org — global StakeholderOrg or user's TrackedStakeholder
    org = None
    if org_id:
        if org_id.startswith('user-'):
            try:
                sh_id = int(org_id[5:])
                sh = TrackedStakeholder.query.get(sh_id)
                if sh and sh.user_id == current_user.id:
                    org = _stakeholder_org_view(sh)
            except Exception:
                pass
        else:
            try:
                db_org = StakeholderOrg.query.get(int(org_id))
                if db_org:
                    org = _stakeholder_org_view(db_org)
            except Exception:
                pass

    # Convert all_orgs to view dicts for downstream uniformity
    all_org_views = [_stakeholder_org_view(o) for o in all_orgs]

    # Determine which orgs to search
    search_org_views = [org] if org else all_org_views
    org_names = [o['hansard_search_name'] or o['short_name'] or o['name'] for o in search_org_views]

    date_range = ''
    if start_date and end_date:
        date_range = f'{start_date.replace("-","")}..{end_date.replace("-","")}'
    elif start_date:
        date_range = f'{start_date.replace("-","")}..{datetime.now().strftime("%Y%m%d")}'

    # Parallel fetch
    hansard_rows = []
    news_items = []
    publications = []
    social_posts = []

    def _do_hansard():
        rows = []
        clean_query = (f'"{topic}" AND "{org["hansard_search_name"] or org["name"]}"'
                       if org else f'"{topic}"')
        for src in ['commons', 'lords', 'westminsterhall']:
            rows.extend(fetch_hansard_topic(clean_query, src, date_range))
        return deduplicate_by_listurl(rows)

    def _do_news():
        return fetch_stakeholder_news(org_names, topic, start_date, end_date)

    def _do_rss():
        results = []
        rss_orgs = [o for o in search_org_views if o.get('rss_url')]
        for o in rss_orgs[:10]:
            items = fetch_org_rss(o['rss_url'], topic)
            for item in items:
                item['org_name'] = o['name']
            results.extend(items)
        return sorted(results, key=lambda x: x.get('published', ''), reverse=True)

    def _do_bluesky():
        results = []
        bsky_orgs = [o for o in search_org_views if o.get('bsky_handle')]
        for o in bsky_orgs[:5]:
            items = fetch_org_bluesky(o['bsky_handle'], topic)
            for item in items:
                item['org_name'] = o['name']
            results.extend(items)
        return sorted(results, key=lambda x: x.get('published', ''), reverse=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        fut_hansard = executor.submit(copy_current_request_context(_do_hansard))
        fut_news = executor.submit(copy_current_request_context(_do_news))
        fut_rss = executor.submit(copy_current_request_context(_do_rss))
        fut_bsky = executor.submit(copy_current_request_context(_do_bluesky))
        try:
            hansard_rows = fut_hansard.result(timeout=30)
        except Exception:
            hansard_rows = []
        try:
            news_items = fut_news.result(timeout=20)
        except Exception:
            news_items = []
        try:
            publications = fut_rss.result(timeout=20)
        except Exception:
            publications = []
        try:
            social_posts = fut_bsky.result(timeout=20)
        except Exception:
            social_posts = []

    # Apply date filter to Hansard rows
    if start_date or end_date:
        hansard_rows = [r for r in hansard_rows
                        if (not start_date or r.get('hdate', '') >= start_date)
                        and (not end_date or r.get('hdate', '') <= end_date)]

    # Flag ministers in Hansard results
    flag_minister_rows(hansard_rows)
    for row in hansard_rows:
        row['body_clean'] = clean_body_text(row.get('body', ''))

    # AI briefing
    briefing = None
    if hansard_rows or news_items or publications or social_posts:
        try:
            briefing = generate_stakeholder_briefing(
                topic, org=org,
                hansard_rows=hansard_rows,
                news=news_items,
                publications=publications,
                social=social_posts,
            )
        except Exception:
            pass

    user_pref = _get_user_pref()

    return render_template('debate_scanner.html',
        mode='stakeholder',
        stakeholder_topic=topic,
        stakeholder_org=org,
        stakeholder_org_id=org_id,
        stakeholder_hansard=hansard_rows,
        stakeholder_news=news_items,
        stakeholder_publications=publications,
        stakeholder_social=social_posts,
        stakeholder_briefing=briefing,
        all_orgs=all_orgs,
        orgs_by_category=orgs_by_category,
        user_stakeholders=user_stakeholders,
        # other tab defaults (required by template)
        topic=topic, topic_rows=[], topic_briefing=None,
        topic_briefing_as_text='', house_filter='all',
        selected_depts=[], start_date=start_date, end_date=end_date,
        wq_rows=[], oral_grouped=[], urgent_grouped=[],
        statement_grouped=[], debate_grouped=[],
        legislation_grouped=[], debug_query='',
        user_pref=user_pref, is_post=True)


# ==========================================
# ROUTE 10: ADD STAKEHOLDER ORG
# ==========================================
@debate_scanner_bp.route('/stakeholder_add', methods=['POST'])
@login_required
def stakeholder_add():
    name = request.form.get('org_name', '').strip()
    website = request.form.get('org_website', '').strip().lstrip('https://').lstrip('http://').rstrip('/')
    department = request.form.get('org_department', '').strip()

    if not name or not website:
        return jsonify({'error': 'Name and website required'}), 400

    # Map department to category
    dept_to_category = {
        'Department for Education': 'Education',
        'Department of Health and Social Care': 'Health & Social Care',
        'HM Treasury': 'Economics & Finance',
        'Home Office': 'Home Affairs',
        'Ministry of Defence': 'Defence',
        'Ministry of Justice': 'Justice',
        'Department for Science, Innovation and Technology': 'Science & Technology',
        'Cabinet Office': 'Government & Public Administration',
    }
    category = dept_to_category.get(department, department or 'Other')

    from extensions import db as _db

    # Check for duplicate
    existing = StakeholderOrg.query.filter(
        StakeholderOrg.name.ilike(name)
    ).first()
    if existing:
        return jsonify({'error': f'"{name}" is already in the directory'}), 409

    # Attempt RSS auto-discovery (best-effort)
    rss_url = None
    try:
        rss_url = _discover_rss(website)
    except Exception:
        pass

    org = StakeholderOrg(
        name=name, category=category,
        website=website, rss_url=rss_url,
        hansard_search_name=name, active=True,
    )
    try:
        _db.session.add(org)
        _db.session.commit()
    except Exception:
        _db.session.rollback()
        return jsonify({'error': 'Could not save organisation'}), 500

    return jsonify({
        'id': org.id, 'name': org.name, 'category': org.category,
        'website': org.website, 'rss_found': bool(rss_url),
    })


# ==========================================
# ROUTE 8: SECTIONED RESEARCH BRIEF DOWNLOAD
# ==========================================
@debate_scanner_bp.route('/download_research_brief', methods=['POST'])
def download_research_brief():
    if not Document:
        return "Word library missing.", 500

    from docx.shared import Pt, RGBColor, Inches
    from docx.oxml.ns import qn as _qn
    from docx.oxml import OxmlElement as _OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    topic = request.form.get('topic', 'Parliamentary Research').strip()
    mode = request.form.get('mode', '')
    briefing_text = request.form.get('briefing_text', '')
    sections_json = request.form.get('sections_json', '[]')
    depts_json = request.form.get('depts_json', '[]')
    briefing_json = request.form.get('briefing_json', '')
    start_date = request.form.get('start_date', '')
    end_date = request.form.get('end_date', '')
    try:
        sections = json.loads(sections_json)
    except Exception:
        sections = []
    try:
        depts = json.loads(depts_json)
    except Exception:
        depts = []
    try:
        briefing_struct = json.loads(briefing_json) if briefing_json else None
        if isinstance(briefing_struct, dict) and not briefing_struct:
            briefing_struct = None  # treat empty {} as no briefing
    except Exception:
        briefing_struct = None

    # ── Stakeholder brief mode ──────────────────────────────────────
    if mode == 'stakeholder':
        def _parse(field):
            try:
                return json.loads(request.form.get(field, '[]'))
            except Exception:
                return []

        sk_news    = _parse('stakeholder_news')
        sk_pubs    = _parse('stakeholder_publications')
        sk_social  = _parse('stakeholder_social')
        sk_hansard = _parse('stakeholder_hansard')

        doc = Document()
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        def _clr(run, hex_str):
            r, g, b = int(hex_str[0:2],16), int(hex_str[2:4],16), int(hex_str[4:6],16)
            run.font.color.rgb = RGBColor(r, g, b)

        # Cover heading
        title_p = doc.add_heading(f'Stakeholder Research Brief', 0)
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub = doc.add_paragraph(f'Topic: {topic}   ·   Generated {datetime.now().strftime("%d %b %Y")}')
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub.runs[0].italic = True
        doc.add_paragraph()

        notice = doc.add_paragraph(
            '⚠ AI-generated — verify positions against source links before use in official documents.')
        notice.runs[0].font.size = Pt(9)
        notice.runs[0].italic = True
        _clr(notice.runs[0], '856404')
        doc.add_paragraph()

        # AI summary section
        if briefing_struct:
            b = briefing_struct
            doc.add_heading('AI Stakeholder Summary', 1)
            if b.get('summary'):
                doc.add_paragraph(b['summary'])
            if b.get('stated_positions'):
                doc.add_heading('Stated Positions', 2)
                for p in b['stated_positions']:
                    para = doc.add_paragraph(style='List Bullet')
                    run = para.add_run(p.get('org', '') + ': ')
                    run.bold = True
                    src = p.get('source_type', '')
                    dt = p.get('source_date', '')
                    meta = f' [{", ".join(filter(None, [src, dt]))}]' if src or dt else ''
                    para.add_run(p.get('position', '') + meta)
            if b.get('key_asks'):
                doc.add_heading('Key Asks from Stakeholders', 2)
                for ask in b['key_asks']:
                    doc.add_paragraph(ask, style='List Bullet')
            if b.get('coverage_note'):
                p = doc.add_paragraph(b['coverage_note'])
                p.runs[0].italic = True
                p.runs[0].font.size = Pt(9)
            doc.add_paragraph()

        def _add_item_table(heading, items, cols):
            """Add a simple table section for stakeholder results."""
            if not items:
                return
            doc.add_heading(heading, 1)
            tbl = doc.add_table(rows=1, cols=len(cols))
            tbl.style = 'Table Grid'
            for i, label in enumerate(cols):
                cell = tbl.rows[0].cells[i]
                cell.text = label
                cell.paragraphs[0].runs[0].bold = True
            for item in items:
                row = tbl.add_row().cells
                if cols[0] == 'Date':
                    row[0].text = item.get('published', '')
                    row[1].text = item.get('org_name', '') or item.get('outlet', '')
                    row[2].text = item.get('title', '')
                    if len(cols) > 3:
                        row[3].text = (item.get('summary', '') or '')[:200]
                else:
                    row[0].text = item.get('speaker_name', '')
                    row[1].text = item.get('hdate', '')
                    row[2].text = clean_body_text(item.get('body', ''))[:300]
            doc.add_paragraph()

        _add_item_table('News Media Coverage', sk_news,
                        ['Date', 'Outlet', 'Headline', 'Summary'])
        _add_item_table('Reports & Publications', sk_pubs,
                        ['Date', 'Organisation', 'Title', 'Summary'])
        _add_item_table('Social Media (Bluesky)', sk_social,
                        ['Date', 'Organisation', 'Post', 'Link'])
        _add_item_table('Parliamentary Mentions', sk_hansard,
                        ['Speaker', 'Date', 'Extract'])

        mem_doc = io.BytesIO()
        doc.save(mem_doc)
        mem_doc.seek(0)
        safe_topic = re.sub(r'[^\w\s-]', '', topic)[:40].strip()
        filename = f'Stakeholder Brief - {safe_topic} - {datetime.now().strftime("%Y%m%d")}.docx'
        return send_file(mem_doc, as_attachment=True, download_name=filename,
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    # ── end stakeholder mode ────────────────────────────────────────

    SECTION_TITLES = {
        'wq': 'Written Questions & Answers',
        'oral': 'Oral Questions',
        'urgent': 'Urgent Questions',
        'statement': 'Ministerial Statements',
        'debate': 'Parliamentary Debates',
        'legislation': 'Legislation',
    }
    REL_LABELS = {'high': 'HIGH RELEVANCE', 'medium': 'MODERATE RELEVANCE', 'low': 'SESSION CONTEXT'}

    def _set_cell_bg(cell, hex_colour):
        tc = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd = _OxmlElement('w:shd')
        shd.set(_qn('w:val'), 'clear')
        shd.set(_qn('w:color'), 'auto')
        shd.set(_qn('w:fill'), hex_colour)
        tcPr.append(shd)

    def _set_run_colour(run, hex_colour):
        run.font.color.rgb = RGBColor(
            int(hex_colour[0:2], 16),
            int(hex_colour[2:4], 16),
            int(hex_colour[4:6], 16)
        )

    doc = Document()

    # ── Page margins ──────────────────────────────────────────────
    for section_obj in doc.sections:
        section_obj.top_margin = Inches(0.9)
        section_obj.bottom_margin = Inches(0.9)
        section_obj.left_margin = Inches(1.1)
        section_obj.right_margin = Inches(1.1)

    # ── Title block ───────────────────────────────────────────────
    title_p = doc.add_heading('Parliamentary Research Brief', 0)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    total_contributions = sum(len(s.get('items', [])) for s in sections)
    unique_debates = len({
        (r.get('listurl', '')[:50] or r.get('debate_title', ''))
        for s in sections for r in s.get('items', [])
        if s.get('type') != 'wq'
    })

    meta_lines = [f'Topic: {topic}']
    if depts:
        meta_lines.append(f'Department(s): {", ".join(depts)}')
    date_parts = []
    if start_date: date_parts.append(f'from {start_date}')
    if end_date: date_parts.append(f'to {end_date}')
    if date_parts:
        meta_lines.append('Date range: ' + ' '.join(date_parts))
    if total_contributions:
        debate_note = f'{unique_debates} debate(s)' if unique_debates else ''
        meta_lines.append(f'Sources: {total_contributions} contributions' + (f' from {debate_note}' if debate_note else ''))
    meta_lines.append(f'Generated: {datetime.now().strftime("%d %B %Y at %H:%M")}')
    meta_lines.append('AI model: Gemini 1.5 Flash (with Claude Haiku fallback)')
    meta_lines.append('Source: Westminster Brief — westminsterbrief.co.uk')

    for line in meta_lines:
        mp = doc.add_paragraph(line)
        mp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        mp.runs[0].font.size = Pt(10)
        mp.runs[0].font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    doc.add_paragraph()

    # ── Summary table ─────────────────────────────────────────────
    non_empty = [(s, SECTION_TITLES.get(s.get('type',''), s.get('type','').title()), s.get('items', []))
                 for s in sections if s.get('items')]
    if non_empty:
        doc.add_heading('Contents Summary', 1)
        tbl = doc.add_table(rows=1, cols=3)
        tbl.style = 'Table Grid'
        hdr = tbl.rows[0].cells
        for i, label in enumerate(['Section', 'Contributions', 'Notes']):
            hdr[i].text = label
            hdr[i].paragraphs[0].runs[0].bold = True
            _set_cell_bg(hdr[i], '1C3E6E')
            hdr[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        for sec, title, items in non_empty:
            row = tbl.add_row().cells
            row[0].text = title
            row[1].text = str(len(items))
            ministers = sum(1 for r in items if r.get('is_minister') and not r.get('minister_unverified'))
            row[2].text = f'{ministers} minister(s)' if ministers else '—'
        doc.add_paragraph()

    # ── AI Summary ────────────────────────────────────────────────
    if briefing_struct or briefing_text:
        doc.add_heading('AI Parliamentary Briefing', 1)
        notice = doc.add_paragraph(
            '⚠ AI-generated — review for accuracy and remove any non-neutral language before use.')
        notice.runs[0].font.size = Pt(9)
        notice.runs[0].italic = True
        _set_run_colour(notice.runs[0], '856404')

        if briefing_struct:
            b = briefing_struct
            coverage = b.get('coverage_note', '')

            def _section_caveat(label):
                caveat_text = f'AI synthesis — {label}. Verify against source links before ministerial submission.'
                if coverage:
                    caveat_text += f' {coverage}'
                cp = doc.add_paragraph(caveat_text)
                cp.runs[0].font.size = Pt(8)
                cp.runs[0].italic = True
                _set_run_colour(cp.runs[0], '856404')

            # Topic summary
            if b.get('topic_summary'):
                doc.add_heading('Summary', 2)
                doc.add_paragraph(b['topic_summary'])

            # Government / Opposition positions with per-section caveats
            if b.get('government_position'):
                doc.add_heading('Government Position', 2)
                _section_caveat('government statements from Hansard')
                doc.add_paragraph(b['government_position'])
            if b.get('opposition_position'):
                doc.add_heading('Opposition Position', 2)
                _section_caveat('opposition contributions from Hansard')
                doc.add_paragraph(b['opposition_position'])

            # Government speakers
            govt_spks = b.get('government_speakers', [])
            if govt_spks:
                doc.add_heading('Government Speakers', 2)
                for s in govt_spks:
                    role = s.get('confirmed_role') or s.get('role', '')
                    p = doc.add_paragraph(style='List Bullet')
                    p.add_run(s.get('name', '')).bold = True
                    if role:
                        p.add_run(f' ({role})')
                    if s.get('stance'):
                        p.add_run(f': {s["stance"]}')

            # Opposition speakers
            opp_spks = b.get('non_government_speakers', [])
            if opp_spks:
                doc.add_heading('Opposition / Non-Government Speakers', 2)
                for s in opp_spks:
                    p = doc.add_paragraph(style='List Bullet')
                    p.add_run(s.get('name', '')).bold = True
                    if s.get('role_or_party'):
                        p.add_run(f' ({s["role_or_party"]})')
                    if s.get('stance'):
                        p.add_run(f': {s["stance"]}')

            # Key opposition questions
            kqs = b.get('key_questions', [])
            if kqs:
                doc.add_heading('Key Opposition Questions', 2)
                doc.add_paragraph('Questions raised in Parliament — likely lines of challenge in future debates.').italic = True
                for q in kqs:
                    p = doc.add_paragraph(style='List Bullet')
                    p.add_run(f'{q.get("speaker", "")}').bold = True
                    role_party = q.get('role_or_party', '')
                    date = q.get('date', '')
                    if role_party or date:
                        p.add_run(f' ({", ".join(filter(None, [role_party, date]))})')
                    p.add_run(f': {q.get("question", "")}')
                    # Source hyperlink on its own line
                    url = q.get('listurl', '')
                    if url:
                        url = _full_url(url)
                        src_p = doc.add_paragraph()
                        _add_hyperlink(src_p, url, '↗ View on Hansard')
                        if src_p.runs:
                            src_p.runs[0].font.size = Pt(8)
                        src_p.paragraph_format.left_indent = Pt(18)

            # Anticipated / harder questions
            aqs = b.get('anticipated_questions', [])
            if aqs:
                doc.add_heading('Anticipated Questions (AI-Generated)', 2)
                doc.add_paragraph(
                    'Questions not yet raised in Parliament but likely based on gaps in government answers '
                    'and opposition lines of attack. For ministerial preparation only.').italic = True
                for q in aqs:
                    p = doc.add_paragraph(style='List Bullet')
                    p.add_run(q.get('question', '')).bold = False
                    if q.get('rationale'):
                        rat = doc.add_paragraph(f'Why likely: {q["rationale"]}')
                        rat.runs[0].font.size = Pt(9)
                        rat.runs[0].italic = True
                        _set_run_colour(rat.runs[0], '666666')

            # Next steps
            if b.get('next_steps'):
                doc.add_heading('Next Steps', 2)
                doc.add_paragraph(b['next_steps'])

            if b.get('coverage_note'):
                p = doc.add_paragraph(f'Coverage note: {b["coverage_note"]}')
                p.runs[0].font.size = Pt(9)
                _set_run_colour(p.runs[0], '666666')

        elif briefing_text:
            # Fallback: render flat text if structured briefing not available
            for line in briefing_text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                if line.startswith('## '):
                    doc.add_heading(line[3:].strip(), level=2)
                elif line.startswith('* ') or line.startswith('- '):
                    doc.add_paragraph(line[2:].strip(), style='List Bullet')
                else:
                    doc.add_paragraph(line.replace('**', ''))

        doc.add_paragraph()

    # ── Sections ──────────────────────────────────────────────────
    SECTION_ORDER = ['debate', 'oral', 'urgent', 'statement', 'wq']
    sections_by_type = {s.get('type'): s for s in sections}

    for sec_type in SECTION_ORDER:
        section = sections_by_type.get(sec_type)
        if not section:
            continue
        items = section.get('items', [])
        if not items:
            continue
        title = SECTION_TITLES.get(sec_type, sec_type.title())
        doc.add_heading(f'{title} ({len(items)})', 1)

        if sec_type == 'wq':
            # Written Questions as a table
            tbl = doc.add_table(rows=1, cols=5)
            tbl.style = 'Table Grid'
            hdr = tbl.rows[0].cells
            for i, label in enumerate(['Status', 'Date', 'Department', 'Member (Party)', 'Question / Answer']):
                hdr[i].text = label
                hdr[i].paragraphs[0].runs[0].bold = True
                _set_cell_bg(hdr[i], '1C3E6E')
                hdr[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            for q in items:
                row = tbl.add_row().cells
                status = 'Answered' if q.get('is_answered') else 'Unanswered'
                if q.get('is_withdrawn'): status = 'Withdrawn'
                if q.get('is_holding'): status = 'Holding'
                row[0].text = status
                row[1].text = q.get('date_tabled', '')
                row[2].text = q.get('dept', '')
                mp_info = q.get('asking_mp', '')
                party = q.get('party', '')
                row[3].text = f'{mp_info} ({party})' if party else mp_info

                qa_cell = row[4]
                qa_para = qa_cell.paragraphs[0]
                qa_para.add_run('Q: ').bold = True
                qa_para.add_run(q.get('question_text', ''))
                if q.get('answer_text'):
                    ans_para = qa_cell.add_paragraph()
                    ans_para.add_run('A: ').bold = True
                    ans_para.add_run(q.get('answer_text', ''))
                if q.get('url'):
                    link_para = qa_cell.add_paragraph()
                    _add_hyperlink(link_para, q['url'], '↗ Parliament.uk')
            doc.add_paragraph()

        else:
            # Group debate speeches under their debate heading
            # Build ordered list of unique debates preserving minister-first order
            seen_debates = {}
            ordered_debates = []
            for r in items:
                key = r.get('listurl', '')[:50] or r.get('debate_title', '')
                if key not in seen_debates:
                    seen_debates[key] = []
                    ordered_debates.append(key)
                seen_debates[key].append(r)

            for debate_key in ordered_debates:
                speeches = seen_debates[debate_key]
                first = speeches[0]

                # Debate header
                debate_hdr = doc.add_paragraph()
                rel = first.get('relevance_level', '')
                rel_label = REL_LABELS.get(rel, '')
                if rel_label:
                    rl_run = debate_hdr.add_run(f'[{rel_label}]  ')
                    rl_run.bold = True
                    rl_run.font.size = Pt(9)
                    colour = {'HIGH RELEVANCE': '166534', 'MODERATE RELEVANCE': '92400E', 'SESSION CONTEXT': '6B7280'}.get(rel_label, '444444')
                    _set_run_colour(rl_run, colour)
                src_run = debate_hdr.add_run(
                    f"{first.get('source_label', '')}  ·  {first.get('hdate', '')}  ·  {first.get('debate_type', '')}"
                )
                src_run.font.size = Pt(9)
                _set_run_colour(src_run, '555555')

                title_line = doc.add_paragraph(first.get('debate_title', ''))
                title_line.runs[0].bold = True
                title_line.runs[0].font.size = Pt(11)

                for r in speeches:
                    spk_p = doc.add_paragraph()
                    spk_run = spk_p.add_run(r.get('speaker_name', ''))
                    spk_run.bold = True
                    if r.get('is_minister') and not r.get('minister_unverified'):
                        role_run = spk_p.add_run(f"  —  {r.get('minister_role') or 'Minister'}")
                        role_run.font.size = Pt(9)
                        _set_run_colour(role_run, '1C3E6E')
                    elif r.get('speaker_party'):
                        party_run = spk_p.add_run(f"  ({r['speaker_party']})")
                        party_run.font.size = Pt(9)
                        _set_run_colour(party_run, '555555')

                    # Full text for ministers (body_export up to 3000 chars); snippet for others
                    body = r.get('body_export') or r.get('body_clean', '')
                    if not r.get('is_minister'):
                        body = body[:600]
                    if body:
                        body_p = doc.add_paragraph(body)
                        body_p.runs[0].font.size = Pt(10)
                        if len(r.get('body_export', r.get('body_clean', ''))) > len(body):
                            body_p.add_run('  […]').italic = True

                    url = r.get('listurl', '')
                    if url:
                        url = _full_url(url)
                        lp = doc.add_paragraph()
                        _add_hyperlink(lp, url, '↗ View on Hansard')
                        if lp.runs:
                            lp.runs[0].font.size = Pt(9)

                doc.add_paragraph('─' * 72)
            doc.add_paragraph()

    # ── Debates — Sources appendix ────────────────────────────────────
    # Collect unique debate titles + links from all non-WQ sections
    all_debate_links = {}
    for sec_type in ['debate', 'oral', 'urgent', 'statement']:
        section = sections_by_type.get(sec_type)
        if not section:
            continue
        for r in section.get('items', []):
            title = r.get('debate_title', '') or ''
            url = r.get('listurl', '') or ''
            url = _full_url(url)
            date = r.get('hdate', '') or ''
            source = r.get('source_label', '') or ''
            key = url or title
            if key and key not in all_debate_links:
                all_debate_links[key] = {'title': title, 'url': url, 'date': date, 'source': source}

    if all_debate_links:
        doc.add_heading('Debates — Sources', 1)
        for entry in all_debate_links.values():
            p = doc.add_paragraph(style='List Bullet')
            label = '  ·  '.join(filter(None, [entry['source'], entry['date'], entry['title']]))
            if entry['url']:
                _add_hyperlink(p, entry['url'], label)
            else:
                p.add_run(label)
        doc.add_paragraph()

    # ── Hansard Citations — per-speech reference table ─────────────────
    citation_rows = []
    for sec_type in ['oral', 'urgent', 'statement', 'debate']:
        section = sections_by_type.get(sec_type)
        if not section:
            continue
        for r in section.get('items', []):
            spk = r.get('speaker_name', '') or ''
            if not spk:
                continue
            url = r.get('listurl', '') or ''
            url = _full_url(url)
            role_or_party = (r.get('minister_role') or r.get('speaker_party') or '')
            citation_rows.append({
                'speaker': spk,
                'role': role_or_party,
                'date': r.get('hdate', ''),
                'house': r.get('source_label', ''),
                'debate': r.get('debate_title', ''),
                'url': url,
            })

    if citation_rows:
        doc.add_heading('Hansard Citations', 1)
        note = doc.add_paragraph(
            'Full source list for all parliamentary contributions included in this brief. '
            'Cross-reference against the AI synthesis above to verify specific statements.')
        note.runs[0].font.size = Pt(9)
        note.runs[0].italic = True
        _set_run_colour(note.runs[0], '555555')

        cite_tbl = doc.add_table(rows=1, cols=5)
        cite_tbl.style = 'Table Grid'
        for i, label in enumerate(['Speaker', 'Role / Party', 'Date', 'House / Type', 'Debate']):
            cell = cite_tbl.rows[0].cells[i]
            cell.text = label
            cell.paragraphs[0].runs[0].bold = True
            _set_cell_bg(cell, '1C3E6E')
            cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            cell.paragraphs[0].runs[0].font.size = Pt(9)

        for cr in citation_rows:
            row = cite_tbl.add_row().cells
            row[0].text = cr['speaker']
            row[1].text = cr['role']
            row[2].text = cr['date']
            row[3].text = cr['house']
            # Debate cell: hyperlink if URL available, plain text if not
            debate_cell = row[4]
            debate_cell.paragraphs[0].clear()
            if cr['url']:
                _add_hyperlink(debate_cell.paragraphs[0], cr['url'],
                               cr['debate'][:80] or '↗ View')
            else:
                debate_cell.text = cr['debate'][:80]
            for cell in row:
                for para in cell.paragraphs:
                    for run in para.runs:
                        run.font.size = Pt(9)
        doc.add_paragraph()

    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)
    safe_topic = re.sub(r'[^\w\s-]', '', topic)[:40].strip()
    filename = f"Research - {safe_topic} - {datetime.now().strftime('%Y%m%d')}.docx"
    return send_file(mem_doc, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


# ==========================================
# DEBATE PREP — Helper functions
# ==========================================

def _prep_resolve_peer(peer_name):
    """Look up a peer's Parliament profile.
    Returns dict with parliament_id, display_name, party, house,
    biography_posts, interests — or None if not found."""
    # Build a list of name variants to try — full name first, then with
    # honorifics stripped (Parliament API searches on the name part, not title)
    _HONORIFICS = re.compile(
        r'^(The\s+)?(Lord|Baroness|Baron|Lady|Viscount|Viscountess|Earl|Countess|Duke|Duchess|Marquess|Prince|Princess)\s+',
        re.IGNORECASE
    )
    name_variants = [peer_name]
    stripped = _HONORIFICS.sub('', peer_name).strip()
    if stripped and stripped != peer_name:
        name_variants.append(stripped)

    try:
        items = []
        for name in name_variants:
            if items:
                break
            resp = requests.get(
                'https://members-api.parliament.uk/api/Members/Search',
                params={'Name': name, 'IsCurrentMember': 'true', 'take': 5},
                timeout=8
            )
            if resp.status_code == 200:
                items = resp.json().get('items', [])
            if not items:
                resp2 = requests.get(
                    'https://members-api.parliament.uk/api/Members/Search',
                    params={'Name': name, 'take': 5},
                    timeout=8
                )
                if resp2.status_code == 200:
                    items = resp2.json().get('items', [])
        if not items:
            return None

        v = items[0]['value']
        parliament_id = v['id']
        house_num = (v.get('latestHouseMembership') or {}).get('house', 1)
        house = 'Lords' if house_num == 2 else 'Commons'
        party = (v.get('latestParty') or {}).get('name', '')
        name = v.get('nameDisplayAs', peer_name)
        thumbnail = v.get('thumbnailUrl', '')

        bio_posts = []
        try:
            bio_resp = requests.get(
                f'https://members-api.parliament.uk/api/Members/{parliament_id}/Biography',
                timeout=8
            )
            if bio_resp.status_code == 200:
                bio = bio_resp.json().get('value', {})
                for category_key, cat_label in [
                    ('governmentPosts', 'Government Post'),
                    ('oppositionPosts', 'Opposition Post'),
                    ('committeeMemberships', 'Committee'),
                    ('representations', 'Career'),
                ]:
                    for post in (bio.get(category_key) or []):
                        bio_posts.append({
                            'category': cat_label,
                            'title': post.get('name', ''),
                            'start': (post.get('startDate') or '')[:10],
                            'end': (post.get('endDate') or '')[:10],
                            'is_current': not post.get('endDate'),
                        })
        except Exception:
            pass

        interests = []
        try:
            int_resp = requests.get(
                f'https://members-api.parliament.uk/api/Members/{parliament_id}/RegisteredInterests',
                timeout=8
            )
            if int_resp.status_code == 200:
                for category in (int_resp.json().get('value') or []):
                    cat_name = category.get('name', '')
                    cat_items = []
                    for item in (category.get('interests') or []):
                        desc = item.get('interest', '') or item.get('description', '')
                        if desc:
                            cat_items.append(desc[:200])
                    if cat_items:
                        interests.append({'category': cat_name, 'items': cat_items})
        except Exception:
            pass

        return {
            'parliament_id': parliament_id,
            'display_name': name,
            'party': party,
            'house': house,
            'thumbnail': thumbnail,
            'biography_posts': bio_posts,
            'interests': interests,
        }
    except Exception:
        return None


def _prep_one_pager(question_text, topic, question_date='', house='lords'):
    """Generate an AI one-pager briefing for an oral question.
    Returns dict with why_now, sector_context, major_criticisms, opposition_position
    or None on failure."""
    if not GEMINI_API_KEY:
        return None

    house_label = 'Commons oral question' if house == 'commons' else 'Lords oral question'
    prompt = (
        f"You are briefing a UK civil servant preparing for a {house_label}.\n\n"
        f"Question: {question_text}\n"
        f"Topic: {topic}\n"
        f"Date: {question_date or 'upcoming'}\n\n"
        "Generate a structured background briefing with these five sections:\n"
        "1. why_now: 2-3 sentences explaining why this question is being asked at this "
        "time — recent events, policy changes, sector pressures, or public debate driving it\n"
        "2. sector_context: 3-4 sentences of factual background on the sector or policy area\n"
        "3. major_criticisms: Array of 3-5 specific criticisms or concerns currently being "
        "raised about this area by campaigners, academics, media or opposition\n"
        "4. opposition_position: 2-3 sentences summarising the likely opposition angle — "
        "what they want the government to commit to, and what they may push back on\n"
        "5. sources: Array of 3-6 specific, real sources that a civil servant could consult — "
        "e.g. named government reports, parliamentary publications, think-tank research, "
        "named media outlets or academic bodies. Use the format 'Organisation/Publication — "
        "Title or description (Year if known)'. Only cite sources that are likely to genuinely "
        "exist; do not invent titles.\n\n"
        'Return as JSON only, no prose, no markdown fences:\n'
        '{"why_now": "...", "sector_context": "...", "major_criticisms": ["...", "..."], '
        '"opposition_position": "...", "sources": ["...", "..."]}'
    )

    if GEMINI_API_KEY:
        try:
            model_path = get_working_model(GEMINI_API_KEY)
            url = (f"https://generativelanguage.googleapis.com/v1beta/"
                   f"{model_path}:generateContent")
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1400}}
            resp = requests.post(url, headers=_gemini_headers(GEMINI_API_KEY), json=body, timeout=30)
            if resp.status_code == 503:
                time.sleep(2)
                resp = requests.post(url, headers=_gemini_headers(GEMINI_API_KEY), json=body, timeout=30)
            if resp.status_code == 200:
                raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
                result = _parse_ai_json(raw)
                if result:
                    return result
            else:
                import logging as _oplog
                _oplog.warning('[_prep_one_pager] Gemini returned %s', resp.status_code)
        except Exception as _ope:
            import logging as _oplog
            _oplog.warning('[_prep_one_pager] Gemini exception: %s', _ope)
    return None


_NEGATIVE_SENTIMENT_WORDS = {
    'crisis', 'failure', 'fail', 'cut', 'cuts', 'closure', 'closures', 'lost',
    'concern', 'problem', 'criticism', 'shortage', 'scandal', 'row', 'backlash',
    'warning', 'threat', 'decline', 'collapse', 'protest', 'underfund', 'underfunded',
    'anger', 'outrage', 'catastrophe', 'catastrophic', 'devastating',
}
_POSITIVE_SENTIMENT_WORDS = {
    'invest', 'investment', 'growth', 'support', 'success', 'improve', 'improvement',
    'boost', 'achieve', 'achievement', 'celebrate', 'award', 'record', 'launch',
    'fund', 'expand', 'expansion', 'progress', 'reform',
}


def _classify_sentiment(title, summary):
    text = (title + ' ' + (summary or '')).lower()
    words = set(re.findall(r'\b\w+\b', text))
    neg_score = len(words & _NEGATIVE_SENTIMENT_WORDS)
    pos_score = len(words & _POSITIVE_SENTIMENT_WORDS)
    if neg_score > pos_score:
        return 'negative'
    if pos_score > neg_score:
        return 'positive'
    return 'neutral'


def _prep_media(topic, start_date='', end_date=''):
    """Search News API for media coverage of the topic.
    Returns list of {title, outlet, published, summary, link, sentiment}."""
    NEWS_API_KEY = os.environ.get('NEWS_API_KEY')
    if not NEWS_API_KEY:
        return []
    try:
        from newsapi import NewsApiClient
        newsapi = NewsApiClient(api_key=NEWS_API_KEY)
        kwargs = {'q': topic, 'language': 'en', 'sort_by': 'relevancy', 'page_size': 20}
        if start_date:
            kwargs['from_param'] = start_date
        if end_date:
            kwargs['to'] = end_date
        resp = newsapi.get_everything(**kwargs)
        results = []
        for art in resp.get('articles', []):
            title = art.get('title', '')
            summary = art.get('description') or art.get('title', '')
            results.append({
                'title': title,
                'link': art.get('url', ''),
                'published': (art.get('publishedAt') or '')[:10],
                'summary': summary[:200],
                'outlet': art.get('source', {}).get('name', ''),
                'sentiment': _classify_sentiment(title, summary),
            })
        return results
    except Exception:
        return []


def _prep_parl_lords(topic, date_range):
    """Lords debates for topic — called directly in the top-level executor."""
    try:
        rows = fetch_hansard_topic(topic, 'lords', date_range, num=50)
        return [r for r in rows if not r.get('_error')]
    except Exception:
        return []


def _prep_parl_commons(topic, date_range):
    """Commons + Westminster Hall debates for topic — called directly in the top-level executor."""
    try:
        rows = []
        rows.extend(fetch_hansard_topic(topic, 'commons', date_range, num=40))
        rows.extend(fetch_hansard_topic(topic, 'westminsterhall', date_range, num=30))
        return [r for r in rows if not r.get('_error')]
    except Exception:
        return []


def _prep_parl_wqs(topic, start_date, end_date):
    """Written questions for topic — called directly in the top-level executor."""
    try:
        wqs, _ = _fetch_topic_wqs(topic, start_date, end_date, [], limit=20)
        return wqs
    except Exception:
        return []


def _prep_parl_statements(topic, date_range):
    """Written ministerial statements for topic — called directly in the top-level executor.
    Reads our own ha_written_statement table; the live Parliament API is only a
    fallback while that table is missing or empty."""
    try:
        rows = fetch_db_wms(topic, date_range, num=20)
        if rows is None:
            rows = fetch_parliament_wms(topic, date_range, dept_id=None, num=20)
        return [r for r in rows if not r.get('_error')]
    except Exception as e:
        import logging
        logging.warning(f"[debate_prep] statements failed: {type(e).__name__}: {e}")
        return []


def _prep_peer_contributions(parliament_id, start_date='', end_date=''):
    """Fetch the peer's own recent speeches and tabled WPQs.
    Returns dict: {speeches, tabled_wqs}."""
    date_range = ''
    if start_date and end_date:
        date_range = f'{start_date.replace("-","")}..{end_date.replace("-","")}'
    elif start_date:
        date_range = f'{start_date.replace("-","")}..{datetime.now().strftime("%Y%m%d")}'

    def _speeches():
        return fetch_hansard_minister_topic(
            parliament_id, '', date_range,
            ['lords', 'commons', 'wms', 'westminsterhall'],
            num=20, is_lord=True
        )

    def _wqs():
        try:
            params = {
                'tabledBy': parliament_id, 'take': 20, 'skip': 0,
                'expandMember': 'false', 'orderBy': 'DateTabledDesc',
            }
            resp = requests.get(PARLIAMENT_WQ_API, params=params, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = []
            for item in data.get('results', []):
                val = item.get('value', {})
                raw_date = (val.get('dateTabled') or '').split('T')[0]
                uin = str(val.get('uin', ''))
                q_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ',
                                val.get('questionText') or '')).strip()
                dept = val.get('answeringBodyName', '')
                results.append({
                    'uin': uin,
                    'question_text': q_text[:300],
                    'dept': dept,
                    'date_tabled': raw_date,
                    'url': (f"https://questions-statements.parliament.uk/written-questions"
                            f"/detail/{raw_date}/{uin}"),
                })
            return results
        except Exception:
            return []

    try:
        speeches = _speeches()
    except Exception:
        speeches = []
    try:
        tabled_wqs = _wqs()
    except Exception:
        tabled_wqs = []

    return {'speeches': speeches, 'tabled_wqs': tabled_wqs}


# ==========================================
# ROUTE 11: DEBATE PREP
# ==========================================
@debate_scanner_bp.route('/debate_prep', methods=['GET', 'POST'])
@limiter.limit("10 per minute; 100 per day", methods=["POST"])
def debate_prep():
    import logging, traceback
    if request.method == 'GET':
        return render_template('debate_prep.html',
            is_post=False, peer_name='', question_date='', question_text='',
            media_start='', media_end='', house='lords', peer_info=None, one_pager=None,
            media_items=[], parl_sections={}, peer_contributions={}, error=None)

    try:
        return _debate_prep_post()
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"[debate_prep POST] UNHANDLED: {e}\n{tb}")
        _h = request.form.get('house', 'lords')
        return render_template('debate_prep.html',
            is_post=True, peer_name=request.form.get('peer_name',''),
            question_date=request.form.get('question_date',''),
            question_text=request.form.get('question_text',''),
            media_start='', media_end='', house=_h, peer_info=None, one_pager=None,
            media_items=[], parl_sections={}, peer_contributions={},
            error='Something went wrong preparing this briefing. Please try again.'), 200


def _debate_prep_post():
    peer_name = request.form.get('peer_name', '').strip()
    question_date = request.form.get('question_date', '').strip()
    question_text = request.form.get('question_text', '').strip()
    media_start = request.form.get('media_start', '').strip()
    media_end = request.form.get('media_end', '').strip()
    house = request.form.get('house', 'lords').strip().lower()
    if house not in ('lords', 'commons'):
        house = 'lords'

    if not peer_name or not question_text:
        return render_template('debate_prep.html',
            is_post=True, peer_name=peer_name, question_date=question_date,
            question_text=question_text, media_start=media_start, media_end=media_end,
            house=house, peer_info=None, one_pager=None, media_items=[], parl_sections={},
            peer_contributions={}, error='Please provide a member name and question text.')

    # Derive topic from question text — use first sentence as search topic
    topic = re.split(r'[.?]', question_text)[0].strip()[:150]

    # Default media range: 60 days before question date
    if not media_start and question_date:
        try:
            from datetime import timedelta
            qd = datetime.strptime(question_date, '%Y-%m-%d')
            media_start = (qd - timedelta(days=60)).strftime('%Y-%m-%d')
            media_end = media_end or question_date
        except Exception:
            pass

    # Parliamentary range: 1 year look-back
    parl_start = ''
    parl_end = question_date or datetime.now().strftime('%Y-%m-%d')
    if question_date:
        try:
            from datetime import timedelta
            qd = datetime.strptime(question_date, '%Y-%m-%d')
            parl_start = (qd - timedelta(days=365)).strftime('%Y-%m-%d')
        except Exception:
            pass

    parl_date_range = format_date_range(parl_start, parl_end)

    peer_info = None
    one_pager = None
    media_items = []
    peer_contributions = {}

    # All tasks submitted to a single flat executor — no nested thread pools
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        f_peer    = executor.submit(copy_current_request_context(_prep_resolve_peer), peer_name)
        f_op      = executor.submit(copy_current_request_context(_prep_one_pager), question_text, topic, question_date, house)
        f_media   = executor.submit(copy_current_request_context(_prep_media), topic, media_start, media_end)
        f_lords   = executor.submit(copy_current_request_context(_prep_parl_lords), topic, parl_date_range)
        f_commons = executor.submit(copy_current_request_context(_prep_parl_commons), topic, parl_date_range)
        f_wqs     = executor.submit(copy_current_request_context(_prep_parl_wqs), topic, parl_start, parl_end)
        f_stmts   = executor.submit(copy_current_request_context(_prep_parl_statements), topic, parl_date_range)

        try:
            peer_info = f_peer.result(timeout=15)
        except Exception:
            peer_info = None
        try:
            one_pager = f_op.result(timeout=35)
        except Exception:
            one_pager = None
        try:
            media_items = f_media.result(timeout=20)
        except Exception:
            media_items = []
        try:
            lords_debates = f_lords.result(timeout=25)
        except Exception:
            lords_debates = []
        try:
            commons_debates = f_commons.result(timeout=25)
        except Exception:
            commons_debates = []
        try:
            parl_wqs = f_wqs.result(timeout=25)
        except Exception:
            parl_wqs = []
        try:
            statements = f_stmts.result(timeout=25)
        except Exception:
            statements = []

    parl_sections = {
        'lords_debates': lords_debates,
        'commons_debates': commons_debates,
        'wqs': parl_wqs,
        'statements': statements,
    }

    # Peer contributions requires parliament_id — run sequentially after peer resolves
    if peer_info and peer_info.get('parliament_id'):
        try:
            peer_contributions = _prep_peer_contributions(
                peer_info['parliament_id'], parl_start, parl_end
            )
        except Exception:
            peer_contributions = {}

    return render_template('debate_prep.html',
        is_post=True,
        peer_name=peer_name,
        question_date=question_date,
        question_text=question_text,
        media_start=media_start,
        media_end=media_end,
        house=house,
        peer_info=peer_info,
        one_pager=one_pager,
        media_items=media_items or [],
        parl_sections=parl_sections or {},
        peer_contributions=peer_contributions or {},
        error=None)


# ==========================================
# ROUTE 12: DOWNLOAD DEBATE PREP BRIEF
# ==========================================
@debate_scanner_bp.route('/download_debate_prep_brief', methods=['POST'])
def download_debate_prep_brief():
    if not Document:
        return "Word library missing.", 500

    from docx.shared import Pt, RGBColor, Inches
    from docx.oxml.ns import qn as _qn
    from docx.oxml import OxmlElement as _OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    def _parse_field(field, default=None):
        val = request.form.get(field, '')
        if not val:
            return default if default is not None else {}
        try:
            return json.loads(val)
        except Exception:
            return default if default is not None else {}

    peer_name = request.form.get('peer_name', 'Unknown Peer')
    question_date = request.form.get('question_date', '')
    question_text = request.form.get('question_text', '')
    house = request.form.get('house', 'lords')
    member_label = 'MP' if house == 'commons' else 'Peer'
    peer_info = _parse_field('peer_info_json', {})
    one_pager = _parse_field('one_pager_json', {})
    media_items = _parse_field('media_json', [])
    parl_sections = _parse_field('parl_json', {})
    peer_contributions = _parse_field('peer_contrib_json', {})

    doc = Document()

    # Page margins
    sec = doc.sections[0]
    sec.top_margin = Inches(0.9)
    sec.bottom_margin = Inches(0.9)
    sec.left_margin = Inches(1.1)
    sec.right_margin = Inches(1.1)

    def _set_cell_bg(cell, hex_colour):
        tc = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd = _OxmlElement('w:shd')
        shd.set(_qn('w:val'), 'clear')
        shd.set(_qn('w:color'), 'auto')
        shd.set(_qn('w:fill'), hex_colour)
        tcPr.append(shd)

    def _clr(run, hex_str):
        r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
        run.font.color.rgb = RGBColor(r, g, b)

    # Cover block
    title_p = doc.add_heading('Debate Preparation Briefing', 0)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for line in filter(None, [
        f'{member_label}: {peer_name}',
        f'Question date: {question_date}' if question_date else None,
        f'Generated: {datetime.now().strftime("%d %b %Y %H:%M")}',
    ]):
        p = doc.add_paragraph(line)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.runs[0].italic = True
        p.runs[0].font.size = Pt(11)

    if question_text:
        q_p = doc.add_paragraph()
        q_p.add_run('Question: ').bold = True
        q_p.add_run(question_text)
        q_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    notice = doc.add_paragraph(
        '⚠ AI-generated — verify against source links before use in official documents.')
    notice.alignment = WD_ALIGN_PARAGRAPH.CENTER
    notice.runs[0].font.size = Pt(9)
    notice.runs[0].italic = True
    _clr(notice.runs[0], '856404')
    doc.add_paragraph()

    # ── Section 1: One Pager ──────────────────────────────────────
    if one_pager:
        doc.add_heading('One Pager — Background Briefing', 1)
        for sub_key, sub_title in [
            ('why_now', 'Why Now'),
            ('sector_context', 'Sector Context'),
            ('opposition_position', 'Opposition Position'),
        ]:
            if one_pager.get(sub_key):
                doc.add_heading(sub_title, 2)
                doc.add_paragraph(one_pager[sub_key])
        if one_pager.get('major_criticisms'):
            doc.add_heading('Major Criticisms', 2)
            for crit in one_pager['major_criticisms']:
                doc.add_paragraph(crit, style='List Bullet')
        if one_pager.get('sources'):
            doc.add_heading('Suggested Sources', 2)
            for src in one_pager['sources']:
                doc.add_paragraph(src, style='List Bullet')
            p = doc.add_paragraph('AI-suggested — verify existence before citing')
            p.runs[0].italic = True
            p.runs[0].font.size = Pt(9)
            _clr(p.runs[0], '888888')
        doc.add_paragraph()

    # ── Section 2: Media ──────────────────────────────────────────
    if media_items:
        doc.add_heading(f'Media Coverage ({len(media_items)} articles)', 1)
        tbl = doc.add_table(rows=1, cols=4)
        tbl.style = 'Table Grid'
        hdr = tbl.rows[0].cells
        for i, lbl in enumerate(['Date', 'Outlet', 'Sentiment', 'Headline & Summary']):
            hdr[i].text = lbl
            hdr[i].paragraphs[0].runs[0].bold = True
            _set_cell_bg(hdr[i], '1C3E6E')
            hdr[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        sent_colours = {'positive': 'D4EDDA', 'negative': 'F8D7DA', 'neutral': 'F8F9FA'}
        for art in media_items:
            row = tbl.add_row().cells
            row[0].text = art.get('published', '')
            row[1].text = art.get('outlet', '')
            sentiment = art.get('sentiment', 'neutral')
            row[2].text = sentiment.capitalize()
            _set_cell_bg(row[2], sent_colours.get(sentiment, 'F8F9FA'))
            cell = row[3]
            p = cell.paragraphs[0]
            link = art.get('link', '')
            if link:
                _add_hyperlink(p, link, art.get('title', ''))
            else:
                p.add_run(art.get('title', ''))
            summary = art.get('summary', '')
            if summary and summary != art.get('title', ''):
                sp = cell.add_paragraph(summary)
                sp.runs[0].font.size = Pt(9)
        doc.add_paragraph()

    # ── Section 3: Parliamentary Record ───────────────────────────
    parl_label_map = [
        ('lords_debates', 'Lords Debates'),
        ('commons_debates', 'Commons Debates'),
        ('wqs', 'Written Questions'),
        ('statements', 'Oral Statements'),
    ]
    if any(parl_sections.get(k) for k, _ in parl_label_map):
        doc.add_heading('Parliamentary Record', 1)
        for key, label in parl_label_map:
            rows = parl_sections.get(key, [])
            if not rows:
                continue
            doc.add_heading(f'{label} ({len(rows)})', 2)
            if key == 'wqs':
                for q in rows[:15]:
                    p = doc.add_paragraph()
                    meta_run = p.add_run(
                        f"{q.get('date_tabled', '')}  ·  {q.get('dept', '')}  — ")
                    meta_run.font.size = Pt(9)
                    _clr(meta_run, '555555')
                    p.add_run(q.get('question_text', '')[:200])
                    if q.get('url'):
                        lp = doc.add_paragraph()
                        _add_hyperlink(lp, q['url'], '↗ Parliament.uk')
                        if lp.runs:
                            lp.runs[0].font.size = Pt(9)
            else:
                seen_d = set()
                for r in rows[:20]:
                    url = r.get('listurl', '')
                    url = _full_url(url)
                    key_d = url or r.get('debate_title', '')
                    if key_d in seen_d:
                        continue
                    seen_d.add(key_d)
                    hp = doc.add_paragraph()
                    meta_run = hp.add_run(
                        f"{r.get('hdate', '')}  ·  {r.get('source_label', '')}  — ")
                    meta_run.font.size = Pt(9)
                    _clr(meta_run, '555555')
                    hp.add_run(r.get('debate_title', '')).bold = True
                    sp = doc.add_paragraph()
                    sp.add_run(f"{r.get('speaker_name', '')}:  ").bold = True
                    body_run = sp.add_run(r.get('body_clean', '')[:250])
                    body_run.font.size = Pt(10)
                    if url:
                        lp = doc.add_paragraph()
                        _add_hyperlink(lp, url, '↗ View on Hansard')
                        if lp.runs:
                            lp.runs[0].font.size = Pt(9)
                    doc.add_paragraph('─' * 60)
        doc.add_paragraph()

    # ── Section 4: Member Profile ─────────────────────────────────
    doc.add_heading(f'{member_label} Profile — {peer_name}', 1)
    if peer_info:
        meta = '  ·  '.join(filter(None, [peer_info.get('party', ''),
                                           peer_info.get('house', '')]))
        if meta:
            doc.add_paragraph(meta).runs[0].font.size = Pt(10)

        bio_posts = peer_info.get('biography_posts', [])
        if bio_posts:
            doc.add_heading('Career & Posts', 2)
            current = [p for p in bio_posts if p.get('is_current')]
            past = [p for p in bio_posts if not p.get('is_current')]
            for post in current[:5]:
                p = doc.add_paragraph(style='List Bullet')
                run = p.add_run(f"[Current] {post['category']}: {post['title']}")
                run.bold = True
                _clr(run, '1C3E6E')
            for post in past[:12]:
                p = doc.add_paragraph(style='List Bullet')
                yr = ''
                if post.get('start'):
                    yr = f" ({post['start'][:4]}"
                    yr += f"–{post['end'][:4]})" if post.get('end') else '–present)'
                p.add_run(f"{post['category']}: {post['title']}{yr}")

        interests = peer_info.get('interests', [])
        if interests:
            doc.add_heading('Register of Interests', 2)
            for cat in interests[:6]:
                doc.add_paragraph(cat['category']).runs[0].bold = True
                for item in cat['items'][:5]:
                    p = doc.add_paragraph(item, style='List Bullet')
                    p.runs[0].font.size = Pt(10)
    else:
        doc.add_paragraph(f'{member_label} not found in Parliament Members API.')

    speeches = (peer_contributions or {}).get('speeches', [])
    if speeches:
        doc.add_heading('Recent Spoken Contributions', 2)
        seen_s = set()
        for r in speeches[:12]:
            url = r.get('listurl', '')
            url = _full_url(url)
            key_s = url or r.get('debate_title', '')
            if key_s in seen_s:
                continue
            seen_s.add(key_s)
            p = doc.add_paragraph(style='List Bullet')
            p.add_run(f"{r.get('hdate', '')}  —  {r.get('debate_title', '')}")
            if url:
                lp = doc.add_paragraph()
                _add_hyperlink(lp, url, '↗ Hansard')
                if lp.runs:
                    lp.runs[0].font.size = Pt(9)

    tabled_wqs = (peer_contributions or {}).get('tabled_wqs', [])
    if tabled_wqs:
        doc.add_heading('Recent Written Questions Tabled', 2)
        for q in tabled_wqs[:12]:
            p = doc.add_paragraph(style='List Bullet')
            p.add_run(
                f"{q.get('date_tabled', '')}  ·  {q.get('dept', '')}  —  "
                f"{q.get('question_text', '')[:150]}")
            if q.get('url'):
                lp = doc.add_paragraph()
                _add_hyperlink(lp, q['url'], '↗ Parliament.uk')
                if lp.runs:
                    lp.runs[0].font.size = Pt(9)

    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)
    safe_name = re.sub(r'[^\w\s-]', '', peer_name)[:30].strip()
    filename = f"Debate Prep - {safe_name} - {datetime.now().strftime('%Y%m%d')}.docx"
    return send_file(mem_doc, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')