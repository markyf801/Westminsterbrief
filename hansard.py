import re
import json
import html as _html_mod
import requests, io, csv
from flask import Blueprint, render_template, request, make_response
from docx import Document
from docx.oxml.shared import OxmlElement, qn
from datetime import datetime, date as date_type
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_models import CachedMember
from extensions import db
from hansard_archive.html_sanitizer import to_plain_text

hansard_bp = Blueprint('hansard', __name__)

_DEPARTMENTS_FALLBACK = {
    "All Departments": "", "Department for Education": "60", "Department of Health and Social Care": "17",
    "HM Treasury": "14", "Home Office": "1", "Ministry of Defence": "11", "Ministry of Justice": "54",
    "Department for Science, Innovation and Technology": "216", "Cabinet Office": "53"
}


def _get_department_list():
    """Return {dept_name: dept_id_str} from ha_pq, alphabetically sorted."""
    from hansard_archive.models import HaPQ
    try:
        rows = (db.session.query(HaPQ.answering_body, HaPQ.answering_body_id)
                .filter(HaPQ.answering_body.isnot(None), HaPQ.answering_body_id.isnot(None))
                .distinct()
                .order_by(HaPQ.answering_body)
                .all())
        depts = {"All Departments": ""}
        for name, body_id in rows:
            if name and body_id is not None:
                depts[name] = str(body_id)
        return depts if len(depts) > 1 else _DEPARTMENTS_FALLBACK
    except Exception:
        return _DEPARTMENTS_FALLBACK

PARTY_COLOURS = {
    'Labour': '#E4003B',
    'Conservative': '#0087DC',
    'Liberal Democrat': '#FAA61A',
    'SNP': '#005EB8',
    'Green Party': '#00B140',
    'Reform UK': '#12B6CF',
    'Plaid Cymru': '#3F8428',
    'DUP': '#D46A4C',
    'Alliance': '#F6CB2F',
}

# DB cap — mirrors the old API cap so template variables are unchanged.
WQ_MAX_RESULTS = 1200

MEMBER_CACHE = {}


def strip_html(raw):
    """Strip HTML tags and decode entities from Parliament API text."""
    if not raw:
        return ''
    text = re.sub(r'<[^>]+>', ' ', raw)
    text = _html_mod.unescape(text)
    return ' '.join(text.split())


def prefetch_members(member_ids):
    """Parallel-fetch member details for all IDs not already cached."""
    unknown = [mid for mid in member_ids if mid and mid not in MEMBER_CACHE and not CachedMember.get(mid)]
    if not unknown:
        return
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(get_member_details, mid, 'Commons'): mid for mid in unknown}
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass


def get_member_details(member_id, fallback_house):
    if not member_id:
        return "Unknown", "No Party", "Unknown", fallback_house
    if member_id in MEMBER_CACHE:
        return MEMBER_CACHE[member_id]

    cached = CachedMember.get(member_id)
    if cached:
        result = (cached.name, cached.party, cached.constituency, cached.house)
        MEMBER_CACHE[member_id] = result
        return result

    try:
        url = f"https://members-api.parliament.uk/api/Members/{member_id}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            data = resp.json().get('value') or {}
            membership = data.get('latestHouseMembership') or {}
            name = data.get('nameDisplayAs') or 'Unknown'
            party = (data.get('latestParty') or {}).get('name') or 'No Party'
            constituency = membership.get('membershipFrom') or 'Unknown'
            house = "Lords" if membership.get('house') == 2 else "Commons"
            image_url = data.get('thumbnailUrl') or ''
            CachedMember.store(member_id, name, party, constituency, house, image_url)
            MEMBER_CACHE[member_id] = (name, party, constituency, house)
            return MEMBER_CACHE[member_id]
    except Exception:
        pass
    return "Member", "Party Info Pending", "Unknown", fallback_house


def add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
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
    new_run.text = text
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _oldest_pq_date() -> str:
    """Return human-readable oldest tabled_date in ha_pq (e.g. '30 Apr 2025'), or ''."""
    try:
        from hansard_archive.models import HaPQ
        from sqlalchemy import func
        oldest = db.session.query(func.min(HaPQ.tabled_date)).scalar()
        return f"{oldest.day} {oldest.strftime('%B %Y')}" if oldest else ''
    except Exception:
        return ''


def _last_pq_update():
    """Return the most recent successful PQ cron run time, or None."""
    try:
        from hansard_archive.models import HaCronRun
        run = (
            HaCronRun.query
            .filter(
                HaCronRun.service_name.in_(['pq-morning', 'pq-afternoon', 'pq-monday']),
                HaCronRun.status == 'ok',
            )
            .order_by(HaCronRun.finished_at.desc())
            .first()
        )
        if run and run.finished_at:
            return run.finished_at.strftime('%d %b %Y %H:%M') + ' UTC'
    except Exception:
        pass
    return None


def _search_pq_db(subjects, start_date, end_date, dept_id, house_filter, status_filter):
    """
    Query ha_pq and return (rows: list[HaPQ], total: int).

    All filters applied at SQL level.
    Keyword search: Postgres FTS via question_tsv GIN index; ILIKE fallback for SQLite.
    Multiple subjects (comma-separated) are OR-ed together.
    is_holding / is_withdrawn are not tracked in the archive — status filters for those
    will naturally return 0 results.
    """
    from hansard_archive.models import HaPQ
    from sqlalchemy import text as sqla_text, or_

    is_pg = db.engine.url.drivername.startswith('postgresql')
    q = HaPQ.query

    if start_date:
        try:
            q = q.filter(HaPQ.tabled_date >= date_type.fromisoformat(start_date))
        except ValueError:
            pass
    if end_date:
        try:
            q = q.filter(HaPQ.tabled_date <= date_type.fromisoformat(end_date))
        except ValueError:
            pass
    if dept_id:
        try:
            q = q.filter(HaPQ.answering_body_id == int(dept_id))
        except (ValueError, TypeError):
            pass
    if house_filter in ('Commons', 'Lords'):
        q = q.filter(HaPQ.chamber == house_filter)
    if status_filter == 'unanswered':
        q = q.filter(HaPQ.is_answered == False)   # noqa: E712
    elif status_filter == 'answered':
        q = q.filter(HaPQ.is_answered == True)    # noqa: E712
    elif status_filter == 'holding':
        q = q.filter(HaPQ.is_holding == True)     # noqa: E712
    elif status_filter == 'withdrawn':
        q = q.filter(HaPQ.is_withdrawn == True)   # noqa: E712

    active_subjects = [s for s in subjects if s]
    if active_subjects:
        if is_pg:
            # OR multiple subjects via tsquery union (||)
            fts_expr = " || ".join(
                f"plainto_tsquery('english', :q{i})" for i in range(len(active_subjects))
            )
            params = {f"q{i}": s for i, s in enumerate(active_subjects)}
            q = q.filter(sqla_text(f"question_tsv @@ ({fts_expr})").bindparams(**params))
        else:
            # SQLite ILIKE fallback (dev only — no GIN index)
            ilike_filters = []
            for subj in active_subjects:
                like = f"%{subj}%"
                ilike_filters.append(or_(
                    HaPQ.heading.ilike(like),
                    HaPQ.question_text.ilike(like),
                    HaPQ.answer_text.ilike(like),
                ))
            q = q.filter(or_(*ilike_filters))

    q = q.order_by(HaPQ.tabled_date.desc())
    total = q.count()
    rows = q.limit(WQ_MAX_RESULTS).all()
    return rows, total


