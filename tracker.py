import io
import json
import os
import re

import requests
from flask import Blueprint, render_template, request, send_file
from datetime import date, datetime, timedelta
from sqlalchemy import func
from cache_models import CachedMember
from extensions import db, limiter
from hansard_archive.models import PQ_SCHEDULED_RUNS, HaCronRun, HaPQ, HaPQDeadline
from hansard_archive.pq_publication import to_uk

try:
    import docx
    from docx import Document
    from docx.shared import RGBColor, Pt, Inches
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
except ImportError:
    Document = None

tracker_bp = Blueprint('tracker', __name__)
api_key = os.environ.get("GEMINI_API_KEY")

DEPARTMENTS = {
    "All Departments": "", "Department for Education": "60", "Department of Health and Social Care": "17",
    "HM Treasury": "14", "Home Office": "1", "Ministry of Defence": "11", "Ministry of Justice": "54",
    "Department for Science, Innovation and Technology": "216", "Cabinet Office": "53"
}

def add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    c = OxmlElement('w:color')
    c.set(qn('w:val'), '0000FF')
    rPr.append(c)
    u = OxmlElement('w:u')
    u.set(qn('w:val'), 'single')
    rPr.append(u)
    new_run.append(rPr)
    text_element = OxmlElement('w:t')
    text_element.text = text
    new_run.append(text_element)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink

_GEMINI_MODEL_CACHE = {}

# Shared AI categorisation cache: key → (categories_dict, expires_at)
# Keyed by dept_id + sitting_day so 100 concurrent users share one Gemini call.
_AI_CATEGORY_CACHE: dict[str, tuple[dict, datetime]] = {}


def _ai_cache_key(dept_id: str, date_str: str) -> str:
    return f"tracker_ai_{dept_id}_{date_str}"


def _get_cached_categories(dept_id: str, date_str: str) -> dict | None:
    entry = _AI_CATEGORY_CACHE.get(_ai_cache_key(dept_id, date_str))
    if entry is None:
        return None
    categories, expires_at = entry
    if datetime.now() >= expires_at:
        del _AI_CATEGORY_CACHE[_ai_cache_key(dept_id, date_str)]
        return None
    return categories


def _set_cached_categories(dept_id: str, date_str: str, categories: dict) -> None:
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    _AI_CATEGORY_CACHE[_ai_cache_key(dept_id, date_str)] = (categories, midnight)


def _gemini_generate(api_key, prompt):
    """Call Gemini REST API. Auto-detects a working model and endpoint version."""
    global _GEMINI_MODEL_CACHE

    if api_key not in _GEMINI_MODEL_CACHE:
        try:
            resp = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": api_key},  # not in the URL, so it can't leak into logs
                timeout=5
            )
            if resp.status_code == 200:
                available = [m['name'] for m in resp.json().get('models', [])
                             if 'generateContent' in m.get('supportedGenerationMethods', [])]
                # Prefer 2.5-flash-lite (same price as 2.0, available to all keys)
                # 2.0-flash and 2.0-flash-lite deprecated Feb 2026, hard shutdown Jun 2026
                for prefix in ['models/gemini-2.5-flash-lite', 'models/gemini-2.5-flash',
                               'models/gemini-flash-latest']:
                    match = next((m for m in available if m.startswith(prefix)), None)
                    if match:
                        _GEMINI_MODEL_CACHE[api_key] = match.removeprefix('models/')
                        break
                else:
                    first = available[0] if available else None
                    _GEMINI_MODEL_CACHE[api_key] = first.removeprefix('models/') if first else 'gemini-2.5-flash-lite'
        except Exception:
            _GEMINI_MODEL_CACHE[api_key] = 'gemini-2.5-flash-lite'

    model = _GEMINI_MODEL_CACHE.get(api_key, 'gemini-2.5-flash-lite')
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for version in ('v1', 'v1beta'):
        url = f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": api_key}, json=payload, timeout=90)
            if r.status_code == 200:
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            if r.status_code not in (404, 400):
                raise Exception(f"HTTP {r.status_code}")
        except Exception:
            continue

    raise Exception(f"All endpoints failed for {model}")

