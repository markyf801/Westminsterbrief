import re
import json
import html as _html_mod
import time
import requests
import io
import csv
from urllib.parse import urlencode
from flask import Blueprint, render_template, request, make_response, redirect
from docx import Document
from docx.oxml.shared import OxmlElement, qn
from datetime import datetime, date as date_type, timedelta
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


# Only departments that answered questions recently are offered in the filter,
# so bodies abolished in a machinery-of-government change drop out by themselves.
_DEPT_ACTIVE_DAYS = 183
_DEPT_CACHE_TTL = 3600  # 1 hour
_dept_list_cache: tuple | None = None  # (depts, monotonic timestamp)


def _get_department_list():
    """
    Return {dept_name: dept_id_str} for departments with questions tabled in the
    6 months before the newest question in ha_pq, alphabetically sorted.

    Measured from the newest question rather than today so a stale archive still
    lists departments. Cached for an hour.
    """
    global _dept_list_cache
    now = time.monotonic()
    if _dept_list_cache and now - _dept_list_cache[1] < _DEPT_CACHE_TTL:
        return _dept_list_cache[0]

    from hansard_archive.models import HaPQ
    from sqlalchemy import func
    try:
        newest = db.session.query(func.max(HaPQ.tabled_date)).scalar()
        if newest is None:
            return _DEPARTMENTS_FALLBACK
        rows = (db.session.query(HaPQ.answering_body, HaPQ.answering_body_id)
                .filter(HaPQ.answering_body.isnot(None), HaPQ.answering_body_id.isnot(None),
                        HaPQ.tabled_date >= newest - timedelta(days=_DEPT_ACTIVE_DAYS))
                .distinct()
                .order_by(HaPQ.answering_body)
                .all())
        depts = {"All Departments": ""}
        for name, body_id in rows:
            if name and body_id is not None:
                depts[name] = str(body_id)
        if len(depts) == 1:
            return _DEPARTMENTS_FALLBACK
        _dept_list_cache = (depts, now)
        return depts
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
WQ_PAGE_SIZE = 50
# Results list: answers up to _WQ_SHORT_ANSWER chars of HTML show in full; longer
# ones show a WQ_EXCERPT_CHARS plain-text excerpt cut from the first _WQ_ANSWER_HEAD.
WQ_EXCERPT_CHARS = 400
_WQ_SHORT_ANSWER = 1500
_WQ_ANSWER_HEAD = 2000

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


def _add_html_table_to_doc(doc, table_html: str) -> None:
    """Convert a single HTML <table> to a Word table with grid borders."""
    row_matches = re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', table_html, re.IGNORECASE)
    if not row_matches:
        return
    grid = []
    header_flags = []
    for row_html in row_matches:
        cells = re.findall(r'<(th|td)[^>]*>([\s\S]*?)</t[hd]>', row_html, re.IGNORECASE)
        row_texts = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', content)).strip()
                     for _, content in cells]
        if any(row_texts):
            grid.append(row_texts)
            header_flags.append(bool(cells) and cells[0][0].lower() == 'th')
    if not grid:
        return
    col_count = max(len(row) for row in grid)
    table = doc.add_table(rows=len(grid), cols=col_count)
    try:
        table.style = 'Table Grid'
    except Exception:
        pass
    for i, row_texts in enumerate(grid):
        word_row = table.rows[i]
        for j in range(col_count):
            cell_text = row_texts[j] if j < len(row_texts) else ''
            cell = word_row.cells[j]
            cell.text = cell_text
            if header_flags[i] and cell.paragraphs and cell.paragraphs[0].runs:
                cell.paragraphs[0].runs[0].bold = True
    doc.add_paragraph()  # spacing after table