@hansard_bp.route('/questions', methods=['GET', 'POST'])
def index():
    results, error_message = [], None
    selected_dept_id = ""
    selected_house = "All"
    status_filter = "all"
    subject, start_date, end_date = "", "", ""
    total_available = 0
    pre_filter_count = 0
    grouped_results = []
    departments = _get_department_list()

    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        selected_dept_id = request.form.get('department', '').strip()
        selected_house = request.form.get('house_filter', 'All')
        status_filter = request.form.get('status_filter', 'all')
        action = request.form.get('action', 'search')

        subjects = [s.strip() for s in subject.split(',') if s.strip()] or ['']

        try:
            pq_rows, total_available = _search_pq_db(
                subjects, start_date, end_date,
                selected_dept_id, selected_house, status_filter,
            )
            pre_filter_count = len(pq_rows)

            # Pre-warm member cache in parallel (DB lookup, API fallback)
            member_ids = list({r.asking_mnis_id for r in pq_rows if r.asking_mnis_id})
            prefetch_members(member_ids)

            for pq in pq_rows:
                api_name, party, constituency, actual_house = get_member_details(
                    pq.asking_mnis_id, pq.chamber or 'Commons'
                )

                tabled_str = pq.tabled_date.isoformat() if pq.tabled_date else ''
                try:
                    f_date = f"{pq.tabled_date.day} {pq.tabled_date.strftime('%B %Y')}"
                except Exception:
                    f_date = 'N/A'

                try:
                    date_answered = (
                        f"{pq.answer_date.day} {pq.answer_date.strftime('%B %Y')}"
                        if pq.answer_date else ''
                    )
                except Exception:
                    date_answered = ''

                question_url = (
                    f"https://questions-statements.parliament.uk/written-questions/detail"
                    f"/{tabled_str}/{pq.uin}"
                )

                house = pq.chamber or actual_house
                results.append({
                    'uin':                pq.uin,
                    'url':                question_url,
                    'dept':               pq.answering_body or '',
                    'name':               pq.asking_member or api_name or 'Unknown',
                    'party':              party,
                    'role':               "Life Peer" if house == "Lords" else f"MP for {constituency}",
                    'date':               f_date,
                    'text':               pq.question_text or '',
                    'heading':            pq.heading or '',
                    'party_colour':       PARTY_COLOURS.get(party, '#888888'),
                    'answered':           pq.is_answered,
                    'is_holding':         pq.is_holding,
                    'is_withdrawn':       pq.is_withdrawn,
                    'answer_text':        pq.answer_text or '',
                    'answering_minister': pq.answering_member or '',
                    'date_answered':      date_answered,
                    '_sort_date':         tabled_str,
                })

            results.sort(key=lambda r: r.get('_sort_date', ''), reverse=True)
            for r in results:
                r.pop('_sort_date', None)

            # Group by Parliament heading for topic view
            heading_groups = {}
            for r in results:
                key = r.get('heading') or 'Other'
                heading_groups.setdefault(key, []).append(r)
            grouped_results = sorted(heading_groups.items(), key=lambda x: len(x[1]), reverse=True)

            # Export metadata (shared by Word and CSV)
            dept_label = next((k for k, v in departments.items() if v == selected_dept_id), 'All Departments')
            filename_ts = datetime.now().strftime('%Y%m%d_%H%M')
            meta_lines = [
                f"Search: {subject or '(all)'}",
                f"Department: {dept_label}",
                f"House: {selected_house}",
                f"Date range: {start_date or 'any'} to {end_date or 'any'}",
                f"Status filter: {status_filter}",
                f"Results: {len(results)} shown",
                f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')}",
            ]

            if action == 'word' and results:
                doc = Document()
                doc.add_heading('Parliamentary Written Questions', 0)
                for line in meta_lines:
                    p = doc.add_paragraph(line)
                    p.runs[0].italic = True
                doc.add_paragraph()
                for r in results:
                    p = doc.add_paragraph()
                    status_label = ('WITHDRAWN' if r['is_withdrawn'] else
                                    'HOLDING ANSWER' if r['is_holding'] else
                                    'ANSWERED' if r['answered'] else 'UNANSWERED')
                    p.add_run(f"[{status_label}] [{r['dept']}] {r['name']} ({r['party']}, {r['role']})\n").bold = True
                    meta_run = f"UIN: {r['uin']}  ·  Date Asked: {r['date']}"
                    if r['answering_minister']:
                        meta_run += f"  ·  Answered by: {r['answering_minister']}"
                    if r['date_answered']:
                        meta_run += f" ({r['date_answered']})"
                    p.add_run(meta_run + "\n")
                    p.add_run(f"Question: {r['text']}\n")
                    if r['answer_text']:
                        p.add_run(f"Answer: {to_plain_text(r['answer_text'])}\n")
                    p.add_run("Link: ")
                    add_hyperlink(p, r['url'], r['url'])
                b = io.BytesIO()
                doc.save(b)
                b.seek(0)
                resp = make_response(b.getvalue())
                resp.headers["Content-Disposition"] = f"attachment; filename=WQ_{filename_ts}.docx"
                return resp

            elif action == 'csv' and results:
                si = io.StringIO()
                si.write('﻿')  # UTF-8 BOM for Excel on Windows
                cw = csv.writer(si)
                for line in meta_lines:
                    cw.writerow([line])
                cw.writerow([])
                cw.writerow(['UIN', 'Status', 'Department', 'Member', 'Party', 'Role',
                              'Date Asked', 'Answering Minister', 'Date Answered',
                              'Question', 'Answer', 'URL'])
                for r in results:
                    status_label = ('WITHDRAWN' if r['is_withdrawn'] else
                                    'HOLDING ANSWER' if r['is_holding'] else
                                    'ANSWERED' if r['answered'] else 'UNANSWERED')
                    cw.writerow([r['uin'], status_label, r['dept'], r['name'], r['party'],
                                 r['role'], r['date'], r['answering_minister'] or '',
                                 r['date_answered'], r['text'], to_plain_text(r['answer_text']), r['url']])
                resp = make_response(si.getvalue())
                resp.headers["Content-Disposition"] = f"attachment; filename=WQ_{filename_ts}.csv"
                resp.headers["Content-type"] = "text/csv; charset=utf-8"
                return resp

        except Exception as e:
            error_message = f"Search error: {str(e)}"

    return render_template('index.html',
                           results=results,
                           grouped_results=grouped_results,
                           error_message=error_message,
                           departments=departments,
                           selected_dept=selected_dept_id,
                           selected_house=selected_house,
                           status_filter=status_filter,
                           subject=subject,
                           start_date=start_date,
                           end_date=end_date,
                           total_available=total_available,
                           pre_filter_count=pre_filter_count,
                           results_cap=WQ_MAX_RESULTS,
                           last_pq_update=_last_pq_update(),
                           oldest_pq_date=_oldest_pq_date())


