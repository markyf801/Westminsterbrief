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
import time

from flask import Blueprint, abort, current_app, render_template, request

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


@stats_catalogue_bp.before_request
def _check_enabled():
    if not _ENABLED:
        from flask import render_template as _rt
        return _rt("hansard_archive/stats_coming_soon.html")


@stats_catalogue_bp.route("/stats")
def catalogue_list():
    _t0 = time.monotonic()
    current_app.logger.info("[STATS_DIAG] catalogue_list entered")

    from extensions import db
    from hansard_archive.models import StatProducer, StatPublication
    from sqlalchemy import or_

    current_app.logger.info("[STATS_DIAG] imports done +%.3fs", time.monotonic() - _t0)

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
        q = q.order_by(StatPublication.last_seen_at.desc())

    current_app.logger.info("[STATS_DIAG] query built +%.3fs", time.monotonic() - _t0)

    _t_count = time.monotonic()
    total = q.count()
    current_app.logger.info("[STATS_DIAG] COUNT done (%d rows) +%.3fs (query %.3fs)",
                            total, time.monotonic() - _t0, time.monotonic() - _t_count)

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    _t_rows = time.monotonic()
    rows = q.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()
    current_app.logger.info("[STATS_DIAG] main rows done (%d rows) +%.3fs (query %.3fs)",
                            len(rows), time.monotonic() - _t0, time.monotonic() - _t_rows)

    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)

    _t_prod = time.monotonic()
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
    current_app.logger.info("[STATS_DIAG] producers done (%d) +%.3fs (query %.3fs)",
                            len(producers), time.monotonic() - _t0, time.monotonic() - _t_prod)

    current_app.logger.info("[STATS_DIAG] calling render_template +%.3fs", time.monotonic() - _t0)

    result = render_template(
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

    current_app.logger.info("[STATS_DIAG] render_template done, returning +%.3fs", time.monotonic() - _t0)
    return result


@stats_catalogue_bp.route("/stats/<producer_slug>/<pub_slug>")
def catalogue_detail(producer_slug, pub_slug):
    from hansard_archive.models import StatProducer, StatPublication

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

    return render_template(
        "stats_catalogue_detail.html",
        pub=pub,
        producer=producer,
        cadence_label=_CADENCE_LABELS.get(pub.update_cadence, "—"),
    )
