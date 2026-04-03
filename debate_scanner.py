import requests, os, json, re, io, concurrent.futures, time
from flask import Blueprint, render_template, request, send_file
from flask_login import current_user
from datetime import datetime
from cache_models import CachedTranscript

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

TWFY_API_KEY = os.environ.get("TWFY_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TWFY_API_URL = "https://www.theyworkforyou.com/api/getDebates"
TWFY_WRANS_URL = "https://www.theyworkforyou.com/api/getWrans"
TWFY_WMS_URL = "https://www.theyworkforyou.com/api/getWMS"
MINISTER_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'minister_cache.json')
MINISTER_CACHE_TTL = 7 * 24 * 3600  # 7 days

DEPARTMENTS_TWFY = [
    "All Departments", "Department for Education", "Department of Health and Social Care",
    "HM Treasury", "Home Office", "Ministry of Defence", "Ministry of Justice",
    "Department for Science, Innovation and Technology", "Cabinet Office"
]

DEPT_KEYWORDS = {
    "Department for Education": '("Education" OR "Schools" OR "Skills" OR "Childcare" OR "Universities" OR "SEND")',
    "Department of Health and Social Care": '("Health" OR "NHS" OR "Social Care")',
    "HM Treasury": '("Treasury" OR "Tax" OR "Economy" OR "Spending")',
    "Home Office": '("Home Office" OR "Police" OR "Immigration")',
    "Ministry of Defence": '("Defence" OR "Military" OR "Armed Forces")',
    "Ministry of Justice": '("Justice" OR "Prisons" OR "Courts")',
    "Department for Science, Innovation and Technology": '("Science" OR "Technology" OR "Innovation")',
    "Cabinet Office": '("Cabinet Office" OR "Civil Service")'
}

PARLIAMENT_WQ_API = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
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

def get_working_model(api_key):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            available = [m['name'] for m in resp.json().get('models', []) 
                         if 'generateContent' in m.get('supportedGenerationMethods', [])]
            for pref in ['models/gemini-1.5-flash', 'models/gemini-1.5-pro']:
                if pref in available: return pref
            if available: return available[0]
    except: pass
    return "models/gemini-pro"

def get_twfy_date_range(start_str, end_str):
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
    if 'urgent question' in t: return '❗ Urgent Question'
    if 'oral answers' in t or 'question time' in t: return '🗣️ Oral Question'
    if 'prime minister' in t and 'question' in t: return '🗣️ Oral Question'
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

def get_source_label(source):
    return {'commons': 'Commons', 'westminsterhall': 'Westminster Hall',
            'lords': 'Lords', 'wrans': 'Written Answer',
            'wms': 'Ministerial Statement'}.get(source, source.title())

def fetch_twfy_topic(search, source_type, date_range, num=150):
    """Fetch rows from TWFY for a topic search. Returns normalised list or [] on failure."""
    try:
        if source_type == 'wrans':
            api_url = TWFY_WRANS_URL
        elif source_type == 'wms':
            api_url = TWFY_WMS_URL
        else:
            api_url = TWFY_API_URL
        search_with_date = f"{search} {date_range}".strip() if date_range else search
        params = {'key': TWFY_API_KEY, 'search': search_with_date, 'order': 'r', 'num': num, 'output': 'json'}
        if source_type not in ('wrans', 'wms'):
            params['type'] = source_type
        resp = requests.get(api_url, params=params, timeout=15)
        if resp.status_code != 200:
            return []
        rows = resp.json().get('rows', [])
        results = []
        for r in rows:
            body_raw = r.get('body', '')
            body_text = clean_body_text(body_raw)
            debate_title = re.sub(r'<[^>]+>', '', r.get('parent', {}).get('body', '') or '')
            if source_type in ('wrans', 'wms'):
                debate_title = re.sub(r'<[^>]+>', '', body_raw)[:80]
            if source_type == 'wrans':
                dtype = 'Written Answer'
            elif source_type == 'wms':
                dtype = 'Ministerial Statement'
            else:
                dtype = get_debate_type(debate_title, source=source_type)
            results.append({
                'listurl': r.get('listurl', ''),
                'body_clean': body_text[:500],
                'body_export': body_text[:3000],
                'body_word_count': len(body_text.split()),
                'speaker_name': (r.get('speaker') or {}).get('name', 'Unknown'),
                'speaker_party': (r.get('speaker') or {}).get('party', ''),
                'hdate': r.get('hdate', ''),
                'debate_title': debate_title,
                'source': source_type,
                'source_label': get_source_label(source_type),
                'relevance': r.get('relevance', 0),
                'debate_type': dtype,
            })
        return results
    except Exception:
        return []

def deduplicate_by_listurl(rows):
    seen = set()
    out = []
    for r in rows:
        key = r.get('listurl', '')
        if key and key not in seen:
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

    quotes = briefing_dict.get('key_quotes', [])
    if quotes:
        lines.append("## 6. KEY QUOTES")
        for q in quotes:
            lines.append(f"- \"{q.get('quote', '')}\" — {q.get('speaker', '')} ({q.get('date', '')}, {q.get('source', '')})")
        lines.append("")

    lines.append(f"## 7. NEXT STEPS\n{briefing_dict.get('next_steps', '')}\n")
    lines.append(f"## 8. COVERAGE NOTE\n{briefing_dict.get('coverage_note', '')}\n")
    return "\n".join(lines)

def expand_search_query(topic, api_key):
    """Use Gemini to expand a topic into related parliamentary search terms."""
    try:
        model_path = get_working_model(api_key)
        ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={api_key}"
        prompt = (
            f"You are a UK Parliamentary researcher. A user wants to search Hansard for: \"{topic}\"\n"
            "Give up to 6 short additional phrases that cover the same policy area "
            "using different vocabulary MPs and Lords commonly use in Parliament. Include:\n"
            "- Any common acronyms (e.g. 'LLE', 'SEND', 'HTQ')\n"
            "- Previous or alternative names for the policy\n"
            "- Related legislation names\n"
            "- Synonymous terms used in debate\n"
            "Return ONLY a JSON array of strings, no explanation, no markdown. "
            f"Example for 'student loan repayments': [\"repayment threshold\", \"graduate debt\", \"loan write-off\", \"income contingent\", \"Plan 2 loans\"]"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"responseMimeType": "application/json"}}
        resp = requests.post(ai_url, json=payload, timeout=15)
        if resp.status_code == 200:
            raw = resp.json()['candidates'][0]['content']['parts'][0]['text']
            terms = json.loads(raw.strip())
            if isinstance(terms, list) and terms:
                all_terms = [topic] + [str(t) for t in terms[:6]]
                return '(' + ' OR '.join(f'"{t}"' for t in all_terms) + ')'
    except Exception:
        pass
    return f'"{topic}"'