def _categorise_questions(questions: list[dict], dept_id: str, date_str: str, gemini_key: str | None = None) -> dict:
    """Return {uin: theme} mapping. Returns cached result if available; calls Gemini on miss."""
    cached = _get_cached_categories(dept_id, date_str)
    if cached is not None:
        return cached

    categories: dict = {}
    if not gemini_key:
        return categories

    try:
        questions_data = [{"uin": r['uin'], "text": r['text'][:200]} for r in questions]
        prompt = (
            "Categorize these UK Parliamentary questions into broad team-level policy themes "
            "that a single policy team would own (e.g., 'SEND', 'Early Years', 'Higher Education Finance', "
            "'Disabled Children\\'s Social Care', 'School Standards'). "
            "Use short, team-level labels -- do NOT add sub-categories or qualifiers after a dash. "
            "Return ONLY a valid JSON dictionary where keys are the UIN strings and values are the Themes. "
            f"Data: {json.dumps(questions_data)}"
        )
        raw_text = _gemini_generate(gemini_key, prompt)
        match = re.search(r'\{.*\}|\[.*\]', raw_text.replace('\n', ' '), re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                for item in parsed:
                    k = str(item.get('uin', item.get('id', '')))
                    v = str(item.get('theme', 'Uncategorized'))
                    if k:
                        categories[k] = v
            elif isinstance(parsed, dict):
                for k, v in parsed.items():
                    categories[str(k)] = str(v)
        else:
            print("[tracker] categorisation issue: unexpected response format")
        _set_cached_categories(dept_id, date_str, categories)
    except Exception as e:
        print(f"[tracker] categorisation issue: AI Error: {str(e)[:60]}")

    return categories


# Today's PQs reads only our own database (ha_pq, ha_pq_deadline,
# cached_member, ha_cron_run): no Parliament API calls on page view.
LOOKBACK_DAYS = 5   # walk back this many days (from yesterday) for a sitting day


def question_type(house: str | None, is_named_day: bool | None) -> str:
    """Badge type: LORDS (14 calendar day deadline), NAMED_DAY (date set by
    the MP, min 2 sitting days) or ORDINARY (conventional 7 sitting days)."""
    if house == 'Lords':
        return 'LORDS'
    if is_named_day:
        return 'NAMED_DAY'
    return 'ORDINARY'


def _human_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def _unanswered_filters(dept_id: str) -> list:
    conds = [HaPQ.is_answered.is_(False), HaPQ.is_withdrawn.is_(False)]
    if dept_id:
        conds.append(HaPQ.answering_body_id == int(dept_id))
    return conds


def _latest_sitting_day(dept_id: str, today: date) -> date | None:
    """Most recent day from yesterday back LOOKBACK_DAYS with unanswered
    questions for the department (handles weekends, bank holidays, recess)."""
    return (db.session.query(func.max(HaPQ.tabled_date))
            .filter(HaPQ.tabled_date >= today - timedelta(days=LOOKBACK_DAYS),
                    HaPQ.tabled_date <= today - timedelta(days=1),
                    *_unanswered_filters(dept_id))
            .scalar())


def _unanswered_pqs(dept_id: str, day: date) -> list[HaPQ]:
    return (HaPQ.query
            .filter(HaPQ.tabled_date == day, *_unanswered_filters(dept_id))
            .order_by(HaPQ.answering_body, HaPQ.id)
            .all())


def _deadlines(api_ids: list[int]) -> dict[int, HaPQDeadline]:
    """{api_id: deadline row}. Empty (logged) if ha_pq_deadline is unreadable,
    e.g. before migrations/pq_deadline_table.py has run: the page still works,
    just without due dates and named-day badges."""
    if not api_ids:
        return {}
    try:
        rows = HaPQDeadline.query.filter(HaPQDeadline.api_id.in_(api_ids)).all()
    except Exception as e:
        db.session.rollback()
        print(f"[tracker] ha_pq_deadline unreadable, due dates skipped: {str(e)[:120]}")
        return {}
    return {r.api_id: r for r in rows}


def _members(member_ids: set[int]) -> dict[int, CachedMember]:
    if not member_ids:
        return {}
    rows = CachedMember.query.filter(CachedMember.member_id.in_(list(member_ids))).all()
    return {m.member_id: m for m in rows}


def _pq_updated_label(now_utc: datetime | None = None) -> str | None:
    """'09:42 today' / '14:05 on 21 October' (UK time) for the newest PQ cron
    run that finished successfully (scheduled crons only, not one-off runs);
    None if there is none or it can't be read."""
    try:
        finished = (db.session.query(func.max(HaCronRun.finished_at))
                    .filter(HaCronRun.service_name.in_(PQ_SCHEDULED_RUNS),
                            HaCronRun.status == 'ok')
                    .scalar())
    except Exception as e:
        db.session.rollback()
        print(f"[tracker] could not read ha_cron_run: {str(e)[:120]}")
        return None
    if finished is None:
        return None
    at = to_uk(finished)
    today_uk = to_uk(now_utc or datetime.utcnow()).date()
    if at.date() == today_uk:
        return f"{at:%H:%M} today"
    return f"{at:%H:%M} on {at.day} {at:%B}"


def _result_row(pq: HaPQ, member: CachedMember | None, deadline: HaPQDeadline | None) -> dict:
    tabled = pq.tabled_date.isoformat()
    due = deadline.date_for_answer if deadline else None
    return {
        'dept': pq.answering_body,
        'uin': str(pq.uin),
        'member': (member.name if member and member.name else None)
                  or pq.asking_member or "Unknown Member",
        'member_id': pq.asking_mnis_id,
        'party': member.party if member else '',
        'constituency': member.constituency if member else '',
        'house': pq.chamber or 'Commons',
        'text': pq.question_text or '',
        'raw_date': tabled,
        'date_asked': _human_date(pq.tabled_date),
        'due_date': tabled,
        'date_for_answer': due.isoformat() if due else '',
        'question_type': question_type(pq.chamber, deadline.is_named_day if deadline else None),
        'is_answered': False,
        'status': 'UNANSWERED',
    }


@tracker_bp.route('/tracker', methods=['GET', 'POST'])
@limiter.limit("10 per minute; 100 per day", methods=["POST"])
def morning_tracker():
    sorted_grouped_results = {}
    error_message = None
    selected_dept = ""
    sitting_day_used = None

    if request.method == 'POST':
        selected_dept = request.form.get('department', '').strip()
        if selected_dept not in DEPARTMENTS.values():
            selected_dept = ""
        results = []

        try:
            day = _latest_sitting_day(selected_dept, datetime.now().date())
            pqs = _unanswered_pqs(selected_dept, day) if day else []
            if pqs:
                sitting_day_used = day.isoformat()
            members = _members({pq.asking_mnis_id for pq in pqs if pq.asking_mnis_id})
            deadlines = _deadlines([pq.api_id for pq in pqs if pq.api_id])
            results = [_result_row(pq, members.get(pq.asking_mnis_id), deadlines.get(pq.api_id))
                       for pq in pqs]

            categories = _categorise_questions(results, selected_dept, sitting_day_used, api_key) if results else {}

            if results:
                temp_group = {}
                for r in results:
                    r_date = r['raw_date']
                    theme = categories.get(r['uin']) or "Uncategorized"
                    if ' - ' in theme:
                        theme = theme.split(' - ')[0].strip()
                    if r_date not in temp_group:
                        temp_group[r_date] = {'display_date': r['date_asked'], 'themes': {}}
                    if theme not in temp_group[r_date]['themes']:
                        temp_group[r_date]['themes'][theme] = []
                    temp_group[r_date]['themes'][theme].append(r)

                for date_key in sorted(temp_group.keys(), reverse=True):
                    sorted_grouped_results[date_key] = temp_group[date_key]

        except Exception as e:
            db.session.rollback()
            print(f"[tracker] search error: {type(e).__name__}: {str(e)[:200]}")
            error_message = "Something went wrong loading the questions. Please try again in a moment."

    return render_template(
        'tracker.html',
        sorted_grouped_results=sorted_grouped_results,
        error_message=error_message,
        departments=DEPARTMENTS,
        selected_dept=selected_dept,
        is_post=(request.method == 'POST'),
        sitting_day_used=sitting_day_used,
        pq_updated_label=_pq_updated_label(),
    )

HISTORY_DAYS = 90
HISTORY_PER_MEMBER = 50


def _member_history(member_ids: set[int], since: date, dept_id: str) -> dict[int, list]:
    """{member id: newest HISTORY_PER_MEMBER of their questions tabled since
    `since` (rows of uin, tabled_date, question_text)}, in one ha_pq query."""
    if not member_ids:
        return {}
    q = (db.session.query(HaPQ.asking_mnis_id, HaPQ.uin, HaPQ.tabled_date, HaPQ.question_text)
         .filter(HaPQ.asking_mnis_id.in_(list(member_ids)), HaPQ.tabled_date >= since))
    if dept_id:
        q = q.filter(HaPQ.answering_body_id == int(dept_id))
    out: dict[int, list] = {}
    for row in q.order_by(HaPQ.tabled_date.desc(), HaPQ.id.desc()):
        rows = out.setdefault(row.asking_mnis_id, [])
        if len(rows) < HISTORY_PER_MEMBER:
            rows.append(row)
    return out


@tracker_bp.route('/download_tracker_word', methods=['POST'])
def download_tracker_word():
    if not Document:
        return "Word generator library missing! Please run 'pip install python-docx' in your PythonAnywhere bash console.", 500

    export_data_str = request.form.get('export_data')
    include_history = request.form.get('include_history') == 'true'
    include_ai_context = request.form.get('include_ai_context') == 'true'
    selected_dept = request.form.get('selected_dept', '').strip()
    if selected_dept not in DEPARTMENTS.values():
        selected_dept = ""

    if not export_data_str:
        return "No data provided to download.", 400
    export_data = json.loads(export_data_str)

    history = {}
    if include_history:
        member_ids = {int(q['member_id']) for section in export_data for q in section['questions']
                      if str(q.get('member_id') or '').isdigit()}
        try:
            history = _member_history(member_ids, datetime.now().date() - timedelta(days=HISTORY_DAYS),
                                      selected_dept)
        except Exception as e:
            db.session.rollback()
            print(f"[tracker] history query error: {str(e)[:120]}")
    
    ai_context_dict = {}
    if include_ai_context and api_key:
        flat_questions = []
        for section in export_data:
            for q in section['questions']:
                flat_questions.append({"uin": q['uin'], "member": q['member'], "text": q['text']})
        
        try:
            prompt = (
                "You are an expert UK political analyst. For each of the following parliamentary questions, "
                "provide a brief 2-sentence explanation of WHY this specific MP might be asking this right now. "
                "Consider local constituency issues, party political campaigns, or recent national news. "
                "Return a pure JSON dictionary where the keys are the UIN strings and the values are your analysis. "
                f"Data: {json.dumps(flat_questions)}"
            )
            raw_text = _gemini_generate(api_key, prompt)
            match = re.search(r'\{.*\}', raw_text.replace('\n', ' '), re.DOTALL)
            if match:
                ai_context_dict = json.loads(match.group(0))
        except Exception as e:
            print(f"AI Context Error: {e}")

    def _fmt_date(iso_str: str) -> str:
        try:
            d = datetime.fromisoformat(iso_str)
            month_year = d.strftime("%B %Y")
            return f"{d.day} {month_year}"
        except Exception:
            return iso_str

    def _set_indent(para, inches: float):
        para.paragraph_format.left_indent = Inches(inches)

    doc = Document()
    doc.add_heading("Today's PQs (Enhanced Briefing)", 0)

    ignore_words = {"school", "schools", "education", "student", "students", "university", "universities", "college", "health", "nhs", "funding", "fund", "policy", "department", "support", "review", "system", "provision", "england"}

    for section in export_data:
        theme = section['theme']
        doc.add_heading(f"\U0001f4c5 {section['date']} -- {theme}", level=1)

        for q in section['questions']:
            # Header: [STATUS] Member (UIN: xxx)
            p = doc.add_paragraph()
            header_run = p.add_run(f"[{q.get('status', 'UNANSWERED')}] {q['member']} (UIN: {q['uin']})")
            header_run.bold = True

            # Question text -- plain, not italic, indented
            q_p = doc.add_paragraph(f'"{q["text"]}"')
            _set_indent(q_p, 0.2)

            # Tabled date -- small, muted, human-readable
            date_p = doc.add_paragraph()
            date_run = date_p.add_run(f"Tabled: {_fmt_date(q['due_date'])}")
            date_run.font.size = Pt(9)
            date_run.font.color.rgb = RGBColor(120, 120, 120)
            _set_indent(date_p, 0.2)

            # Link to this question on Parliament.uk
            q_link_p = doc.add_paragraph()
            q_link = f"https://questions-statements.parliament.uk/written-questions/detail/{q['due_date']}/{q['uin']}"
            add_hyperlink(q_link_p, q_link, "View question ->")
            _set_indent(q_link_p, 0.2)

            if include_ai_context:
                analysis = ai_context_dict.get(str(q['uin']))
                if analysis:
                    ai_p = doc.add_paragraph()
                    ai_run = ai_p.add_run(f"\U0001f916 AI Political Context: {analysis}")
                    ai_run.font.color.rgb = RGBColor(100, 100, 100)
                    ai_run.font.size = Pt(10)
                    _set_indent(ai_p, 0.2)

            if include_history and str(q.get('member_id') or '').isdigit():
                similar_questions_data = []
                keywords = [w.lower() for w in re.findall(r'\w+', theme)
                            if len(w) > 3 and w.lower() not in ignore_words and w.lower() != 'uncategorized']
                if not keywords:
                    keywords = [w.lower() for w in re.findall(r'\w+', q['text'])
                                if len(w) > 4 and w.lower() not in ignore_words]

                for h in history.get(int(q['member_id']), []):
                    h_uin = str(h.uin)
                    h_date = h.tabled_date.isoformat()
                    if h_uin == str(q['uin']) and h_date == q.get('due_date'):
                        continue
                    h_text = h.question_text or ''
                    if keywords and any(kw in h_text.lower() for kw in keywords):
                        h_link = f"https://questions-statements.parliament.uk/written-questions/detail/{h_date}/{h_uin}"
                        similar_questions_data.append({'uin': h_uin, 'date': h_date, 'text': h_text, 'link': h_link})

                if similar_questions_data:
                    hp = doc.add_paragraph()
                    hist_run = hp.add_run(f"↳ See previous responses by {q['member']} on this topic (last 3 months):")
                    hist_run.bold = True
                    hist_run.font.size = Pt(10)
                    _set_indent(hp, 0.2)

                    for sq in similar_questions_data:
                        sq_p = doc.add_paragraph()
                        sq_p.add_run(f'[UIN: {sq["uin"]} | {_fmt_date(sq["date"])}]  "{sq["text"]}"')
                        sq_p.paragraph_format.left_indent = Inches(0.4)
                        sq_p.paragraph_format.first_line_indent = Inches(-0.15)

                        link_p = doc.add_paragraph()
                        add_hyperlink(link_p, sq['link'], "View question →")
                        link_p.paragraph_format.left_indent = Inches(0.4)

            doc.add_paragraph()
            
    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)
    
    return send_file(
        mem_doc,
        as_attachment=True,
        download_name=f"Today_PQs_Briefing_{datetime.now().strftime('%Y%m%d')}.docx",
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )