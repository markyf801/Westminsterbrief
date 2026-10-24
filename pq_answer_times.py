"""
pq_answer_times.py — /written-questions/answer-times: how quickly each
government department answered Commons written questions in a period.

Calculation, definitions and caching: hansard_archive/pq_timing.py.
Behind FEATURE_PQ_ANSWER_TIMES (hidden / beta / live, see feature_flags.py);
the sitemap lists the page only when the flag is live.
"""
import csv
import io
import logging
from urllib.parse import urlencode

from flask import Blueprint, abort, make_response, render_template, request
from sqlalchemy.exc import SQLAlchemyError

from extensions import db, limiter
from hansard_archive import pq_timing as pt

log = logging.getLogger(__name__)

pq_answer_times_bp = Blueprint("pq_answer_times", __name__)

PATH = "/written-questions/answer-times"
BASE_URL = "https://westminsterbrief.co.uk"


def _csv_cell(value: str) -> str:
    """Neutralise spreadsheet formulas (CSV injection): text starting with
    = + - @ or a tab/carriage return gets a leading apostrophe."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _csv_response(period: pt.Period, departments: list[dict]):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Department", "Named-day questions", "Named-day answered on time",
                "Named-day on time (%)", "Ordinary questions", "Ordinary within expected time",
                "Ordinary within expected time (%)", "Median working days to answer",
                "Period start", "Period end"])
    for d in departments:
        w.writerow([_csv_cell(d["department"]),
                    d["named"]["questions"], d["named"]["on_time"], d["named"]["pct"] if d["named"]["pct"] is not None else "",
                    d["ordinary"]["questions"], d["ordinary"]["on_time"],
                    d["ordinary"]["pct"] if d["ordinary"]["pct"] is not None else "",
                    d["median_days"] if d["median_days"] is not None else "",
                    period.start.isoformat(), period.end.isoformat()])
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="pq-answer-times-{period.start.isoformat()}-to-{period.end.isoformat()}.csv"')
    return resp


@pq_answer_times_bp.route(PATH)
@limiter.limit("60 per minute")
def answer_times():
    if not pt.answer_times_enabled():
        abort(404)
    today = pt.today()
    try:
        periods = pt.available_periods(today)
    except SQLAlchemyError as e:
        db.session.rollback()
        log.warning("answer times: periods unavailable: %s", str(e)[:120])
        periods = [pt.default_period(today)]
    period_key = request.args.get("period", "")
    if period_key:
        period = next((p for p in periods if p.key == period_key), None)
        if period is None:
            abort(404)
    else:
        period = periods[0]
    sort = request.args.get("sort", "named")
    if sort not in pt.SORTS:
        sort = "named"

    try:
        figures = pt.department_figures(period, today)
    except SQLAlchemyError as e:
        db.session.rollback()
        log.warning("answer times: figures unavailable: %s", str(e)[:120])
        figures = None
    ranked = pt.sort_departments(figures["departments"], sort) if figures else []
    main = [d for d in ranked if d["questions"] >= pt.MIN_QUESTIONS]
    small = [d for d in ranked if d["questions"] < pt.MIN_QUESTIONS]

    if request.args.get("format") == "csv" and figures:
        return _csv_response(period, ranked)

    def url_for_args(**changes) -> str:
        args = {"period": period.key if period is not periods[0] else "", "sort": sort if sort != "named" else ""}
        args.update(changes)
        args = {k: v for k, v in args.items() if v}
        return PATH + ("?" + urlencode(args) if args else "")

    from hansard import _last_pq_update
    meta_desc = ("How quickly each government department answered written questions in the House of "
                 "Commons: named-day questions answered on time and ordinary questions answered within "
                 "the expected time, calculated from Parliament's data.")
    is_default = not request.args
    return render_template(
        "pq_answer_times.html",
        period=period,
        periods=periods,
        sort=sort,
        main=main,
        small=small,
        figures=figures,
        min_questions=pt.MIN_QUESTIONS,
        benchmark=pt.BENCHMARK_PERCENT,
        lords_days=pt.LORDS_EXPECTED_WORKING_DAYS,
        ordinary_extra=pt.ORDINARY_EXTRA_WORKING_DAYS,
        validation=pt.validation_table(),
        committee_url=pt.PROCEDURE_COMMITTEE_URL,
        fmt_pct=pt.fmt_pct,
        fmt_date=pt.fmt_date,
        sort_url=lambda s: url_for_args(sort=s if s != "named" else ""),
        csv_url=url_for_args(format="csv"),
        archive_updated=_last_pq_update(),
        meta_desc=meta_desc,
        canonical_url=BASE_URL + PATH,
        robots_meta="index, follow" if is_default else "noindex, follow",
    )