_SOURCE_GID_PREFIX = {
    'commons':        'uk.org.publicwhip/debate/',
    'westminsterhall':'uk.org.publicwhip/westminhall/',
    'lords':          'uk.org.publicwhip/lords/',
    'wms':            'uk.org.publicwhip/wms/',
}

def _listurl_to_parent_gid(listurl, source):
    """Convert a TWFY listurl to the parent debate-section GID (speech-index replaced with .0).
    e.g. /debates/?id=2026-01-15.123.4 + commons → uk.org.publicwhip/debate/2026-01-15.123.0
    Returns None if listurl cannot be parsed or source is not expandable."""
    if not listurl or source not in _SOURCE_GID_PREFIX:
        return None
    try:
        m = re.search(r'\?id=([^&]+)', listurl)
        if not m:
            return None
        id_val = m.group(1)                   # e.g. "2026-01-15.123.4"
        section = id_val.rsplit('.', 1)[0]     # e.g. "2026-01-15.123"
        return f"{_SOURCE_GID_PREFIX[source]}{section}.0"
    except Exception:
        return None


def fetch_full_debate_session(parent_gid, source):
    """Fetch ALL speeches from a TWFY debate section via its parent GID.
    Returns normalised speech list (same schema as fetch_twfy_topic). relevance=0."""
    if not TWFY_API_KEY:
        return []
    try:
        api_url = TWFY_WMS_URL if source == 'wms' else TWFY_API_URL
        resp = requests.get(api_url,
                            params={'key': TWFY_API_KEY, 'gid': parent_gid, 'output': 'json'},
                            timeout=10)
        if resp.status_code != 200:
            return []
        rows = resp.json().get('rows', [])
        results = []
        for r in rows:
            body_raw = r.get('body', '')
            body_text = clean_body_text(body_raw)
            debate_title = re.sub(r'<[^>]+>', '', r.get('parent', {}).get('body', '') or '')
            results.append({
                'listurl': r.get('listurl', ''),
                'body_clean': body_text[:500],
                'body_export': body_text[:3000],
                'body_word_count': len(body_text.split()),
                'speaker_name': (r.get('speaker') or {}).get('name', 'Unknown'),
                'speaker_party': (r.get('speaker') or {}).get('party', ''),
                'hdate': r.get('hdate', ''),
                'debate_title': debate_title,
                'source': source,
                'source_label': get_source_label(source),
                'relevance': 0,
                'debate_type': get_debate_type(debate_title, source=source),
                'from_session_fetch': True,
            })
        return results
    except Exception:
        return []


def fetch_all_debate_sessions(matched_rows, max_debates=15):
    """For each unique debate in matched_rows, fetch all speeches via TWFY GID lookup.
    This guarantees ministers appear even when their responses don't contain search keywords.
    Skips wrans (written answers have no multi-speaker session to expand)."""
    seen_gids = set()
    gid_source_pairs = []
    for r in matched_rows:
        source = r.get('source', '')
        if source == 'wrans':
            continue
        gid = _listurl_to_parent_gid(r.get('listurl', ''), source)
        if gid and gid not in seen_gids:
            seen_gids.add(gid)
            gid_source_pairs.append((gid, source))
        if len(gid_source_pairs) >= max_debates:
            break
    if not gid_source_pairs:
        return []
    extra = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_full_debate_session, gid, src): (gid, src)
                for gid, src in gid_source_pairs}
        for f in concurrent.futures.as_completed(futs):
            try:
                extra.extend(f.result())
            except Exception:
                pass
    return extra


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
            'has_minister': any(r.get('is_minister') for r in v),
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

    if source == 'wms':
        return 'statement'
    if 'urgent question' in title:
        return 'urgent'
    if 'oral answers' in title or 'question time' in title:
        return 'oral'
    if 'prime minister' in title and 'question' in title:
        return 'oral'
    # Word-count heuristic: if the longest speech in the group is under 300 words,
    # all speeches are short — consistent with an Oral PQ session (including follow-ups)
    if source in ('commons', 'lords') and speeches:
        max_words = max((r.get('body_word_count', 0) for r in speeches), default=0)
        if 0 < max_words < 300:
            return 'oral'
    return 'debate'


def _fetch_topic_wqs(topic, start_date, end_date, selected_depts, limit=400):
    """Fetch WQs matching topic from Parliament API. Returns (list_of_dicts, total_count)."""
    try:
        params = {'searchTerm': f'"{topic}"', 'take': limit, 'skip': 0}
        if start_date:
            params['tabledWhenFrom'] = start_date
        if end_date:
            params['tabledWhenTo'] = end_date
        dept_ids = [PARLIAMENT_DEPT_IDS[d] for d in selected_depts if d in PARLIAMENT_DEPT_IDS]
        if dept_ids:
            params['answeringBodies'] = dept_ids
        resp = requests.get(PARLIAMENT_WQ_API, params=params, timeout=20)
        if resp.status_code != 200:
            return [], 0
        data = resp.json()
        total = data.get('totalResults', 0)
        results = []
        for item in data.get('results', []):
            val = item.get('value', {})
            raw_date = (val.get('dateTabled') or '').split('T')[0]
            date_ans = (val.get('dateAnswered') or '').split('T')[0]
            is_answered = bool(val.get('answerText') or val.get('dateAnswered'))
            is_withdrawn = bool(val.get('isWithdrawn'))
            is_holding = (is_answered and not is_withdrawn and
                          'i will write' in (val.get('answerText') or '').lower()[:200])
            uin = str(val.get('uin', ''))
            answer_raw = val.get('answerText') or ''
            answer_clean = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', answer_raw)).strip()
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
                'dept': val.get('answeringBodyName', ''),
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
        return results, total
    except Exception:
        return [], 0


