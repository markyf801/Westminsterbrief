"""
Blueprint for the stakeholder directory read-only user interface.

Routes:
    GET /directory              — landing page with search form and stats
    GET /directory/search       — search results (paginated, 25 per page)
    GET /directory/org/<id>     — organisation detail page
"""
import re
from flask import Blueprint, render_template, request, abort
from sqlalchemy import func
from extensions import db
from stakeholder_directory.models import Organisation, Alias, Engagement, Flag

directory_bp = Blueprint('directory', __name__, url_prefix='/directory')

PAGE_SIZE = 25

SOURCE_TYPE_LABELS = {
    'ministerial_meeting': 'Ministerial Meeting',
    'oral_evidence_committee': 'Oral Evidence',
    'written_evidence_committee': 'Written Evidence',
    'lobbying_register': 'Lobbying Register',
    'lobbying_register_client': 'Lobbying Client',
}

SOURCE_TYPE_COLOR = {
    'ministerial_meeting': 'badge-st-blue',
    'oral_evidence_committee': 'badge-st-purple',
    'written_evidence_committee': 'badge-st-teal',
    'lobbying_register': 'badge-st-orange',
    'lobbying_register_client': 'badge-st-grey',
}


def _engagement_stats(org_ids: list) -> dict:
    """Return {org_id: {count, latest, sources}} for the given org IDs."""
    if not org_ids:
        return {}
    rows = (
        db.session.query(
            Engagement.organisation_id,
            func.count(Engagement.id).label('n'),
            func.max(Engagement.engagement_date).label('latest'),
        )
        .filter(Engagement.organisation_id.in_(org_ids))
        .group_by(Engagement.organisation_id)
        .all()
    )
    stats = {r.organisation_id: {'count': r.n, 'latest': r.latest, 'sources': set()} for r in rows}
    src_rows = (
        db.session.query(Engagement.organisation_id, Engagement.source_type)
        .filter(Engagement.organisation_id.in_(org_ids))
        .distinct()
        .all()
    )
    for r in src_rows:
        if r.organisation_id in stats:
            stats[r.organisation_id]['sources'].add(r.source_type)
    return stats


@directory_bp.route('')
def index():
    total_orgs = db.session.query(func.count(Organisation.id)).scalar() or 0
    total_engs = db.session.query(func.count(Engagement.id)).scalar() or 0
    source_counts = (
        db.session.query(Engagement.source_type, func.count(Engagement.id).label('n'))
        .group_by(Engagement.source_type)
        .order_by(func.count(Engagement.id).desc())
        .all()
    )
    return render_template(
        'stakeholder_directory/index.html',
        total_orgs=total_orgs,
        total_engs=total_engs,
        source_counts=source_counts,
        source_labels=SOURCE_TYPE_LABELS,
        source_color=SOURCE_TYPE_COLOR,
    )