def _add_answer_to_doc(doc, answer_html: str) -> None:
    """Add HTML answer content to a Word document.

    Non-table segments are added as plain-text paragraphs.
    <table> elements are added as proper Word tables.
    """
    if not answer_html:
        return
    parts = re.split(r'(<table[\s\S]*?</table>)', answer_html, flags=re.IGNORECASE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if re.match(r'<table', part, re.IGNORECASE):
            _add_html_table_to_doc(doc, part)
        else:
            text = to_plain_text(part).strip()
            if text:
                doc.add_paragraph(text)


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
                # Every PQ cron records a "pq-*" name (pq-cron-early, pq-quickcheck, ...).
                HaCronRun.service_name.like('pq-%'),
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


def _pq_query(subjects, start_date, end_date, dept_id, house_filter, status_filter):
    """
    Build the ha_pq query for a Written Questions search, newest first.

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

    # id breaks ties within a day so OFFSET paging is stable
    return q.order_by(HaPQ.tabled_date.desc(), HaPQ.id.desc())


def _wq_list_columns():
    """Columns the results list needs: answer HTML is cut to its first few KB, plus its full length."""
    from hansard_archive.models import HaPQ
    from sqlalchemy import func
    return (
        HaPQ.uin, HaPQ.heading, HaPQ.question_text, HaPQ.tabled_date, HaPQ.answer_date,
        HaPQ.chamber, HaPQ.asking_member, HaPQ.asking_mnis_id, HaPQ.answering_member,
        HaPQ.answering_mnis_id, HaPQ.answering_body, HaPQ.is_answered, HaPQ.is_holding,
        HaPQ.is_withdrawn,
        func.substr(HaPQ.answer_text, 1, _WQ_ANSWER_HEAD).label('answer_text'),
        func.length(HaPQ.answer_text).label('answer_len'),
    )


def _pq_result(pq) -> dict:
    """Display/export dict for one PQ (an HaPQ or a row from _wq_list_columns)."""
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

    answering_role = None
    if pq.answering_mnis_id:
        ans_cached = CachedMember.get(pq.answering_mnis_id)
        if ans_cached:
            from ministers import pq_answering_role
            answering_role = pq_answering_role(pq.answering_mnis_id, pq.answering_body,
                                               pq.answer_date, fallback=ans_cached.ministerial_role)

    return {
        'uin':                    pq.uin,
        'url':                    question_url,
        'dept':                   pq.answering_body or '',
        'name':                   pq.asking_member or api_name or 'Unknown',
        'party':                  party,
        'role':                   "Life Peer" if house == "Lords" else f"MP for {constituency}",
        'date':                   f_date,
        'text':                   pq.question_text or '',
        'heading':                pq.heading or '',
        'party_colour':           PARTY_COLOURS.get(party, '#888888'),
        'answered':               pq.is_answered,
        'is_holding':             pq.is_holding,
        'is_withdrawn':           pq.is_withdrawn,
        'answer_text':            pq.answer_text or '',
        'answering_minister':     pq.answering_member or '',
        'answering_minister_role': answering_role or '',
        'date_answered':          date_answered,
    }


def _add_answer_preview(r: dict, answer_len) -> None:
    """
    Short answers stay as full HTML. Long ones (or any with a table) become a
    plain-text excerpt; the full answer is on /archive/pq/<uin>.
    """
    head = r['answer_text']
    r['answer_truncated'] = False
    if not head:
        return
    if (answer_len or 0) <= _WQ_SHORT_ANSWER and '<table' not in head:
        return
    plain = ' '.join(to_plain_text(re.sub(r'<[^>]*$', '', head)).split())
    if len(plain) > WQ_EXCERPT_CHARS:
        plain = plain[:WQ_EXCERPT_CHARS].rsplit(' ', 1)[0] + '…'
    r['answer_text'] = ''
    r['answer_excerpt'] = plain
    r['answer_truncated'] = True


def _status_label(r: dict) -> str:
    return ('WITHDRAWN' if r.get('is_withdrawn') else
            'HOLDING ANSWER' if r.get('is_holding') else
            'ANSWERED' if r.get('answered') else 'UNANSWERED')


# GET parameters for a Written Questions search. The keyword is ?q= (the old
# POST form field was "subject", still accepted); the rest keep the form names.
_WQ_FILTER_PARAMS = ('department', 'house_filter', 'status_filter', 'start_date', 'end_date')
_WQ_DEFAULTS = {'house_filter': 'All', 'status_filter': 'all'}


def _wq_search_qs(source) -> str:
    """Query string for a WQ search from a form/args MultiDict, dropping empty and default values."""
    params = {}
    q = (source.get('q') or source.get('subject') or '').strip()
    if q:
        params['q'] = q
    for key in _WQ_FILTER_PARAMS:
        val = (source.get(key) or '').strip()
        if val and val != _WQ_DEFAULTS.get(key):
            params[key] = val
    return urlencode(params)


def _wq_page_url(source, page: int) -> str:
    """URL of one results page; keeps the search and filters. Page 1 has no ?page=."""
    # An empty search still needs a parameter, or the page shows the blank form
    qs = _wq_search_qs(source) or 'q='
    return f"/written-questions?{qs}" + (f"&page={page}" if page > 1 else '')


def _parse_page(raw) -> int:
    """?page= as a positive int; anything unreadable is page 1."""
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 1


def _wq_pager(source, page: int, last_page: int) -> dict:
    """Previous/Next links and a short run of page numbers (None marks a gap)."""
    shown = sorted({1, last_page, *range(max(1, page - 2), min(last_page, page + 2) + 1)})
    numbers = []
    for n in shown:
        if numbers and n - numbers[-1][0] > 1:
            numbers.append((None, None))
        numbers.append((n, _wq_page_url(source, n)))
    return {
        'page': page,
        'last_page': last_page,
        'prev_url': _wq_page_url(source, page - 1) if page > 1 else None,
        'next_url': _wq_page_url(source, page + 1) if page < last_page else None,
        'numbers': numbers,
    }


def _wq_export(action, rows, total, meta_lines, filename_ts):
    """Word or CSV download of the full (capped) result set."""
    results = [_pq_result(pq) for pq in rows]
    meta_lines = meta_lines + [
        f"Results: {len(results)}" + (f" (newest {len(results)} of {total})" if total > len(results) else ''),
        f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')}",
    ]

    if action == 'word':
        doc = Document()
        doc.add_heading('Parliamentary Written Questions', 0)
        for line in meta_lines:
            p = doc.add_paragraph(line)
            p.runs[0].italic = True
        doc.add_paragraph()
        for r in results:
            p = doc.add_paragraph()
            p.add_run(f"[{_status_label(r)}] [{r['dept']}] {r['name']} ({r['party']}, {r['role']})\n").bold = True
            meta_run = f"UIN: {r['uin']}  ·  Date Asked: {r['date']}"
            if r['answering_minister']:
                meta_run += f"  ·  Answered by: {r['answering_minister']}"
                if r.get('answering_minister_role'):
                    meta_run += f" ({r['answering_minister_role']})"
            if r['date_answered']:
                meta_run += f"  ·  {r['date_answered']}"
            p.add_run(meta_run + "\n")
            p.add_run(f"Question: {r['text']}")
            if r['answer_text']:
                if '<table' in r['answer_text']:
                    p.add_run("\nAnswer:")
                    _add_answer_to_doc(doc, r['answer_text'])
                else:
                    p.add_run(f"\nAnswer: {to_plain_text(r['answer_text'])}")
            p_link = doc.add_paragraph()
            p_link.add_run("Link: ")
            add_hyperlink(p_link, r['url'], r['url'])
        b = io.BytesIO()
        doc.save(b)
        b.seek(0)
        resp = make_response(b.getvalue())
        resp.headers["Content-Disposition"] = f"attachment; filename=WQ_{filename_ts}.docx"
        return resp

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
        cw.writerow([r['uin'], _status_label(r), r['dept'], r['name'], r['party'],
                     r['role'], r['date'], r['answering_minister'] or '',
                     r['date_answered'], r['text'], to_plain_text(r['answer_text']), r['url']])
    resp = make_response(si.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename=WQ_{filename_ts}.csv"
    resp.headers["Content-type"] = "text/csv; charset=utf-8"
    return resp


@hansard_bp.route('/questions', methods=['GET', 'POST'])
def index():
    results, error_message = [], None
    selected_dept_id = ""
    selected_house = "All"
    status_filter = "all"
    subject, start_date, end_date = "", "", ""
    total_available = 0
    grouped_results = []
    pager = None
    page_start = page_end = 0
    departments = _get_department_list()

    # Searches run on GET so results can be bookmarked and shared. POST is kept
    # for the Export buttons; a POSTed plain search redirects to the GET URL.
    if request.method == 'POST':
        params = request.form
        action = request.form.get('action', 'search')
        if action not in ('word', 'csv'):
            qs = _wq_search_qs(params)
            # An empty search lists every question, as the POST form always did
            return redirect(f"/written-questions?{qs or 'q='}", 303)
    else:
        params = request.args
        action = 'search'
    searched = request.method == 'POST' or any(
        k in request.args for k in ('q', 'subject') + _WQ_FILTER_PARAMS)

    if searched:
        subject = (params.get('q') or params.get('subject') or '').strip()
        start_date = params.get('start_date', '').strip()
        end_date = params.get('end_date', '').strip()
        selected_dept_id = params.get('department', '').strip()
        selected_house = params.get('house_filter', 'All')
        status_filter = params.get('status_filter', 'all')

        subjects = [s.strip() for s in subject.split(',') if s.strip()] or ['']

        try:
            q = _pq_query(subjects, start_date, end_date,
                          selected_dept_id, selected_house, status_filter)
            total_available = q.count()

            # Exports rebuild the whole (capped) result set from the filters,
            # not just the page on screen.
            if action in ('word', 'csv') and total_available:
                export_rows = q.limit(WQ_MAX_RESULTS).all()
                prefetch_members(list({r.asking_mnis_id for r in export_rows if r.asking_mnis_id}))
                dept_label = next((k for k, v in departments.items() if v == selected_dept_id),
                                  'All Departments')
                meta_lines = [
                    f"Search: {subject or '(all)'}",
                    f"Department: {dept_label}",
                    f"House: {selected_house}",
                    f"Date range: {start_date or 'any'} to {end_date or 'any'}",
                    f"Status filter: {status_filter}",
                ]
                return _wq_export(action, export_rows, total_available, meta_lines,
                                  datetime.now().strftime('%Y%m%d_%H%M'))

            # Paging covers the same newest-WQ_MAX_RESULTS window as the export.
            last_page = max(1, -(-min(total_available, WQ_MAX_RESULTS) // WQ_PAGE_SIZE))
            page = _parse_page(params.get('page')) if action == 'search' else 1
            if page > last_page:
                return redirect(_wq_page_url(params, last_page), 302)

            offset = (page - 1) * WQ_PAGE_SIZE
            pq_rows = q.with_entities(*_wq_list_columns()).offset(offset).limit(WQ_PAGE_SIZE).all()

            # Pre-warm member cache in parallel (DB lookup, API fallback)
            prefetch_members(list({r.asking_mnis_id for r in pq_rows if r.asking_mnis_id}))

            for pq in pq_rows:
                r = _pq_result(pq)
                _add_answer_preview(r, pq.answer_len)
                results.append(r)

            if results:
                page_start, page_end = offset + 1, offset + len(results)
                pager = _wq_pager(params, page, last_page)

            # Group this page by Parliament heading for topic view
            heading_groups = {}
            for r in results:
                key = r.get('heading') or 'Other'
                heading_groups.setdefault(key, []).append(r)
            grouped_results = sorted(heading_groups.items(), key=lambda x: len(x[1]), reverse=True)

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
                           page_start=page_start,
                           page_end=page_end,
                           pager=pager,
                           results_cap=WQ_MAX_RESULTS,
                           searched=searched,
                           robots_meta='noindex, follow' if searched else 'index, follow',
                           last_pq_update=_last_pq_update(),
                           oldest_pq_date=_oldest_pq_date())


def _selected_results(items: list) -> list[dict]:
    """
    Full records for ticked questions, read from the archive by UIN (the page
    only carries UINs and truncated answers). Items whose UIN isn't in the
    archive fall back to the posted fields, for pages rendered before this change.
    """
    from hansard_archive.models import HaPQ
    items = [i for i in items if isinstance(i, dict)][:WQ_MAX_RESULTS]
    uins = [str(i.get('uin')) for i in items if i.get('uin')]
    by_uin = {pq.uin: pq for pq in HaPQ.query.filter(HaPQ.uin.in_(uins)).all()} if uins else {}
    prefetch_members(list({pq.asking_mnis_id for pq in by_uin.values() if pq.asking_mnis_id}))
    out = []
    for item in items:
        pq = by_uin.get(str(item.get('uin')))
        out.append(_pq_result(pq) if pq else item)
    return out


@hansard_bp.route('/questions/download_selected', methods=['POST'])
def download_selected():
    try:
        items = json.loads(request.form.get('items_json', '[]'))
    except Exception:
        items = []
    if not isinstance(items, list):
        items = []
    items = _selected_results(items)
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
            cw.writerow([r.get('uin', ''), _status_label(r), r.get('dept', ''),
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
        p.add_run(f"[{_status_label(r)}] [{r.get('dept', '')}] {r.get('name', '')} ({r.get('party', '')}, {r.get('role', '')})\n").bold = True
        meta = f"UIN: {r.get('uin', '')}  ·  Date Asked: {r.get('date', '')}"
        if r.get('answering_minister'):
            meta += f"  ·  Answered by: {r['answering_minister']}"
            if r.get('answering_minister_role'):
                meta += f" ({r['answering_minister_role']})"
        if r.get('date_answered'):
            meta += f" ({r['date_answered']})"
        p.add_run(meta + "\n")
        p.add_run(f"Question: {r.get('text', '')}")
        answer_html = r.get('answer_text', '')
        if answer_html:
            if '<table' in answer_html:
                p.add_run("\nAnswer:")
                _add_answer_to_doc(doc, answer_html)
            else:
                p.add_run(f"\nAnswer: {to_plain_text(answer_html)}")
        p_link = doc.add_paragraph()
        p_link.add_run("Link: ")
        add_hyperlink(p_link, r.get('url', ''), r.get('url', ''))
    b = io.BytesIO()
    doc.save(b)
    b.seek(0)
    resp = make_response(b.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename=WQ_selected_{filename_ts}.docx"
    return resp
