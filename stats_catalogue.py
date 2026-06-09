"""
stats_catalogue — public catalogue of statistical publications.

Routes:
  GET /stats                              list page (paginated, filterable, sortable)
  GET /stats/<producer_slug>/<pub_slug>   detail page

Gated by the STATS_CATALOGUE_ENABLED env var. Both routes return 404 when the
var is absent or falsy, so the code ships on master without exposing the page on
production. The same flag drives nav link visibility via the
inject_stats_catalogue_flag context processor in flask_app.py.
"""

from __future__ import annotations

import os

from flask import Blueprint, abort, render_template, request

stats_catalogue_bp = Blueprint("stats_catalogue", __name__)

_ENABLED = bool(os.environ.get("STATS_CATALOGUE_ENABLED"))

_CADENCE_LABELS: dict[str | None, str] = {
    "daily":      "Daily",
    "weekly":     "Weekly",
    "monthly":    "Monthly",
    "quarterly":  "Quarterly",
    "annual":     "Annual",
    "biennial":   "Biennial",
    "ad_hoc":     "Ad hoc",
    "one_off":    "One-off",
    None:         "—",   # em dash — classifier couldn't determine
}

_CADENCE_OPTIONS = [
    ("",          "All cadences"),
    ("annual",    "Annual"),
    ("quarterly", "Quarterly"),
    ("monthly",   "Monthly"),
    ("weekly",    "Weekly"),
    ("daily",     "Daily"),
    ("biennial",  "Biennial"),
    ("ad_hoc",    "Ad hoc"),
    ("one_off",   "One-off"),
    ("none",      "Unknown"),
]

_PER_PAGE = 25

# producer_type → human label for the per-producer page context sentence
# (article baked in so the sentence reads "{name} is {label} that publishes…").
_PRODUCER_TYPE_LABELS = {
    "central_department":      "a central government department",
    "executive_agency":        "an executive agency",
    "ndpb":                    "a non-departmental public body",
    "regulator":               "a regulator",
    "gss_producer":            "a national statistics producer",
    "other_public_body":       "a public body",
    "devolved_administration": "a devolved administration",
    "devolved_body":           "a devolved public body",
}


@stats_catalogue_bp.before_request
def _check_enabled():
    if not _ENABLED:
        from flask import render_template as _rt
        return _rt("hansard_archive/stats_coming_soon.html")


@stats_catalogue_bp.route("/stats")
def catalogue_list():
    from extensions import db
    from hansard_archive.models import StatProducer, StatPublication
    from sqlalchemy import or_

    q = (
        db.session.query(StatPublication, StatProducer)
        .join(StatProducer, StatPublication.producer_id == StatProducer.id)
        .filter(
            StatProducer.authorisation_status == "authorised",
            StatPublication.authorisation_status.in_(["candidate", "authorised"]),
        )
    )

    search = request.args.get("q", "").strip()
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            StatPublication.name.ilike(like),
            StatPublication.description.ilike(like),
        ))

    producer_filter = request.args.get("producer", "").strip()
    if producer_filter:
        q = q.filter(StatProducer.slug == producer_filter)

    cadence_filter = request.args.get("cadence", "").strip()
    if cadence_filter == "none":
        q = q.filter(StatPublication.update_cadence.is_(None))
    elif cadence_filter:
        q = q.filter(StatPublication.update_cadence == cadence_filter)

    sort = request.args.get("sort", "newest")
    if sort == "name":
        q = q.order_by(StatPublication.name.asc())
    else:
        sort = "newest"
        # Newest-first by source publication date; undated rows last, stable by id.
        q = q.order_by(
            StatPublication.first_published_at.desc().nullslast(),
            StatPublication.id.desc(),
        )

    total = q.count()

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    rows = q.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()

    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    producers = (
        db.session.query(StatProducer)
        .join(StatPublication, StatPublication.producer_id == StatProducer.id)
        .filter(
            StatProducer.authorisation_status == "authorised",
            StatPublication.authorisation_status.in_(["candidate", "authorised"]),
        )
        .distinct()
        .order_by(StatProducer.name.asc())
        .all()
    )

    return render_template(
        "stats_catalogue.html",
        rows=rows,
        total=total,
        page=page,
        total_pages=total_pages,
        search=search,
        sort=sort,
        producer_filter=producer_filter,
        cadence_filter=cadence_filter,
        producers=producers,
        cadence_options=_CADENCE_OPTIONS,
        cadence_labels=_CADENCE_LABELS,
    )


