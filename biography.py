"""
Member Profiles — /biography (finder), /members/<id>/<slug> (indexable profile
page), the Word download, the member autocomplete and sitemap-members.xml.

Everything here reads from our own database (member_profile, member_biography,
cached_member). Nothing calls Parliament or an AI model on page view: profiles
and biographies are refreshed offline by scripts/refresh_members.py. The only
outbound request left is the optional member photo in the Word download,
limited to Parliament's own image host with a short timeout.
"""

import io
import logging
import re
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

from flask import Blueprint, abort, jsonify, make_response, redirect, render_template, request, send_file

from cache_models import MemberProfile
from extensions import db, limiter
from member_profiles import (
    AI_LABEL, BASE_URL, BOLD_RE, BULLET_RE, HEADING_RE, fetch_member_photo, get_biography,
    get_profile, member_header, official_links, profile_path, render_bio_html, search_members,
)

try:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor
except ImportError:
    Document = None

log = logging.getLogger(__name__)

biography_bp = Blueprint('biography', __name__)


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _fmt_date(dt) -> str:
    return f"{dt.day} {dt.strftime('%B %Y')}" if dt else ''


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
    c = OxmlElement('w:color')
    c.set(qn('w:val'), '0000FF')
    rPr.append(c)
    u = OxmlElement('w:u')
    u.set(qn('w:val'), 'single')
    rPr.append(u)
    new_run.append(rPr)
    t = OxmlElement('w:t')
    t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def build_profile_docx(header: dict, bio) -> io.BytesIO:
    """Word profile from stored data: header, stored AI biography (labelled), sources, links."""
    doc = Document()

    def compact(para, before=0, after=3):
        pPr = para._p.get_or_add_pPr()
        existing = pPr.find(qn('w:spacing'))
        if existing is not None:
            pPr.remove(existing)
        sp = OxmlElement('w:spacing')
        sp.set(qn('w:before'), str(before * 20))
        sp.set(qn('w:after'), str(after * 20))
        pPr.append(sp)

    def small_grey(para):
        for run in para.runs:
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(110, 110, 110)

    table = doc.add_table(rows=1, cols=2)
    tblPr = table._tbl.find(qn('w:tblPr'))
    borders = OxmlElement('w:tblBorders')
    for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        el = OxmlElement(f'w:{side}')
        el.set(qn('w:val'), 'none')
        borders.append(el)
    tblPr.append(borders)
    photo_cell, detail_cell = table.cell(0, 0), table.cell(0, 1)
    photo_cell.width, detail_cell.width = Inches(1.5), Inches(5.0)
    img = fetch_member_photo(header.get('photo_url', ''))
    if img:
        try:
            photo_cell.paragraphs[0].add_run().add_picture(img, width=Inches(1.3))
        except Exception as exc:  # python-docx rejects some image formats
            log.info('[biography] photo not embedded: %s', exc.__class__.__name__)

    name_para = detail_cell.paragraphs[0]
    name_run = name_para.add_run(header.get('name', 'Unknown Member'))
    name_run.bold = True
    name_run.font.size = Pt(16)
    compact(name_para, after=4)
    meta = detail_cell.add_paragraph('  ·  '.join(p for p in (header.get('party'), header.get('role')) if p))
    compact(meta)
    if header.get('current_role'):
        compact(detail_cell.add_paragraph(header['current_role']))
    updated = detail_cell.add_paragraph(f"Member details last updated {header.get('last_updated') or 'unknown'} · "
                                        f"downloaded {datetime.now().strftime('%d %B %Y')}")
    small_grey(updated)
    compact(updated, after=0)

    if bio is not None:
        h = doc.add_heading('Profile', level=1)
        compact(h, before=12, after=2)
        label = doc.add_paragraph(f"{AI_LABEL}. Generated {_fmt_date(bio.generated_at)} by "
                                  f"{bio.model_used} from the sources listed below.")
        small_grey(label)
        compact(label, after=6)
        for line in bio.bio_text.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            hm = HEADING_RE.match(line)
            if hm:
                compact(doc.add_heading(hm.group(1).strip(), level=2), before=10, after=2)
                continue
            bm = BULLET_RE.match(line)
            clean = BOLD_RE.sub(r'\1', bm.group(1) if bm else line).strip()
            p = doc.add_paragraph(clean, style='List Bullet' if bm else None)
            compact(p, after=2 if bm else 3)
            for run in p.runs:
                run.font.size = Pt(10)
        if bio.sources:
            compact(doc.add_heading('Sources', level=2), before=10, after=2)
            for src in bio.sources:
                p = doc.add_paragraph(style='List Bullet')
                _add_hyperlink(p, src['url'], src['label'])
                compact(p, after=2)
    else:
        p = doc.add_paragraph('No AI-generated profile is stored for this member yet. '
                              'The official Parliament links below have the full record.')
        compact(p, before=10)

    compact(doc.add_heading('Official record', level=2), before=10, after=2)
    for link in official_links(header['id']):
        p = doc.add_paragraph(style='List Bullet')
        _add_hyperlink(p, link['url'], link['label'])
        compact(p, after=2)
    p = doc.add_paragraph()
    _add_hyperlink(p, f"{BASE_URL}{header['profile_url']}", 'Westminster Brief member profile')
    compact(p, before=6)

    mem = io.BytesIO()
    doc.save(mem)
    mem.seek(0)
    return mem


