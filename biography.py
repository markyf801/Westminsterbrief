import os, requests, io, json, re, concurrent.futures
from flask import Blueprint, render_template, request, jsonify, send_file
from datetime import datetime, timedelta
from cache_models import CachedMember

try:
    import wikipedia
except ImportError:
    wikipedia = None

try:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
except ImportError:
    Document = None

biography_bp = Blueprint('biography', __name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


def _gemini_model():
    if not GEMINI_API_KEY:
        return None
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}",
            timeout=5
        )
        if resp.status_code == 200:
            for pref in ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']:
                for m in resp.json().get('models', []):
                    if pref in m['name'] and 'generateContent' in m.get('supportedGenerationMethods', []):
                        return m['name']
    except:
        pass
    return "models/gemini-1.5-flash"


def _add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True
    )
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


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_parliament_biography(member_id):
    try:
        resp = requests.get(
            f"https://members-api.parliament.uk/api/Members/{member_id}/Biography",
            timeout=8
        )
        if resp.status_code == 200:
            return resp.json().get('value', {})
    except:
        pass
    return {}


def _fetch_recent_pqs(member_id, limit=20):
    try:
        six_months_ago = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
        resp = requests.get(
            "https://questions-statements-api.parliament.uk/api/writtenquestions/questions",
            params={'askingMemberId': member_id, 'tabledWhenFrom': six_months_ago, 'take': limit},
            timeout=10
        )
        if resp.status_code == 200:
            results = []
            for item in resp.json().get('results', []):
                val = item.get('value', {})
                text = val.get('questionText', '').replace('<p>', '').replace('</p>', '')
                dept = val.get('answeringBodyName', '')
                if text:
                    results.append(f"[{dept}] {text[:200]}")
            return results
    except:
        pass
    return []


def _fetch_registered_interests(member_id):
    try:
        resp = requests.get(
            f"https://members-api.parliament.uk/api/Members/{member_id}/RegisteredInterests",
            timeout=8
        )
        if resp.status_code == 200:
            categories = resp.json().get('value', {}).get('categories', []) or []
            lines = []
            for cat in categories:
                cat_name = cat.get('name', '')
                for interest in (cat.get('interests') or []):
                    desc = (interest.get('interest') or '').strip()
                    if desc:
                        lines.append(f"  [{cat_name}] {desc[:200]}")
            return lines
    except:
        pass
    return []


def _fetch_wikipedia(mp_name):
    if not wikipedia:
        return ""
    try:
        search = wikipedia.search(f"{mp_name} British politician")
        if not search:
            search = wikipedia.search(mp_name)
        if search:
            page = wikipedia.page(search[0], auto_suggest=False)
            return page.content[:4000]
    except:
        pass
    return ""


def _format_posts(posts, label):
    if not posts:
        return ""
    lines = [f"\n{label}:"]
    for p in posts:
        name = p.get('name', '')
        start = (p.get('startDate') or '').split('T')[0][:7]
        end = (p.get('endDate') or '').split('T')[0][:7]
        date_str = f"{start}–{end}" if end else f"{start}–present"
        if name:
            lines.append(f"  - {name} ({date_str})")
    return '\n'.join(lines)


def _format_committees(committees):
    if not committees:
        return ""
    lines = ["\nCommittee Memberships (current and historical):"]
    for c in committees:
        name = c.get('name', '')
        start = (c.get('startDate') or '').split('T')[0][:7]
        end = (c.get('endDate') or '').split('T')[0][:7]
        date_str = f"{start}–{end}" if end else f"{start}–present"
        if name:
            lines.append(f"  - {name} ({date_str})")
    return '\n'.join(lines)


# ── Main biography generator ──────────────────────────────────────────────────

def generate_mp_biography(mp_name, member_id):
    # Parallel fetch: Parliament biography API + registered interests + recent PQs + Wikipedia
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_bio = ex.submit(_fetch_parliament_biography, member_id)
        f_interests = ex.submit(_fetch_registered_interests, member_id)
        f_pqs = ex.submit(_fetch_recent_pqs, member_id)
        f_wiki = ex.submit(_fetch_wikipedia, mp_name)
        parl_bio = f_bio.result()
        interests = f_interests.result()
        pqs = f_pqs.result()
        wiki_text = f_wiki.result()

    # Build Parliament context block
    parl_context = ""
    parl_context += _format_posts(parl_bio.get('governmentPosts', []), "Government Roles")
    parl_context += _format_posts(parl_bio.get('oppositionPosts', []), "Opposition Roles")
    parl_context += _format_committees(parl_bio.get('committeeMemberships', []))

    interests_context = ""
    if interests:
        interests_context = "\nRegistered Interests:\n" + "\n".join(f"  • {i}" for i in interests[:20])

    pq_context = ""
    if pqs:
        pq_context = "\nRecent Written Questions (sample):\n" + "\n".join(f"  • {q}" for q in pqs[:12])

    prompt = f"""You are a senior UK parliamentary researcher writing a professional briefing note.

Write a structured profile for: {mp_name}

Use ALL of the source data below. Clearly distinguish between their pre-parliamentary career
(from Wikipedia) and their parliamentary record (from the Parliament API data).

--- WIKIPEDIA EXTRACT (use for background, education, pre-parliament career) ---
{wiki_text[:3500] if wiki_text else "No Wikipedia data available."}

--- PARLIAMENT API DATA ---
{parl_context if parl_context else "No parliamentary posts or committee data available."}
{interests_context}
{pq_context}

---

Write the profile using these exact markdown headings:

## Career Before Parliament
Background, profession, education — draw on Wikipedia. If no data available, say so briefly.

## Parliamentary Career
When elected/appointed, party, constituency or peerage. Key milestones.

## Government & Opposition Roles
All ministerial or shadow ministerial roles held, with dates. Distinguish clearly between government and opposition roles. If none, say so.

## Committee Memberships
List all select committees and other parliamentary committees, with dates (include historical ones). If none on record, say so.

## Registered Interests
Summarise their declared financial and other interests by category. If none declared, say so.

## Policy Interests & Focus Areas
Based on their written questions and stated interests — be specific, not generic.

## Key Context for Meetings
2–3 sentences a civil servant would want to know before a ministerial meeting or correspondence.

Keep each section concise and factual. Do not invent information not present in the source data."""

    model = _gemini_model()
    if not model or not GEMINI_API_KEY:
        return "AI service unavailable — please check your GEMINI_API_KEY."

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=45
        )
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except:
        return "AI summary unavailable — the AI service did not respond in time. Please try again."