def _display_name(title):
    """Strip honorifics/post-nominals from a GOV.UK title for clean display.
    Two passes to handle 'The Rt Hon Baroness Smith of Malvern' → 'Smith of Malvern'."""
    name = title.strip()
    prefixes = ['The Rt Hon ', 'The Right Hon ', 'Rt Hon ', 'Right Hon ',
                'The Baroness ', 'Baroness ', 'The Lord ', 'Lord ',
                'Dame ', 'Sir ', 'Dr ', 'Mr ', 'Mrs ', 'Ms ', 'Miss ']
    for _ in range(2):
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
    name = re.sub(r'\s+(MP|OBE|CBE|MBE|PC|QC|KC|DBE|KBE)(\b.*)?$', '', name)
    return name.strip()


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
    """Fetch ALL current government ministers from GOV.UK content API.
    Returns dict with 'by_norm' {normalised_name: role} keyed by _normalise_name().
    File-cached for 24 hours. Includes junior ministers, not just Cabinet.
    Falls back to Parliament GovernmentPosts if GOV.UK is unavailable."""
    cache = {}
    try:
        if os.path.exists(MINISTER_CACHE_FILE):
            with open(MINISTER_CACHE_FILE) as f:
                cache = json.load(f)
        if (cache.get('_ts') and time.time() - cache['_ts'] < MINISTER_CACHE_TTL
                and cache.get('_source') == 'govuk'
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

    data = {'_ts': time.time(), '_source': 'govuk', 'by_norm': by_norm, 'by_id': by_id, 'by_dept': by_dept}
    try:
        with open(MINISTER_CACHE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

    return data


def _normalise_name(name):
    """Strip honorifics, titles, and post-nominal letters for name matching.
    Works on both TWFY speaker names and GOV.UK person titles.
    Applies prefix stripping in two passes to handle 'The Rt Hon Baroness ...'."""
    n = name.lower().strip()
    # Strip trailing post-nominals: MP, OBE, CBE, MBE, PC, QC, KC, etc.
    n = re.sub(r'\s+(obe|cbe|mbe|pc|qc|kc|dbe|kbe|mp|obe mp|cbe mp|mbe mp)\s*$', '', n)
    # Two passes of prefix stripping handles 'the rt hon baroness' → 'baroness' → ''
    prefixes = ['the rt hon ', 'the right hon ', 'rt hon ', 'right hon ',
                'the baroness ', 'baroness ', 'the lord ', 'lord ',
                'dame ', 'sir ', 'dr ', 'mr ', 'mrs ', 'ms ', 'miss ']
    for _ in range(2):
        for prefix in prefixes:
            if n.startswith(prefix):
                n = n[len(prefix):]
                break
    return n.strip()


def verify_government_speaker(name):
    """Confirm a speaker holds a current government post.
    Returns dict with confirmed (bool) and role (str).

    Three-tier approach:
    1. Normalised name match against GOV.UK minister cache (covers all ministers)
    2. Parliament Members Search → check each result ID against Cabinet id cache
    3. Parliament Members Biography → governmentPosts with no endDate (gets precise role)
    """
    minister_data = get_minister_list()
    by_norm = minister_data.get('by_norm', {})
    by_id = minister_data.get('by_id', {})

    # 1. Normalised match against GOV.UK full minister list
    norm_name = _normalise_name(name)
    if norm_name and norm_name in by_norm:
        # Role from cache is generic ('Minister (DfE)') — fetch precise role via Biography
        cached_role = by_norm[norm_name]
        return {'confirmed': True, 'role': cached_role}

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
                    return {'confirmed': True, 'role': by_id[member_id]}

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
                        return {'confirmed': True, 'role': current_posts[0].get('name', '')}
    except Exception:
        pass

    return {'confirmed': False, 'role': ''}


def lookup_twfy_person(name):
    """Look up TWFY person_id by name. Checks MPs first, then Lords.
    Tries multiple name variants to handle titles like 'Baroness Smith of Malvern'.
    Returns (person_id, matched_name, is_lord) or (None, None, False)."""
    # Build variants: original → stripped display → normalised title-case
    variants = [name]
    display = _display_name(name)
    if display and display != name:
        variants.append(display)
    norm_title = _normalise_name(name).title()
    if norm_title and norm_title not in variants:
        variants.append(norm_title)

    # If the name has no Lords title prefix, also try with common prefixes.
    # This handles "Smith of Malvern" → "Baroness Smith of Malvern" for TWFY getLords.
    lords_prefixes = ['Baroness ', 'Lord ', 'Baron ']
    has_lords_prefix = any(name.lower().startswith(p.lower()) for p in lords_prefixes + ['The '])
    if not has_lords_prefix:
        for prefix in lords_prefixes:
            candidate = prefix + name
            if candidate not in variants:
                variants.append(candidate)

    for variant in variants:
        for endpoint, is_lord in [('getMPs', False), ('getLords', True)]:
            try:
                resp = requests.get(
                    'https://www.theyworkforyou.com/api/' + endpoint,
                    params={'key': TWFY_API_KEY, 'search': variant, 'output': 'json'},
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return data[0].get('person_id'), data[0].get('name'), is_lord
            except Exception:
                pass
    return None, None, False


def fetch_twfy_minister_topic(person_id, topic, date_range, sources, num=50):
    """Fetch speeches for a specific TWFY person_id, filtered by topic + date.
    Returns rows in the same normalised schema as fetch_twfy_topic() so they
    can be merged and deduped with keyword-search results."""
    rows = []
    for source in sources:
        api_url = TWFY_WMS_URL if source == 'wms' else TWFY_API_URL
        params = {
            'key': TWFY_API_KEY, 'person': str(person_id),
            'order': 'd', 'num': num, 'output': 'json'
        }
        if topic:
            params['search'] = f"{topic} {date_range}".strip() if date_range else topic
        if source != 'wms':
            params['type'] = source
        try:
            resp = requests.get(api_url, params=params, timeout=15)
            if resp.status_code != 200:
                continue
            for r in resp.json().get('rows', []):
                body_raw = r.get('body', '')
                body_text = clean_body_text(body_raw)
                debate_title = re.sub(r'<[^>]+>', '', r.get('parent', {}).get('body', '') or '')
                if source == 'wms':
                    debate_title = re.sub(r'<[^>]+>', '', body_raw)[:80]
                dtype = get_debate_type(debate_title, source=source)
                rows.append({
                    'listurl': r.get('listurl', ''),
                    'body_clean': body_text[:500],
                    'body_export': body_text[:3000],
                    'body_word_count': len(body_text.split()),
                    'speaker_name': (r.get('speaker') or {}).get('name', 'Unknown'),
                    'speaker_party': (r.get('speaker') or {}).get('party', ''),
                    'hdate': r.get('hdate', ''),
                    'debate_title': debate_title,
                    'source': source,
                    'source_label': get_source_label(source),
                    'relevance': r.get('relevance', 0),
                    'debate_type': dtype,
                })
        except Exception:
            pass
    return rows


def get_dept_minister_twfy_ids(dept_name, minister_data):
    """Resolve all ministers for a department to TWFY person IDs.
    Returns list of {person_id, name, role} dicts. Skips ministers whose
    TWFY ID cannot be resolved."""
    ministers = minister_data.get('by_dept', {}).get(dept_name, [])
    results = []
    for m in ministers:
        display = m.get('display_name') or m.get('name', '')
        if not display:
            continue
        person_id, matched_name, is_lord = lookup_twfy_person(display)
        if person_id:
            results.append({'person_id': person_id, 'name': display, 'role': m.get('role', '')})
    return results


def fetch_minister_debates(person_id, topic, date_range, house_filter='all'):
    """Fetch debates where a minister spoke, deduped by unique debate section."""
    sources = []
    if house_filter in ('all', 'commons'):
        sources.extend(['commons', 'westminsterhall'])
    if house_filter in ('all', 'lords'):
        sources.append('lords')

    all_rows = []
    for source in sources:
        try:
            params = {
                'key': TWFY_API_KEY, 'person': person_id,
                'type': source, 'order': 'd', 'num': 100, 'output': 'json'
            }
            query = f"{topic} {date_range}".strip() if topic else date_range.strip()
            if query:
                params['search'] = query
            resp = requests.get(TWFY_API_URL, params=params, timeout=15)
            if resp.status_code == 200:
                all_rows.extend(resp.json().get('rows', []))
        except Exception:
            pass

    unique_debates = {}
    for r in all_rows:
        parent_body = re.sub(r'<[^>]+>', '', (r.get('parent') or {}).get('body', '') or '')
        date_val = r.get('hdate', '')
        key = f"{parent_body}_{date_val}"
        url = 'https://www.theyworkforyou.com' + r.get('listurl', '')
        if key not in unique_debates:
            unique_debates[key] = {
                'title': parent_body, 'date': date_val, 'url': url,
                'type': get_debate_type(parent_body), 'contributions': 1
            }
        else:
            unique_debates[key]['contributions'] += 1

    result = list(unique_debates.values())
    result.sort(key=lambda x: x['date'], reverse=True)
    return result


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


# ==========================================
# ROUTE 1: SCAN AND GROUP BY THEME (STEP 1)
# ==========================================
@debate_scanner_bp.route('/debates', methods=['GET', 'POST'])
def scan_debates():
    grouped_debates = {}
    error_message = None

    start_date = ""
    end_date = ""
    selected_dept = "All Departments"
    selected_house = "all"
    content_type = "exclude_bills"

    # Handle stakeholder tab GET (search form submits here via GET)
    if request.method == 'GET' and request.args.get('mode') == 'stakeholder':
        return render_template('debate_scanner.html',
                               mode='stakeholder',
                               stakeholder_topic=request.args.get('stakeholder_topic', ''),
                               grouped_debates={}, error_message=None,
                               start_date='', end_date='',
                               departments=DEPARTMENTS_TWFY, selected_dept='All Departments',
                               selected_house='all', content_type='exclude_bills')

    if request.method == 'POST':
        action = request.form.get('action', 'search')
        
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        selected_dept = request.form.get('department', 'All Departments').strip()
        selected_house = request.form.get('house', 'all')
        content_type = request.form.get('content_type', 'exclude_bills')

        if action == 'search':
            try:
                date_query = get_twfy_date_range(start_date, end_date)
                
                search_term = DEPT_KEYWORDS.get(selected_dept, '("Oral questions" OR "Debate")')
                if selected_house == 'all':
                    houses_to_search = ['commons', 'westminsterhall', 'lords']
                elif selected_house == 'commons':
                    houses_to_search = ['commons', 'westminsterhall']
                else:
                    houses_to_search = [selected_house]
                all_rows = []

                for h in houses_to_search:
                    query = f"{search_term} {date_query}".strip()

                    params = {
                        'key': TWFY_API_KEY,
                        'search': query,
                        'type': h,
                        'output': 'json',
                        'num': 1000 
                    }
                    
                    resp = requests.get(TWFY_API_URL, params=params, timeout=15)
                    if resp.status_code == 200:
                        all_rows.extend(resp.json().get('rows', []))

                if all_rows:
                    unique_debates = {}
                    
                    for r in all_rows:
                        raw_title = r.get('parent', {}).get('body', 'General Debate/Question')
                        title = re.sub(r'<[^>]+>', '', raw_title)
                        date_val = r.get('hdate', '')
                        
                        url = "https://www.theyworkforyou.com" + r.get('listurl', '')
                        speaker = r.get('speaker', {}).get('name', 'Unknown')
                        
                        if start_date and date_val < start_date: continue
                        if end_date and date_val > end_date: continue
                        
                        is_bill_or_amendment = bool(re.search(r'\b(bill|amendment)\b', title, re.IGNORECASE))
                        if content_type == 'exclude_bills' and is_bill_or_amendment: continue
                        if content_type == 'only_bills' and not is_bill_or_amendment: continue

                        key = f"{title}_{date_val}"
                        if key not in unique_debates:
                            unique_debates[key] = {
                                'id': key,
                                'title': title,
                                'date': date_val,
                                'minister': speaker,
                                'contributions': 1,
                                'url': url,
                                'type': get_debate_type(title)
                            }
                        else:
                            unique_debates[key]['contributions'] += 1
                            
                    debate_list = list(unique_debates.values())
                    debate_list.sort(key=lambda x: x['date'], reverse=True)
                    
                    if debate_list and GEMINI_API_KEY:
                        try:
                            MAX_AI_ITEMS = 120
                            process_list = debate_list[:MAX_AI_ITEMS]
                            leftover_list = debate_list[MAX_AI_ITEMS:]

                            titles_only = [{"id": d['id'], "title": d['title'], "minister": d['minister']} for d in process_list]
                            
                            prompt = (
                                f"You are an expert UK political analyst. The user only wants debates related to the '{selected_dept}'. "
                                "1. Group the genuinely relevant debates into 3 to 5 clear policy themes. "
                                "2. Group irrelevant/false positive debates into 'Irrelevant (Other Departments)'. "
                                "Return ONLY a pure JSON object where the keys are the 'Theme Names' and the values are arrays of the 'id' strings."
                            )
                            
                            model_path = get_working_model(GEMINI_API_KEY)
                            ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={GEMINI_API_KEY}"
                            payload = {
                                "contents": [{"parts": [{"text": prompt + "\n\nData: " + json.dumps(titles_only)}]}],
                                "generationConfig": {"responseMimeType": "application/json"}
                            }
                            ai_resp = requests.post(ai_url, json=payload, timeout=60)
                            
                            if ai_resp.status_code == 200:
                                ai_text = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                                clean_text = ai_text.replace('```json', '').replace('```', '').strip()
                                theme_mapping = json.loads(clean_text)

                                mapped_ids = set()
                                for theme, ids in theme_mapping.items():
                                    if "Irrelevant" in theme: continue
                                        
                                    theme_debates = [d for d in process_list if d['id'] in ids]
                                    if theme_debates:
                                        grouped_debates[theme] = theme_debates
                                        mapped_ids.update(ids)
                                
                                unmapped = [d for d in process_list if d['id'] not in mapped_ids]
                                if unmapped: grouped_debates["General / Uncategorized"] = unmapped
                                if leftover_list: grouped_debates[f"Older / Extra Sessions ({len(leftover_list)} Uncategorized)"] = leftover_list

                            else:
                                grouped_debates[f"All Debates (AI Error {ai_resp.status_code})"] = debate_list
                        except Exception as e:
                            grouped_debates[f"All Debates (Failsafe Triggered)"] = debate_list
                    else:
                        grouped_debates["All Debates"] = debate_list
                else:
                    error_message = "No debates found for this period/department."

            except Exception as e:
                error_message = f"Internal Error: {str(e)}"

    return render_template('debate_scanner.html',
                           mode='dept',
                           grouped_debates=grouped_debates,
                           error_message=error_message,
                           start_date=start_date, end_date=end_date,
                           departments=DEPARTMENTS_TWFY, selected_dept=selected_dept,
                           selected_house=selected_house, content_type=content_type,
                           is_post=(request.method == 'POST'),
                           # Other tab defaults
                           topic='', topic_rows=[], topic_briefing=None,
                           topic_briefing_as_text='', house_filter='all',
                           stakeholder_topic='')


# ==========================================
# ROUTE 2: TOPIC SEARCH (NEW — PARALLEL API CALLS)
# ==========================================
@debate_scanner_bp.route('/debates_topic', methods=['GET', 'POST'])
def debates_topic():
    topic_rows = []
    oral_rows = []
    statement_rows = []
    debate_rows = []
    urgent_rows = []
    oral_grouped = []
    urgent_grouped = []
    debate_grouped = []
    wq_rows = []
    wq_total = 0
    topic_briefing = None
    topic_briefing_as_text = ""
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
        selected_depts = request.form.getlist('dept_filter')

        if not topic:
            error_message = "Please enter a topic to search."
        elif not TWFY_API_KEY:
            error_message = "TWFY API key is not configured."
        else:
            date_range = get_twfy_date_range(start_date, end_date)

            if house_filter == 'lords_only':
                sources = ['lords', 'wms']
            elif house_filter == 'commons_only':
                sources = ['commons', 'westminsterhall', 'wms']
            else:
                sources = ['commons', 'westminsterhall', 'lords', 'wms']

            # Always expand the topic with AI for broader matching (acronyms, synonyms, etc.)
            # If a narrow keyword is also set, AND it into the expanded query
            expanded = expand_search_query(topic, GEMINI_API_KEY) if GEMINI_API_KEY else f'"{topic}"'
            if narrow_keyword:
                search_query = f'{expanded} AND "{narrow_keyword}"'
            else:
                search_query = expanded
            debug_query = search_query

            # Resolve department ministers to TWFY person IDs for minister-led search.
            # When a dept is selected, we search each minister's debate history directly
            # so their sessions appear even when their speeches don't contain the keywords.
            minister_people = []
            if selected_depts:
                m_data_for_ids = get_minister_list()
                for dept in selected_depts:
                    minister_people.extend(get_dept_minister_twfy_ids(dept, m_data_for_ids))

            # Run TWFY debate search, Parliament WQ API, and minister-led searches in parallel
            all_rows = []

            def _do_wq_fetch():
                return _fetch_topic_wqs(topic, start_date, end_date, selected_depts)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                twfy_futs = {executor.submit(fetch_twfy_topic, search_query, src, date_range): src for src in sources}
                wq_fut = executor.submit(_do_wq_fetch)
                minister_futs = {
                    executor.submit(fetch_twfy_minister_topic, mp['person_id'], topic, date_range, sources): mp
                    for mp in minister_people
                }
                all_futs = list(twfy_futs.keys()) + [wq_fut] + list(minister_futs.keys())
                for future in concurrent.futures.as_completed(all_futs):
                    if future is wq_fut:
                        try:
                            wq_rows, wq_total = future.result()
                        except Exception:
                            pass
                    else:
                        try:
                            all_rows.extend(future.result())
                        except Exception:
                            pass

            topic_rows = deduplicate_by_listurl(all_rows)

            # Debates-first: expand each matched debate to include ALL speeches.
            # This ensures ministerial responses appear even when they don't contain
            # the search keywords (e.g. minister says "supporting graduates" not "loan repayments").
            if topic_rows:
                session_speeches = fetch_all_debate_sessions(topic_rows, max_debates=25)
                if session_speeches:
                    topic_rows = deduplicate_by_listurl(topic_rows + session_speeches)

            # Flag ministerial speakers and sort them to the top
            minister_data = get_minister_list()
            by_norm = minister_data.get('by_norm', {})
            for row in topic_rows:
                spk = row.get('speaker_name', '')
                norm_spk = _normalise_name(spk)
                role = by_norm.get(norm_spk) if norm_spk else None
                row['is_minister'] = bool(role)
                row['minister_role'] = role or ''

            # Ministers always first, then by relevance within each group
            topic_rows.sort(key=lambda x: (not x.get('is_minister', False), -x.get('relevance', 0)))

            if not topic_rows:
                error_message = f"No parliamentary contributions found for '{topic}'. Try a broader search term or wider date range."
            elif GEMINI_API_KEY:
                try:
                    # Always include ALL ministerial contributions, then balance remaining slots by source
                    minister_rows = [r for r in topic_rows if r.get('is_minister')]
                    non_minister_rows = [r for r in topic_rows if not r.get('is_minister')]
                    seen_sources = {}
                    balanced_rest = []
                    for r in non_minister_rows:
                        src = r['source']
                        if seen_sources.get(src, 0) < 8:
                            balanced_rest.append(r)
                            seen_sources[src] = seen_sources.get(src, 0) + 1
                        if len(balanced_rest) >= 30:
                            break
                    balanced = minister_rows + balanced_rest
                    ai_payload = [
                        {'listurl': r['listurl'], 'speaker': r['speaker_name'],
                         'party': r['speaker_party'], 'date': r['hdate'],
                         'source': r['source_label'], 'text': r['body_clean'],
                         'is_minister': r.get('is_minister', False)}
                        for r in balanced
                    ]
                    prompt = (
                        f"You are a senior UK Parliamentary Researcher. Below are the most relevant parliamentary "
                        f"contributions on the topic: \"{topic}\".\n\n"
                        "Return ONLY a valid JSON object (no markdown fences) with these exact keys:\n"
                        "\"topic_summary\", \"government_position\",\n"
                        "\"opposition_position\" (summarise the positions of ALL opposition parties — Conservative, Liberal Democrat, SNP, Reform UK, Plaid Cymru, and any others — not just one party. "
                        "Do NOT describe Labour backbenchers here; Labour is the governing party. Focus on the official opposition and other opposition parties.),\n"
                        "\"government_speakers\" (array of {\"name\", \"role\", \"stance\"} — Ministers, Secretaries of State, PPSs only),\n"
                        "\"non_government_speakers\" (array of {\"name\", \"role_or_party\", \"stance\"} — Shadow Ministers and opposition backbenchers from ALL parties, plus Lords not in government),\n"
                        "\"key_quotes\" (array of {\"quote\", \"speaker\", \"date\", \"source\", \"listurl\" — the listurl value from the matching DATA entry}),\n"
                        "\"next_steps\", \"coverage_note\"\n\n"
                        f"DATA: {json.dumps(ai_payload)}"
                    )
                    model_path = get_working_model(GEMINI_API_KEY)
                    ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={GEMINI_API_KEY}"
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json"}
                    }
                    ai_resp = requests.post(ai_url, json=payload, timeout=90)
                    if ai_resp.status_code == 200:
                        raw_text = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                        clean_text = raw_text.replace('```json', '').replace('```', '').strip()
                        topic_briefing = json.loads(clean_text)

                        # Verify government speakers against Parliament Members API (parallel)
                        govt_speakers = topic_briefing.get('government_speakers', [])
                        if govt_speakers:
                            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as vex:
                                vfutures = {vex.submit(verify_government_speaker, s.get('name', '')): s for s in govt_speakers}
                                for vf in concurrent.futures.as_completed(vfutures):
                                    spk = vfutures[vf]
                                    v = vf.result()
                                    spk['verified'] = v['confirmed']
                                    spk['confirmed_role'] = v['role']

                        topic_briefing_as_text = format_briefing_as_text(topic_briefing, topic)
                except Exception:
                    topic_briefing = None

            # Split TWFY rows into display sections, then group debates by session
            # Group ALL non-statement rows first, then classify each group.
            # Group-level classification is more reliable than per-row:
            # a group where no speech exceeds 300 words is an Oral PQ session
            # (prepared answer ~150w, follow-ups shorter) not a debate.
            statement_rows = [r for r in topic_rows if r.get('source') == 'wms']
            non_statement_rows = [r for r in topic_rows if r.get('source') != 'wms']
            all_grouped = _group_by_debate(non_statement_rows)

            oral_grouped, urgent_grouped, debate_grouped = [], [], []
            for grp in all_grouped:
                section = _classify_group(grp)
                if section == 'oral':
                    oral_grouped.append(grp)
                elif section == 'urgent':
                    urgent_grouped.append(grp)
                else:
                    debate_grouped.append(grp)

            # Flat row lists for JS download variables
            oral_rows = [r for grp in oral_grouped for r in grp['speeches']]
            urgent_rows = [r for grp in urgent_grouped for r in grp['speeches']]
            debate_rows = [r for grp in debate_grouped for r in grp['speeches']]

    return render_template('debate_scanner.html',
                           mode='topic',
                           topic=topic, topic_rows=topic_rows,
                           oral_rows=oral_rows, statement_rows=statement_rows,
                           urgent_rows=urgent_rows,
                           debate_rows=debate_rows,
                           oral_grouped=oral_grouped, debate_grouped=debate_grouped,
                           urgent_grouped=urgent_grouped,
                           wq_rows=wq_rows, wq_total=wq_total,
                           topic_briefing=topic_briefing,
                           topic_briefing_as_text=topic_briefing_as_text,
                           start_date=start_date, end_date=end_date,
                           house_filter=house_filter,
                           narrow_keyword=narrow_keyword,
                           selected_depts=selected_depts,
                           debug_query=debug_query,
                           user_pref=user_pref,
                           error_message=error_message,
                           grouped_debates={}, departments=DEPARTMENTS_TWFY,
                           selected_dept="All Departments", selected_house="all",
                           content_type="exclude_bills", is_post=True,
                           stakeholder_topic='')


# ==========================================
# ROUTE 3: FETCH TRANSCRIPTS AND ANALYSE (DEPT SCAN)
# ==========================================
@debate_scanner_bp.route('/debates_analyze', methods=['POST'])
def analyze_selected():
    selected = request.form.getlist('selected_debates')
    if not selected:
        return "Please go back and select at least one debate.", 400

    full_transcript_text = ""
    
    try:
        for item in selected:
            parts = item.split('||')
            url = parts[0]
            title = parts[1] if len(parts) > 1 else "Unknown Title"
            date_val = parts[2] if len(parts) > 2 else ""

            if url and url.startswith('http'):
                # Check cache first — transcripts never change once published
                cached = CachedTranscript.get(url)
                if cached:
                    full_transcript_text += f"### DEBATE: {cached.title} ({cached.date})\n\n{cached.transcript_text}\n\n"
                    continue

                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                resp = requests.get(url, headers=headers, timeout=20)

                if resp.status_code == 200:
                    html = resp.text
                    html = re.sub(r'<head.*?>.*?</head>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<style.*?>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<nav.*?>.*?</nav>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<header.*?>.*?</header>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<footer.*?>.*?</footer>', '', html, flags=re.DOTALL|re.IGNORECASE)

                    clean_text = re.sub(r'<[^>]+>', ' ', html)
                    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                    clean_text = clean_text[:60000]

                    # Store in cache for next time
                    try:
                        CachedTranscript.store(url=url, title=title, date=date_val,
                                               house='', transcript_text=clean_text)
                    except Exception:
                        pass

                    session_text = f"### DEBATE: {title} ({date_val})\n\n{clean_text}\n\n"
                    full_transcript_text += session_text
            
        if not full_transcript_text:
            return "No content could be extracted from the URL. Please try another session.", 400

        if GEMINI_API_KEY:
            model_path = get_working_model(GEMINI_API_KEY)
            # THE FIX: Added Section 6 for Active Stakeholders
            prompt = (
                "You are a senior UK Parliamentary Researcher. Analyze the following Hansard transcript "
                "and provide a high-level parliamentary briefing.\n\n"
                "Ignore any remaining website navigation text (like 'Home', 'Members', etc.) and focus only on the debate content.\n"
                "Structure your response exactly as follows (use Markdown formatting):\n"
                "## 1. EXECUTIVE SUMMARY\n(A 3-sentence overview of the main policy direction or debate focus)\n\n"
                "## 2. MINISTERIAL COMMITMENTS\n(Bullet points of any concrete promises, funding, or deadlines mentioned by the government)\n\n"
                "## 3. OPPOSITION CRITIQUE\n(Key challenges raised by Shadow Ministers or other parties)\n\n"
                "## 4. MOOD OF THE HOUSE\n(Is the sentiment generally supportive, hostile, or concerned?)\n\n"
                "## 5. KEY QUESTIONS & RESPONSES\n(Extract 3 to 5 of the most pressing questions asked by Members, paired with the Minister's direct response.)\n\n"
                "## 6. ACTIVE STAKEHOLDERS (NON-MINISTERIAL)\n(Provide a bulleted list of the backbench MPs, Opposition leads, or Peers who spoke in these specific sessions. Exclude the responding Government Minister. Briefly summarize their stance or the specific angle they focused on.)\n\n"
                f"TRANSCRIPTS:\n{full_transcript_text}"
            )
            
            ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={GEMINI_API_KEY}"
            ai_resp = requests.post(ai_url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            
            if ai_resp.status_code == 200:
                briefing_content = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
            else:
                briefing_content = f"AI Analysis failed. (Error {ai_resp.status_code})"

            return render_template('debate_briefing.html', briefing=briefing_content, selected_count=len(selected))

    except Exception as e:
        return f"Error during analysis: {str(e)}", 500


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
            error_message = "Please enter a minister's name."
        elif not TWFY_API_KEY:
            error_message = "TWFY API key is not configured."
        else:
            person_id, matched_name, is_lord = lookup_twfy_person(minister_name)
            if not person_id:
                error_message = f"Could not find '{minister_name}' on TheyWorkForYou. Try their full name."
            else:
                minister_info = {'name': matched_name, 'person_id': person_id, 'is_lord': is_lord}
                date_range = get_twfy_date_range(start_date, end_date)
                minister_debates = fetch_minister_debates(person_id, topic, date_range, house_filter)
                if not minister_debates:
                    error_message = f"No debates found for {matched_name}. Try a broader topic or date range."

    return render_template('debate_scanner.html',
                           mode='minister',
                           minister_name=minister_name, minister_info=minister_info,
                           minister_debates=minister_debates,
                           topic=topic, start_date=start_date, end_date=end_date,
                           house_filter=house_filter, error_message=error_message,
                           grouped_debates={}, departments=DEPARTMENTS_TWFY,
                           selected_dept="All Departments", selected_house="all",
                           content_type="exclude_bills", is_post=(request.method == 'POST'),
                           topic_rows=[], topic_briefing=None, topic_briefing_as_text='',
                           stakeholder_topic='')


# ==========================================
# ROUTE 5: MINISTERIAL RECORD — ANALYSE SELECTED
# ==========================================
@debate_scanner_bp.route('/debates_minister_analyze', methods=['POST'])
def debates_minister_analyze():
    selected = request.form.getlist('selected_debates')
    minister_name = request.form.get('minister_name', 'the Minister').strip()

    if not selected:
        return "Please select at least one debate.", 400

    full_transcript_text = ""
    for item in selected:
        parts = item.split('||')
        url = parts[0]
        title = parts[1] if len(parts) > 1 else "Unknown"
        date_val = parts[2] if len(parts) > 2 else ""

        cached = CachedTranscript.get(url)
        if cached:
            full_transcript_text += f"### {cached.title} ({cached.date})\n\n{cached.transcript_text}\n\n"
            continue

        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200:
                html = resp.text
                for tag in ['head', 'script', 'style', 'nav', 'header', 'footer']:
                    html = re.sub(rf'<{tag}.*?>.*?</{tag}>', '', html, flags=re.DOTALL | re.IGNORECASE)
                clean_text = re.sub(r'<[^>]+>', ' ', html)
                clean_text = re.sub(r'\s+', ' ', clean_text).strip()[:60000]
                try:
                    CachedTranscript.store(url=url, title=title, date=date_val, house='', transcript_text=clean_text)
                except Exception:
                    pass
                full_transcript_text += f"### {title} ({date_val})\n\n{clean_text}\n\n"
        except Exception:
            pass

    if not full_transcript_text:
        return "Could not extract content from the selected debates.", 400

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
    ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={GEMINI_API_KEY}"
    ai_resp = requests.post(ai_url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=120)

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
# ROUTE 7: CUSTOM EXPORT — PICK & CHOOSE WORD DOC
# ==========================================
@debate_scanner_bp.route('/export_custom_briefing', methods=['POST'])
def export_custom_briefing():
    if not Document:
        return "Word library missing.", 500

    topic = request.form.get('topic', 'Parliamentary Research').strip()
    include_briefing = request.form.get('include_briefing', '') == 'true'
    briefing_text = request.form.get('briefing_text', '')
    items_json = request.form.get('items', '[]')
    try:
        items = json.loads(items_json)
    except Exception:
        items = []

    contributions = [i for i in items if i.get('type') in ('contribution', 'speech')]
    pq_items = [i for i in items if i.get('type') == 'pq']

    doc = Document()

    # ── Title block ─────────────────────────────────────────────
    h = doc.add_heading('Parliamentary Research Brief', 0)
    h.alignment = 1  # centre
    sub = doc.add_paragraph(f'Topic: {topic}')
    sub.alignment = 1
    sub.runs[0].bold = True
    date_p = doc.add_paragraph(f'Generated: {datetime.now().strftime("%d %B %Y")}')
    date_p.alignment = 1
    doc.add_paragraph()

    # ── AI Briefing ─────────────────────────────────────────────
    if include_briefing and briefing_text:
        doc.add_heading('AI-Generated Briefing', 1)
        for line in briefing_text.split('\n'):
            line = line.strip()
            if not line:
                doc.add_paragraph()
                continue
            if line.startswith('## '):
                doc.add_heading(line[3:].strip(), level=2)
            elif line.startswith('# '):
                doc.add_heading(line[2:].strip(), level=2)
            elif line.startswith('* ') or line.startswith('- '):
                doc.add_paragraph(line[2:].strip(), style='List Bullet')
            else:
                doc.add_paragraph(line.replace('**', ''))
        doc.add_paragraph()

    # ── Parliamentary Contributions ──────────────────────────────
    if contributions:
        doc.add_heading(f'Selected Parliamentary Contributions ({len(contributions)})', 1)
        for item in contributions:
            d = item.get('data', {})
            # Header row
            p = doc.add_paragraph()
            p.add_run(f"{d.get('hdate', '')}").bold = True
            p.add_run(f"  ·  {d.get('source_label', '')}  ·  {d.get('debate_type', '')}")
            # Speaker
            p2 = doc.add_paragraph()
            p2.add_run(f"{d.get('speaker_name', '')}").bold = True
            party = d.get('speaker_party', '')
            if party:
                p2.add_run(f"  ({party})")
            if d.get('is_minister'):
                p2.add_run("  ✓ Minister").font.color.rgb = None  # keep default
            # Debate title
            title = d.get('debate_title', '')
            if title:
                tp = doc.add_paragraph(title)
                tp.runs[0].italic = True
            # Body text
            body = d.get('body_clean', '')
            if body:
                doc.add_paragraph(f'"{body[:600]}{"…" if len(body) > 600 else ""}"')
            # Link
            url = d.get('listurl', '')
            if url:
                if not url.startswith('http'):
                    url = 'https://www.theyworkforyou.com' + url
                lp = doc.add_paragraph()
                lp.add_run('Source: ').bold = True
                _add_hyperlink(lp, url, url)
            doc.add_paragraph('─' * 60)

    # ── Written Questions ────────────────────────────────────────
    if pq_items:
        # Group by speaker
        by_speaker = {}
        for item in pq_items:
            spk = item.get('speaker', 'Unknown')
            by_speaker.setdefault(spk, []).append(item.get('data', {}))

        doc.add_heading(f'Written Questions ({len(pq_items)} questions across {len(by_speaker)} member(s))', 1)
        for speaker, pqs in by_speaker.items():
            doc.add_heading(f'{speaker}  ({len(pqs)} questions)', 2)
            for pq in pqs:
                # Status badge
                status = pq.get('status', '')
                dept = pq.get('dept', '')
                date = pq.get('date', '')
                p = doc.add_paragraph()
                p.add_run(f"{date}  ·  {dept}  ·  ").bold = False
                sr = p.add_run(status)
                sr.bold = True
                # Question text
                doc.add_paragraph(f'"{pq.get("text", "")}"')
                # Link
                link = pq.get('link', '')
                if link:
                    lp = doc.add_paragraph()
                    lp.add_run('Parliament link: ').bold = True
                    _add_hyperlink(lp, link, link)
                doc.add_paragraph()

    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)

    safe_topic = re.sub(r'[^\w\s-]', '', topic)[:40].strip()
    filename = f"Brief - {safe_topic} - {datetime.now().strftime('%Y%m%d')}.docx"
    return send_file(mem_doc, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


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
    briefing_text = request.form.get('briefing_text', '')
    sections_json = request.form.get('sections_json', '[]')
    depts_json = request.form.get('depts_json', '[]')
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

    SECTION_TITLES = {
        'wq': 'Written Questions & Answers',
        'oral': 'Oral Questions',
        'urgent': 'Urgent Questions',
        'statement': 'Ministerial Statements',
        'debate': 'Parliamentary Debates',
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

    meta_lines = [f'Topic: {topic}']
    if depts:
        meta_lines.append(f'Department(s): {", ".join(depts)}')
    date_parts = []
    if start_date: date_parts.append(f'from {start_date}')
    if end_date: date_parts.append(f'to {end_date}')
    if date_parts:
        meta_lines.append('Date range: ' + ' '.join(date_parts))
    meta_lines.append(f'Generated: {datetime.now().strftime("%d %B %Y at %H:%M")}')
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
            ministers = sum(1 for r in items if r.get('is_minister'))
            row[2].text = f'{ministers} minister(s)' if ministers else '—'
        doc.add_paragraph()

    # ── AI Summary ────────────────────────────────────────────────
    if briefing_text:
        doc.add_heading('AI Summary', 1)
        notice = doc.add_paragraph(
            '⚠ AI-generated — review for accuracy and remove any non-neutral language before use.')
        notice.runs[0].font.size = Pt(9)
        notice.runs[0].italic = True
        _set_run_colour(notice.runs[0], '856404')
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
                    if r.get('is_minister'):
                        role_run = spk_p.add_run(f"  —  {r.get('minister_role', 'Minister')}")
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
                        if not url.startswith('http'):
                            url = 'https://www.theyworkforyou.com' + url
                        lp = doc.add_paragraph()
                        _add_hyperlink(lp, url, '↗ Hansard (TheyWorkForYou)')
                        lp.runs[0].font.size = Pt(9) if lp.runs else None

                doc.add_paragraph('─' * 72)
            doc.add_paragraph()

    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)
    safe_topic = re.sub(r'[^\w\s-]', '', topic)[:40].strip()
    filename = f"Research - {safe_topic} - {datetime.now().strftime('%Y%m%d')}.docx"
    return send_file(mem_doc, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')