@hansard_bp.route('/questions/download_selected', methods=['POST'])
def download_selected():
    try:
        items = json.loads(request.form.get('items_json', '[]'))
    except Exception:
        items = []
    if not items:
        return "No items selected", 400

    fmt = request.form.get('format', 'word')
    filename_ts = datetime.now().strftime('%Y%m%d_%H%M')

    if fmt == 'csv':
        si = io.StringIO()
        si.write('﻿')
        cw = csv.writer(si)
        cw.writerow(['UIN', 'Status', 'Department', 'Member', 'Party', 'Role',
                     'Date Asked', 'Answering Minister', 'Date Answered',
                     'Question', 'Answer', 'URL'])
        for r in items:
            status_label = ('WITHDRAWN' if r.get('is_withdrawn') else
                            'HOLDING ANSWER' if r.get('is_holding') else
                            'ANSWERED' if r.get('answered') else 'UNANSWERED')
            cw.writerow([r.get('uin', ''), status_label, r.get('dept', ''),
                         r.get('name', ''), r.get('party', ''), r.get('role', ''),
                         r.get('date', ''), r.get('answering_minister') or '',
                         r.get('date_answered', ''), r.get('text', ''),
                         to_plain_text(r.get('answer_text', '')), r.get('url', '')])
        resp = make_response(si.getvalue())
        resp.headers["Content-Disposition"] = f"attachment; filename=WQ_selected_{filename_ts}.csv"
        resp.headers["Content-type"] = "text/csv; charset=utf-8"
        return resp

    doc = Document()
    doc.add_heading('Selected Parliamentary Written Questions', 0)
    p = doc.add_paragraph(f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')}  ·  {len(items)} question{'s' if len(items) != 1 else ''}")
    p.runs[0].italic = True
    doc.add_paragraph()
    for r in items:
        p = doc.add_paragraph()
        status_label = ('WITHDRAWN' if r.get('is_withdrawn') else
                        'HOLDING ANSWER' if r.get('is_holding') else
                        'ANSWERED' if r.get('answered') else 'UNANSWERED')
        p.add_run(f"[{status_label}] [{r.get('dept', '')}] {r.get('name', '')} ({r.get('party', '')}, {r.get('role', '')})\n").bold = True
        meta = f"UIN: {r.get('uin', '')}  ·  Date Asked: {r.get('date', '')}"
        if r.get('answering_minister'):
            meta += f"  ·  Answered by: {r['answering_minister']}"
        if r.get('date_answered'):
            meta += f" ({r['date_answered']})"
        p.add_run(meta + "\n")
        p.add_run(f"Question: {r.get('text', '')}\n")
        if r.get('answer_text'):
            p.add_run(f"Answer: {to_plain_text(r['answer_text'])}\n")
        p.add_run("Link: ")
        add_hyperlink(p, r.get('url', ''), r.get('url', ''))
    b = io.BytesIO()
    doc.save(b)
    b.seek(0)
    resp = make_response(b.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename=WQ_selected_{filename_ts}.docx"
    return resp
