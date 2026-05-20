"""
Westminster Brief — Stats State Checker
========================================
Read-only diagnostic script for Phase 1 + Phase 1.5 stats tables.
Produces a summary of row counts, period parsing health, mirror sync
between HeadlineStat legacy fields and the latest StatObservation, and
backfill/policy-area integrity. Safe to run on production at any time.

Usage:
  python scripts/check_stats_state.py          # human-readable
  python scripts/check_stats_state.py --json   # structured JSON
  flask stats check-state                      # via Flask CLI
  flask stats check-state --json               # JSON via Flask CLI

No writes are performed. No external API calls are made.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


# ---------------------------------------------------------------------------
# Core collection logic — all queries live here, no Flask import required
# ---------------------------------------------------------------------------

def collect_stats(db_session) -> dict:
    """
    Run all diagnostic queries and return a structured result dict.
    Accepts any SQLAlchemy session — works with Flask-SQLAlchemy or a
    plain session, making it testable without importing flask_app.

    Read-only: no flushes, no commits, no writes of any kind.
    """
    from hansard_archive.models import (
        HeadlineStat, StatObservation, StatPolicyArea, StatDefinition,
    )
    from hansard_archive.stats_queries import get_latest_observation
    from sqlalchemy import func, distinct as sa_distinct

    # ── Section 1: Row counts ────────────────────────────────────────────────

    hs_total      = db_session.query(func.count(HeadlineStat.id)).scalar() or 0
    hs_with_value = (
        db_session.query(func.count(HeadlineStat.id))
        .filter(HeadlineStat.latest_value.isnot(None))
        .scalar() or 0
    )

    obs_total = db_session.query(func.count(StatObservation.id)).scalar() or 0
    obs_real  = (
        db_session.query(func.count(StatObservation.id))
        .filter(
            StatObservation.period_start.isnot(None),
            StatObservation.period_end.isnot(None),
        )
        .scalar() or 0
    )

    spa_total   = db_session.query(func.count(StatPolicyArea.id)).scalar() or 0
    spa_primary = (
        db_session.query(func.count(StatPolicyArea.id))
        .filter(StatPolicyArea.is_primary.is_(True))
        .scalar() or 0
    )

    sd_total = db_session.query(func.count(StatDefinition.id)).scalar() or 0

    section1 = {
        "headline_stat_total":          hs_total,
        "headline_stat_with_value":     hs_with_value,
        "headline_stat_null_value":     hs_total - hs_with_value,
        "stat_observation_total":       obs_total,
        "stat_observation_real":        obs_real,
        "stat_observation_placeholder": obs_total - obs_real,
        "stat_policy_area_total":       spa_total,
        "stat_policy_area_primary":     spa_primary,
        "stat_definition_total":        sd_total,
    }

    # ── Section 2: StatObservation period parsing by source_type ─────────────

    source_types = sorted(
        r[0] for r in
        db_session.query(sa_distinct(HeadlineStat.source_type)).all()
        if r[0]
    )

    by_source_type: dict[str, dict] = {}
    for st in source_types:
        # Aggregate: count, date range
        agg = (
            db_session.query(
                func.count(StatObservation.id),
                func.min(StatObservation.period_start),
                func.max(StatObservation.period_end),
            )
            .join(HeadlineStat, StatObservation.headline_stat_id == HeadlineStat.id)
            .filter(
                HeadlineStat.source_type == st,
                StatObservation.period_start.isnot(None),
                StatObservation.period_end.isnot(None),
            )
            .first()
        )
        count, earliest, latest_end = (agg or (0, None, None))

        straddles = (
            db_session.query(func.count(StatObservation.id))
            .join(HeadlineStat, StatObservation.headline_stat_id == HeadlineStat.id)
            .filter(
                HeadlineStat.source_type == st,
                StatObservation.period_start.isnot(None),
                StatObservation.period_end.isnot(None),
                StatObservation.straddles_cutoff.is_(True),
            )
            .scalar() or 0
        )

        distinct_labels = (
            db_session.query(
                func.count(sa_distinct(StatObservation.period_label))
            )
            .join(HeadlineStat, StatObservation.headline_stat_id == HeadlineStat.id)
            .filter(
                HeadlineStat.source_type == st,
                StatObservation.period_start.isnot(None),
                StatObservation.period_end.isnot(None),
            )
            .scalar() or 0
        )

        by_source_type[st] = {
            "count":                count or 0,
            "earliest_period_start": earliest,
            "latest_period_end":     latest_end,
            "straddles_count":       straddles,
            "distinct_period_labels": distinct_labels,
        }

    section2 = {"by_source_type": by_source_type}

    # ── Section 3: Mirror sync ───────────────────────────────────────────────
    # For each HeadlineStat with at least one real observation, compare the
    # five legacy fields against the latest StatObservation.

    real_obs_stat_ids = [
        r[0] for r in
        db_session.query(sa_distinct(StatObservation.headline_stat_id))
        .filter(
            StatObservation.period_start.isnot(None),
            StatObservation.period_end.isnot(None),
        )
        .all()
    ]

    mismatch_details: list[dict] = []
    fully_matched = 0

    if real_obs_stat_ids:
        hs_rows = (
            db_session.query(HeadlineStat)
            .filter(HeadlineStat.id.in_(real_obs_stat_ids))
            .all()
        )
        for hs in hs_rows:
            latest = get_latest_observation(hs)
            if latest is None:
                # Only straddling observations exist — include them for comparison
                latest = get_latest_observation(hs, include_straddling=True)
            if latest is None:
                continue

            differing: list[str] = []
            if hs.latest_value != latest.value:
                differing.append("latest_value")
            if hs.period_label != latest.period_label:
                differing.append("period_label")
            if hs.release_date != latest.release_date:
                differing.append("release_date")
            if hs.source_url != latest.release_url:
                differing.append("source_url")
            if hs.source_wording != latest.source_wording:
                differing.append("source_wording")

            if differing:
                mismatch_details.append({
                    "headline_stat_id": hs.id,
                    "fields": differing,
                })
            else:
                fully_matched += 1

    nonnull_plain_english = (
        db_session.query(func.count(HeadlineStat.id))
        .filter(HeadlineStat.plain_english.isnot(None))
        .scalar() or 0
    )

    section3 = {
        "stats_checked":            len(real_obs_stat_ids),
        "fully_matched":            fully_matched,
        "mismatched":               len(mismatch_details),
        "mismatch_details":         mismatch_details,
        "nonnull_plain_english_on_hs": nonnull_plain_english,
    }

    # ── Section 4: Backfill integrity ────────────────────────────────────────

    stats_with_primary_spa = (
        db_session.query(func.count(sa_distinct(StatPolicyArea.headline_stat_id)))
        .filter(StatPolicyArea.is_primary.is_(True))
        .scalar() or 0
    )

    stats_with_any_obs_ids = {
        r[0] for r in
        db_session.query(sa_distinct(StatObservation.headline_stat_id)).all()
    }
    stats_with_any_obs = len(stats_with_any_obs_ids)

    # Stats with latest_value but no observation — expected to be zero.
    all_hs = db_session.query(HeadlineStat.id, HeadlineStat.latest_value).all()
    anomalous_no_obs = [
        row.id for row in all_hs
        if row.latest_value is not None and row.id not in stats_with_any_obs_ids
    ]

    section4 = {
        "total_stats":                   hs_total,
        "with_primary_policy_area":      stats_with_primary_spa,
        "missing_primary_policy_area":   hs_total - stats_with_primary_spa,
        "with_any_observation":          stats_with_any_obs,
        "with_no_observation":           hs_total - stats_with_any_obs,
        "anomalous_no_obs_with_value":   anomalous_no_obs,
    }

    # ── Section 5: Cross-policy-area stats ───────────────────────────────────

    multi_rows = (
        db_session.query(
            StatPolicyArea.headline_stat_id,
            func.count(StatPolicyArea.id).label("area_count"),
        )
        .group_by(StatPolicyArea.headline_stat_id)
        .having(func.count(StatPolicyArea.id) > 1)
        .all()
    )

    multi_details: list[dict] = []
    for stat_id, _ in multi_rows:
        areas = (
            db_session.query(StatPolicyArea)
            .filter_by(headline_stat_id=stat_id)
            .order_by(StatPolicyArea.is_primary.desc(), StatPolicyArea.theme_slug)
            .all()
        )
        multi_details.append({
            "headline_stat_id": stat_id,
            "areas": [
                {"theme_slug": a.theme_slug, "is_primary": a.is_primary}
                for a in areas
            ],
        })

    section5 = {
        "multi_policy_area_count": len(multi_details),
        "details": multi_details,
    }

    # ── Anomalies summary ────────────────────────────────────────────────────

    anomalies: list[str] = []

    if section4["missing_primary_policy_area"] > 0:
        anomalies.append(
            f"{section4['missing_primary_policy_area']} HeadlineStat(s) have no "
            "primary StatPolicyArea row"
        )
    if anomalous_no_obs:
        ids_str = ", ".join(str(i) for i in anomalous_no_obs[:10])
        suffix = f" (first 10: {ids_str})" if len(anomalous_no_obs) > 10 else f": {ids_str}"
        anomalies.append(
            f"{len(anomalous_no_obs)} stat(s) have latest_value but no StatObservation{suffix}"
        )
    if mismatch_details:
        anomalies.append(
            f"{len(mismatch_details)} HeadlineStat(s) have legacy fields out of sync "
            "with latest observation"
        )
    if nonnull_plain_english > 0:
        anomalies.append(
            f"{nonnull_plain_english} HeadlineStat(s) have non-null plain_english — "
            "expected NULL in Phase 1.5"
        )

    return {
        "section1": section1,
        "section2": section2,
        "section3": section3,
        "section4": section4,
        "section5": section5,
        "anomalies": anomalies,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _hr(char: str = "-", width: int = 68) -> str:
    return char * width


def format_text(data: dict) -> str:
    lines: list[str] = []
    out = lines.append

    out(_hr("="))
    out("Westminster Brief -- Stats State Check")
    out(_hr("="))
    out("")

    # Section 1
    s1 = data["section1"]
    out("SECTION 1: ROW COUNTS")
    out(_hr())
    out(f"HeadlineStat")
    out(f"  Total:                         {s1['headline_stat_total']}")
    out(f"  With legacy latest_value:      {s1['headline_stat_with_value']}")
    out(f"  With null latest_value:        {s1['headline_stat_null_value']}")
    out(f"StatObservation")
    out(f"  Total:                         {s1['stat_observation_total']}")
    out(f"  Real (non-null period):        {s1['stat_observation_real']}")
    out(f"  Placeholder (null period):     {s1['stat_observation_placeholder']}")
    out(f"StatPolicyArea")
    out(f"  Total:                         {s1['stat_policy_area_total']}")
    out(f"  Where is_primary=True:         {s1['stat_policy_area_primary']}")
    out(f"StatDefinition")
    out(f"  Total:                         {s1['stat_definition_total']}")
    out("")

    # Section 2
    s2 = data["section2"]
    out("SECTION 2: STATOBSERVATION PERIOD PARSING (real observations only)")
    out(_hr())
    if not s2["by_source_type"]:
        out("  No real observations found.")
    else:
        for st, d in s2["by_source_type"].items():
            out(f"  source_type: {st}")
            out(f"    Count:                     {d['count']}")
            start = d["earliest_period_start"]
            end   = d["latest_period_end"]
            out(f"    Period range:              {_fmt_date(start)} to {_fmt_date(end)}")
            out(f"    Straddles cutoff:          {d['straddles_count']}")
            out(f"    Distinct period_labels:    {d['distinct_period_labels']}")
    out("")

    # Section 3
    s3 = data["section3"]
    out("SECTION 3: MIRROR SYNC — HeadlineStat vs latest StatObservation")
    out(_hr())
    out(f"  Stats checked:                 {s3['stats_checked']}")
    out(f"  Fully matched:                 {s3['fully_matched']}")
    out(f"  Any field mismatched:          {s3['mismatched']}")
    if s3["mismatch_details"]:
        out("  Mismatch details:")
        for m in s3["mismatch_details"]:
            out(f"    headline_stat_id={m['headline_stat_id']}: {', '.join(m['fields'])}")
    pe = s3["nonnull_plain_english_on_hs"]
    out(f"  Non-null plain_english on HS:  {pe}" +
        (" [ANOMALY — expected NULL]" if pe > 0 else " (expected)"))
    out("")

    # Section 4
    s4 = data["section4"]
    out("SECTION 4: BACKFILL INTEGRITY")
    out(_hr())
    out(f"  Total HeadlineStats:           {s4['total_stats']}")
    out(f"  With primary StatPolicyArea:   {s4['with_primary_policy_area']}")
    missing = s4["missing_primary_policy_area"]
    out(f"  Missing primary PolicyArea:    {missing}" +
        (" [ANOMALY]" if missing > 0 else ""))
    out(f"  With any StatObservation:      {s4['with_any_observation']}")
    out(f"  With no StatObservation:       {s4['with_no_observation']}")
    anom = s4["anomalous_no_obs_with_value"]
    if anom:
        out(f"  Has value but no obs [ANOMALY]: IDs {anom}")
    out("")

    # Section 5
    s5 = data["section5"]
    out("SECTION 5: CROSS-POLICY-AREA STATS")
    out(_hr())
    out(f"  Stats with >1 policy area:     {s5['multi_policy_area_count']}")
    for d in s5["details"]:
        areas_str = ", ".join(
            f"{a['theme_slug']}{'*' if a['is_primary'] else ''}"
            for a in d["areas"]
        )
        out(f"    headline_stat_id={d['headline_stat_id']}: {areas_str}  (* = primary)")
    out("")

    # Section 6: Anomalies summary
    out("SECTION 6: ANOMALIES SUMMARY")
    out(_hr())
    if data["anomalies"]:
        for a in data["anomalies"]:
            out(f"  • {a}")
    else:
        out("  No anomalies detected.")
    out("")
    out(_hr("="))

    return "\n".join(lines)


def _fmt_date(d) -> str:
    if d is None:
        return "—"
    if isinstance(d, (date, datetime)):
        return d.isoformat()
    return str(d)


def _json_serial(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def format_json(data: dict) -> str:
    return json.dumps(data, indent=2, default=_json_serial)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_check(as_json: bool = False) -> dict:
    """
    Entry point for both direct invocation and Flask CLI.
    Imports flask_app, runs in an app context, prints output.
    Returns the data dict (useful for testing callers).
    """
    from flask_app import app, db
    with app.app_context():
        data = collect_stats(db.session)
    output = format_json(data) if as_json else format_text(data)
    print(output)
    return data


def register_stats_check_cli(stats_group) -> None:
    """
    Register `flask stats check-state` under the existing `stats` CLI group.
    Call from register_stats_cli() in stats_refresh.py.
    """
    import click

    @stats_group.command("check-state")
    @click.option("--json", "as_json", is_flag=True, help="Output structured JSON")
    def cli_check_state(as_json):
        """Read-only diagnostic check of Phase 1 + Phase 1.5 stats tables."""
        run_check(as_json=as_json)


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Read-only diagnostic check for Westminster Brief stats tables."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output structured JSON instead of human-readable text",
    )
    args = parser.parse_args()
    run_check(as_json=args.json)