@stats_catalogue_bp.route("/stats/<producer_slug>/<pub_slug>")
def catalogue_detail(producer_slug, pub_slug):
    from extensions import db
    from hansard_archive.models import (
        StatProducer, StatPublication, StatPublicationTheme,
        THEME_TYPE_POLICY_AREA, THEME_TYPE_SPECIFIC,
    )

    producer = StatProducer.query.filter_by(
        slug=producer_slug,
        authorisation_status="authorised",
    ).first_or_404()

    pub = StatPublication.query.filter_by(
        producer_id=producer.id,
        slug=pub_slug,
    ).first_or_404()

    if pub.authorisation_status not in ("candidate", "authorised"):
        abort(404)

    all_themes      = pub.themes.all()
    policy_areas    = sorted({t.theme for t in all_themes if t.theme_type == THEME_TYPE_POLICY_AREA})
    specific_themes = sorted({t.theme for t in all_themes if t.theme_type == THEME_TYPE_SPECIFIC})

    # Related: same producer + at least one shared policy area, up to 8
    related_publications = []
    if policy_areas:
        related_publications = (
            db.session.query(StatPublication)
            .join(StatPublicationTheme,
                  StatPublicationTheme.publication_id == StatPublication.id)
            .filter(
                StatPublication.producer_id == pub.producer_id,
                StatPublication.id != pub.id,
                StatPublication.authorisation_status.in_(["candidate", "authorised"]),
                StatPublicationTheme.theme_type == THEME_TYPE_POLICY_AREA,
                StatPublicationTheme.theme.in_(policy_areas),
            )
            .distinct()
            .order_by(
                StatPublication.first_published_at.desc().nullslast(),
                StatPublication.id.desc(),
            )
            .limit(8)
            .all()
        )

    return render_template(
        "stats_catalogue_detail.html",
        pub=pub,
        producer=producer,
        cadence_label=_CADENCE_LABELS.get(pub.update_cadence, "—"),
        policy_areas=policy_areas,
        specific_themes=specific_themes,
        related_publications=related_publications,
    )


@stats_catalogue_bp.route("/stats/producer/<slug>")
def producer_page(slug):
    from extensions import db
    from hansard_archive.models import StatProducer, StatPublication
    from sqlalchemy import func

    producer = StatProducer.query.filter_by(
        slug=slug, authorisation_status="authorised",
    ).first_or_404()

    _visible = StatPublication.authorisation_status.in_(["candidate", "authorised"])
    base_q = (
        db.session.query(StatPublication)
        .filter(StatPublication.producer_id == producer.id, _visible)
    )

    total = base_q.count()
    if total == 0:
        abort(404)   # thin-content gate: no page for an unpopulated producer

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    rows = (
        base_q.order_by(
            StatPublication.first_published_at.desc().nullslast(),
            StatPublication.id.desc(),
        )
        .offset((page - 1) * _PER_PAGE)
        .limit(_PER_PAGE)
        .all()
    )
    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    # Date span for the fallback context sentence (min/max ignore nulls).
    date_min, date_max = (
        db.session.query(
            func.min(StatPublication.first_published_at),
            func.max(StatPublication.first_published_at),
        )
        .filter(StatPublication.producer_id == producer.id, _visible)
        .one()
    )

    return render_template(
        "stats_producer.html",
        producer=producer,
        type_label=_PRODUCER_TYPE_LABELS.get(producer.producer_type, "a public body"),
        rows=rows,
        total=total,
        page=page,
        total_pages=total_pages,
        date_min=date_min,
        date_max=date_max,
        cadence_labels=_CADENCE_LABELS,
    )