def _docx_response(member_id: int):
    if not Document:
        return "Word library missing.", 500
    header = member_header(member_id)
    if not header:
        abort(404)
    mem = build_profile_docx(header, get_biography(member_id))
    safe_name = re.sub(r'[^\w\s-]', '', header['name'])[:40].strip() or 'Member'
    resp = send_file(
        mem, as_attachment=True, download_name=f"Profile - {safe_name}.docx",
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
    resp.headers['X-Robots-Tag'] = 'noindex'
    return resp


def _member_id_arg(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

@biography_bp.route('/biography', methods=['GET', 'POST'])
def biography_home():
    """Member finder. Selecting a member goes to their profile page."""
    error = None
    raw = request.form.get('member_id') if request.method == 'POST' else request.args.get('member_id')
    if raw:
        member_id = _member_id_arg(raw)
        header = member_header(member_id) if member_id else {}
        if header:
            return redirect(header['profile_url'], code=303 if request.method == 'POST' else 301)
        error = "We don't hold details for that member yet — they are added by the weekly refresh."
    return render_template('biography.html', error_message=error)


@biography_bp.route('/members/<int:member_id>')
def member_profile_redirect(member_id):
    header = member_header(member_id)
    if not header:
        abort(404)
    return redirect(header['profile_url'], code=301)


@biography_bp.route('/members/<int:member_id>/<slug>')
def member_profile(member_id, slug):
    header = member_header(member_id)
    if not header:
        abort(404)
    if header['profile_url'] != f'/members/{member_id}/{slug}':
        return redirect(header['profile_url'], code=301)

    profile = get_profile(member_id)
    bio = get_biography(member_id)
    canonical = f"{BASE_URL}{header['profile_url']}"
    house_name = 'House of Lords' if header['is_lord'] else 'House of Commons'
    job_title = header['current_role'] or header['role']
    same_as = [f"https://members.parliament.uk/member/{member_id}"]
    if profile is not None and profile.wikipedia_url:
        same_as.append(profile.wikipedia_url)
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": header['name'],
        "url": canonical,
        "jobTitle": job_title,
        "memberOf": {"@type": "Organization", "name": house_name},
        "sameAs": same_as,
    }
    if header['party']:
        json_ld["affiliation"] = {"@type": "Organization", "name": header['party']}
    if header['photo_url']:
        json_ld["image"] = header['photo_url']

    party_bit = f"{header['party']} " if header['party'] else ''
    meta_desc = (f"{header['name']}, {party_bit}{header['role']}. "
                 f"{'Current role: ' + header['current_role'] + '. ' if header['current_role'] else ''}"
                 "Career, government and committee roles, registered interests and recent "
                 "parliamentary activity, with links to the official record.")[:300]
    indexable = header['stored'] and header['is_current'] and profile is not None \
        and profile.details_refreshed_at is not None

    return render_template(
        'member_profile.html',
        header=header,
        profile=profile,
        bio=bio,
        bio_html=render_bio_html(bio.bio_text) if bio else '',
        bio_generated=_fmt_date(bio.generated_at) if bio else '',
        ai_label=AI_LABEL,
        links=official_links(member_id),
        canonical_url=canonical,
        json_ld=json_ld,
        meta_desc=meta_desc,
        robots_meta='index, follow' if indexable else 'noindex, follow',
    )


@biography_bp.route('/members/<int:member_id>/profile.docx')
def member_profile_docx(member_id):
    return _docx_response(member_id)


@biography_bp.route('/api/search_members')
def api_search_members():
    return jsonify({"results": search_members(request.args.get('q', ''))})


@biography_bp.route("/api/biography", methods=["POST"])
@limiter.limit("30 per minute")
def api_biography():
    """Stored biography for a member (no generation on request — see scripts/refresh_members.py)."""
    data = request.get_json(silent=True) or {}
    member_id = _member_id_arg(data.get("member_id"))
    bio = get_biography(member_id) if member_id else None
    if bio is None:
        return jsonify({"biography": "", "available": False})
    return jsonify({
        "biography": bio.bio_text, "available": True, "label": AI_LABEL,
        "generated_at": bio.generated_at.strftime('%Y-%m-%d') if bio.generated_at else '',
        "model_used": bio.model_used, "sources": bio.sources or [],
    })


@biography_bp.route('/export_biography_word', methods=['POST'])
def export_biography_word():
    """Legacy form target — the document is now built from stored data, not posted text."""
    member_id = _member_id_arg(request.form.get('member_id'))
    if not member_id:
        abort(400)
    return _docx_response(member_id)


@biography_bp.route('/sitemap-members.xml')
def sitemap_members():
    try:
        rows = (db.session.query(MemberProfile.member_id, MemberProfile.slug,
                                 MemberProfile.updated_at)
                .filter(MemberProfile.is_current.is_(True),
                        MemberProfile.details_refreshed_at.isnot(None))
                .order_by(MemberProfile.member_id).all())
    except Exception as exc:  # table missing before the migration: serve an empty urlset
        db.session.rollback()
        log.warning('[sitemap-members] %s', exc.__class__.__name__)
        rows = []
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for member_id, slug, refreshed in rows:
        lines.append('  <url>')
        lines.append(f'    <loc>{xml_escape(BASE_URL + profile_path(member_id, slug))}</loc>')
        if refreshed:
            lines.append(f'    <lastmod>{refreshed.date().isoformat()}</lastmod>')
        lines.append('  </url>')
    lines.append('</urlset>')
    resp = make_response('\n'.join(lines))
    resp.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return resp