@directory_bp.route('/search')
def search():
    q = (request.args.get('q') or '').strip()
    sort = request.args.get('sort', 'engagement_count_desc')
    source_types_filter = request.args.getlist('source_types')
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    try:
        page = max(1, int(request.args.get('page') or 1))
    except (ValueError, TypeError):
        page = 1

    ctx = dict(
        q=q, sort=sort, source_types_filter=source_types_filter,
        date_from=date_from, date_to=date_to,
        source_labels=SOURCE_TYPE_LABELS, source_color=SOURCE_TYPE_COLOR,
        all_source_types=list(SOURCE_TYPE_LABELS.keys()),
        results=[], total=0, page=1, pages=0,
        error=None, did_you_mean=[],
    )

    if not q:
        return render_template('stakeholder_directory/results.html', **ctx)

    if len(q) < 3:
        ctx['error'] = 'Please enter at least 3 characters to search.'
        return render_template('stakeholder_directory/results.html', **ctx)

    q_lower = q.lower()

    # Stage 1 — exact match on canonical name or alias
    exact_canonical = {
        r[0] for r in db.session.query(Organisation.id)
        .filter(func.lower(Organisation.canonical_name) == q_lower).all()
    }
    exact_alias = {
        r[0] for r in db.session.query(Alias.organisation_id)
        .filter(func.lower(Alias.alias_name) == q_lower).all()
    }
    exact_ids = exact_canonical | exact_alias

    # Stage 2 — substring match on canonical name (not already exact)
    fuzzy_ids = {
        r[0] for r in db.session.query(Organisation.id)
        .filter(func.lower(Organisation.canonical_name).contains(q_lower)).all()
    } - exact_ids

    all_ids = list(exact_ids | fuzzy_ids)

    if not all_ids:
        from rapidfuzz import process as rfp
        all_names = [r[0] for r in db.session.query(Organisation.canonical_name).all()]
        ctx['did_you_mean'] = [m[0] for m in rfp.extract(q, all_names, limit=3, score_cutoff=45)]
        return render_template('stakeholder_directory/results.html', **ctx)

    # Apply source type filter
    if source_types_filter:
        qualified = {
            r[0] for r in db.session.query(Engagement.organisation_id)
            .filter(Engagement.source_type.in_(source_types_filter))
            .distinct().all()
        }
        all_ids = [i for i in all_ids if i in qualified]
        exact_ids = {i for i in exact_ids if i in qualified}
        if not all_ids:
            return render_template('stakeholder_directory/results.html', **ctx)

    # Apply date range filter
    if date_from or date_to:
        dq = db.session.query(Engagement.organisation_id).distinct()
        if date_from:
            dq = dq.filter(Engagement.engagement_date >= date_from)
        if date_to:
            dq = dq.filter(Engagement.engagement_date <= date_to)
        qualified_dates = {r[0] for r in dq.all()}
        all_ids = [i for i in all_ids if i in qualified_dates]
        exact_ids = {i for i in exact_ids if i in qualified_dates}
        if not all_ids:
            return render_template('stakeholder_directory/results.html', **ctx)

    orgs = {o.id: o for o in db.session.query(Organisation).filter(Organisation.id.in_(all_ids)).all()}
    stats = _engagement_stats(all_ids)

    results = []
    for oid in all_ids:
        org = orgs.get(oid)
        if org is None:
            continue
        s = stats.get(oid, {'count': 0, 'latest': None, 'sources': set()})
        results.append({
            'org': org,
            'engagement_count': s['count'],
            'latest_date': s['latest'],
            'source_types': sorted(s['sources']),
            'exact_match': oid in exact_ids,
        })

    if sort == 'most_recent':
        results.sort(key=lambda r: r['latest_date'] or '', reverse=True)
    elif sort == 'name_asc':
        results.sort(key=lambda r: r['org'].canonical_name.lower())
    else:  # engagement_count_desc: exact matches first, then by count
        results.sort(key=lambda r: (not r['exact_match'], -r['engagement_count']))

    total = len(results)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    ctx.update(
        results=results[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
        total=total, page=page, pages=pages,
    )
    return render_template('stakeholder_directory/results.html', **ctx)


@directory_bp.route('/org/<int:org_id>')
def organisation(org_id):
    org = db.session.get(Organisation, org_id)
    if org is None:
        abort(404)

    try:
        page = max(1, int(request.args.get('page') or 1))
    except (ValueError, TypeError):
        page = 1

    all_engs = (
        db.session.query(Engagement)
        .filter_by(organisation_id=org_id)
        .order_by(Engagement.engagement_date.desc())
        .all()
    )
    total_engs = len(all_engs)
    eng_pages = max(1, (total_engs + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, eng_pages)
    engs_page = all_engs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    source_breakdown = {}
    for e in all_engs:
        source_breakdown[e.source_type] = source_breakdown.get(e.source_type, 0) + 1

    flags = (
        db.session.query(Flag)
        .filter_by(organisation_id=org_id, resolved=False)
        .order_by(Flag.raised_at.desc())
        .all()
    )

    # Parse possible_duplicate flag details to build "may be related to" list
    related_orgs = []
    for flag in flags:
        if flag.flag_type != 'possible_duplicate':
            continue
        m = re.search(r'org_id=(\d+)', flag.detail)
        if not m:
            continue
        other_id = int(m.group(1))
        if any(r['org'].id == other_id for r in related_orgs):
            continue
        other_org = db.session.get(Organisation, other_id)
        if other_org is None:
            continue
        sim_m = re.search(r'similarity (\d+\.\d+)', flag.detail)
        sim = float(sim_m.group(1)) if sim_m else None
        related_orgs.append({'org': other_org, 'similarity': sim})

    return render_template(
        'stakeholder_directory/organisation.html',
        org=org,
        engagements=engs_page,
        total_engs=total_engs,
        eng_pages=eng_pages,
        eng_page=page,
        source_breakdown=source_breakdown,
        flags=flags,
        related_orgs=related_orgs,
        source_labels=SOURCE_TYPE_LABELS,
        source_color=SOURCE_TYPE_COLOR,
    )