# ── Routes ────────────────────────────────────────────────────────────────────

@biography_bp.route('/biography', methods=['GET', 'POST'])
def biography_home():
    mp_data, error = None, None
    if request.method == 'POST':
        member_id = request.form.get('member_id')
        if member_id:
            cached = CachedMember.get(member_id)
            if cached:
                mp_data = {
                    'id': cached.member_id, 'name': cached.name,
                    'party': cached.party, 'constituency': cached.constituency,
                    'image_url': cached.image_url
                }
            else:
                try:
                    resp = requests.get(
                        f"https://members-api.parliament.uk/api/Members/{member_id}",
                        timeout=8
                    ).json().get('value', {})
                    name = resp.get('nameDisplayAs')
                    party = (resp.get('latestParty') or {}).get('name')
                    membership = resp.get('latestHouseMembership') or {}
                    constituency = membership.get('membershipFrom')
                    house = "Lords" if membership.get('house') == 2 else "Commons"
                    image_url = resp.get('thumbnailUrl')
                    CachedMember.store(member_id, name, party, constituency, house, image_url)
                    mp_data = {
                        'id': resp.get('id'), 'name': name, 'party': party,
                        'constituency': constituency, 'image_url': image_url
                    }
                except:
                    error = "Could not load member details."
    return render_template('biography.html', mp_data=mp_data, error_message=error)


@biography_bp.route('/api/search_members')
def api_search_members():
    term = request.args.get('q', '')
    if len(term) < 3:
        return jsonify({"results": []})
    try:
        items = requests.get(
            f"https://members-api.parliament.uk/api/Members/Search?Name={term}&take=15",
            timeout=5
        ).json().get('items') or []
        return jsonify({
            "results": [{"id": i['value']['id'], "text": i['value']['nameFullTitle']} for i in items]
        })
    except:
        return jsonify({"results": []})


@biography_bp.route("/api/biography", methods=["POST"])
def api_biography():
    data = request.get_json()
    bio = generate_mp_biography(data.get("mp_name"), data.get("member_id"))
    return jsonify({"biography": bio})


@biography_bp.route('/export_biography_word', methods=['POST'])
def export_biography_word():
    if not Document:
        return "Word library missing.", 500

    mp_name = request.form.get('mp_name', 'Unknown Member')
    party = request.form.get('party', '')
    constituency = request.form.get('constituency', '')
    bio_text = request.form.get('bio_text', '')

    doc = Document()

    # Title block
    h = doc.add_heading(f'Member Profile: {mp_name}', 0)
    h.alignment = 1
    meta = doc.add_paragraph()
    meta.alignment = 1
    if party:
        meta.add_run(f'{party}').bold = True
    if constituency:
        meta.add_run(f'  ·  {constituency}')
    meta.add_run(f'  ·  Generated {datetime.now().strftime("%d %B %Y")}')
    doc.add_paragraph()

    # Parse and render the markdown biography
    heading_re = re.compile(r'^#{1,3}\s+(.+)$')
    bullet_re = re.compile(r'^[-*]\s+(.+)$')

    for line in bio_text.split('\n'):
        line = line.rstrip()
        if not line:
            doc.add_paragraph()
            continue
        m = heading_re.match(line)
        if m:
            level = line.count('#', 0, 4)
            doc.add_heading(m.group(1).strip(), level=min(level, 3))
            continue
        bm = bullet_re.match(line)
        if bm:
            doc.add_paragraph(bm.group(1).strip(), style='List Bullet')
            continue
        # Strip bold markers and render as plain paragraph
        clean = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
        doc.add_paragraph(clean)

    # Activity links section
    member_id = request.form.get('member_id', '')
    if member_id:
        doc.add_heading('Parliamentary Activity Links', level=2)
        lp = doc.add_paragraph()
        lp.add_run('Hansard contributions: ').bold = True
        _add_hyperlink(lp, f"https://hansard.parliament.uk/search/MemberContributions?memberId={member_id}", "View on Hansard")
        lp2 = doc.add_paragraph()
        lp2.add_run('Written questions: ').bold = True
        _add_hyperlink(lp2, f"https://members.parliament.uk/member/{member_id}/writtenquestions", "View on Parliament.uk")

    mem = io.BytesIO()
    doc.save(mem)
    mem.seek(0)
    safe_name = re.sub(r'[^\w\s-]', '', mp_name)[:40].strip()
    return send_file(
        mem, as_attachment=True,
        download_name=f"Profile - {safe_name}.docx",
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
