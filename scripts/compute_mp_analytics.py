"""
Nightly compute job: refreshes ha_mp_analytics for all Commons MPs.

Builds chamber-wide rankings and per-MP policy area distributions in a small
number of bulk SQL queries (not per-MP loops), then assembles the per-MP rows
in Python before upserting.

Usage:
    python scripts/compute_mp_analytics.py [--dry-run]

Railway cron: mp-analytics-cron
    Schedule:   0 3 * * *   (03:00 UTC daily, after member-cache-refresh)
    Start cmd:  python scripts/compute_mp_analytics.py

Environment:
    DATABASE_URL — Postgres (Railway Variable Reference); falls back to SQLite
"""

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")

from flask_app import app
from extensions import db
from hansard_archive.models import MpAnalytics
from hansard_archive.analytics_constants import (
    BASELINE_MIN_SESSIONS,
    HHI_FOCUSED,
    HHI_SPECIALIST,
    PROCEDURAL_NAME_PATTERNS,
    TIER_FULL,
    TIER_MINIMAL,
    VOICE_RANK_THRESHOLD,
    WINDOW_LONG_DAYS,
    WINDOW_SHORT_DAYS,
    WINDOW_PRIOR_DAYS,
)
from sqlalchemy import text


def _log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# Build parameterised NOT ILIKE exclusion SQL and params dict.
# Alias is the table alias used in the query (e.g. "hc" for ha_contribution).
def _excl_sql(alias: str = "hc") -> tuple[str, dict]:
    clauses = [f"{alias}.member_name NOT ILIKE :ep{i}" for i in range(len(PROCEDURAL_NAME_PATTERNS))]
    params = {f"ep{i}": f"%{p}%" for i, p in enumerate(PROCEDURAL_NAME_PATTERNS)}
    return " AND ".join(clauses), params


def _hhi(counts: list[int]) -> float:
    """Herfindahl-Hirschman Index: sum of squared proportions. Range 0–1."""
    total = sum(counts)
    if total == 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts)


def _specialism_label(score: float, top_areas: list[dict]) -> str:
    if score >= HHI_SPECIALIST and top_areas:
        return f"Specialist: {top_areas[0]['theme']}"
    if score >= HHI_FOCUSED and len(top_areas) >= 2:
        return f"Focused: {top_areas[0]['theme']} and {top_areas[1]['theme']}"
    return "Active across many policy areas"


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]}"


def run(dry_run: bool = False) -> None:
    with app.app_context():
        today = date.today()
        cutoff_12m = today - timedelta(days=WINDOW_LONG_DAYS)
        cutoff_3m  = today - timedelta(days=WINDOW_SHORT_DAYS)
        cutoff_9m  = today - timedelta(days=WINDOW_PRIOR_DAYS)
        now = datetime.utcnow()

        excl, ep = _excl_sql("hc")

        # ── 1. Policy area session counts for all members (one query) ───────────
        _log("Fetching policy area session counts (all members × all themes)…")
        pa_rows = db.session.execute(text(f"""
            SELECT hc.member_id, hst.theme, COUNT(DISTINCT hc.session_id) AS cnt
            FROM ha_contribution hc
            JOIN ha_session_theme hst ON hst.session_id = hc.session_id
            JOIN ha_session hs ON hs.id = hc.session_id
            WHERE hs.date >= :cutoff_12m
              AND hs.is_container IS FALSE
              AND hst.theme_type = 'policy_area'
              AND hc.member_id IS NOT NULL
              AND {excl}
            GROUP BY hc.member_id, hst.theme
        """), {"cutoff_12m": cutoff_12m, **ep}).fetchall()

        _log(f"  {len(pa_rows):,} member×policy_area rows")

        # Build lookup structures from this one result set
        theme_member_counts: dict[str, list[tuple[int, int]]] = defaultdict(list)
        member_pa_counts: dict[int, dict[str, int]] = defaultdict(dict)
        for member_id, theme, cnt in pa_rows:
            theme_member_counts[theme].append((member_id, cnt))
            member_pa_counts[member_id][theme] = cnt
        for theme in theme_member_counts:
            theme_member_counts[theme].sort(key=lambda x: -x[1])

        # ── 2. 12-month session counts — Commons MPs for chamber ranking ────────
        _log("Fetching Commons 12-month session counts…")
        commons_12m_rows = db.session.execute(text(f"""
            SELECT cm.member_id, COUNT(DISTINCT hc.session_id) AS sessions_12m
            FROM cached_member cm
            JOIN ha_contribution hc ON hc.member_id = cm.member_id
            JOIN ha_session hs ON hs.id = hc.session_id
            WHERE cm.house = 'Commons'
              AND hs.date >= :cutoff_12m
              AND hs.is_container IS FALSE
              AND {excl}
            GROUP BY cm.member_id
            HAVING COUNT(DISTINCT hc.session_id) >= :min_sess
            ORDER BY sessions_12m DESC
        """), {"cutoff_12m": cutoff_12m, "min_sess": BASELINE_MIN_SESSIONS, **ep}).fetchall()

        total_commons_ranked = len(commons_12m_rows)
        _log(f"  {total_commons_ranked} Commons MPs qualify for baseline ranking")

        commons_sessions_12m: dict[int, int] = {r[0]: r[1] for r in commons_12m_rows}
        commons_rank: dict[int, int] = {r[0]: i + 1 for i, r in enumerate(commons_12m_rows)}

        # ── 3. 3-month session counts (Commons) ─────────────────────────────────
        _log("Fetching 3-month session counts…")
        counts_3m_rows = db.session.execute(text(f"""
            SELECT cm.member_id, COUNT(DISTINCT hc.session_id) AS cnt
            FROM cached_member cm
            JOIN ha_contribution hc ON hc.member_id = cm.member_id
            JOIN ha_session hs ON hs.id = hc.session_id
            WHERE cm.house = 'Commons'
              AND hs.date >= :cutoff_3m
              AND hs.is_container IS FALSE
              AND {excl}
            GROUP BY cm.member_id
        """), {"cutoff_3m": cutoff_3m, **ep}).fetchall()
        member_sessions_3m: dict[int, int] = {r[0]: r[1] for r in counts_3m_rows}

        # ── 4. Total (all-time) session counts per Commons member ────────────────
        _log("Fetching all-time session counts…")
        total_rows = db.session.execute(text(f"""
            SELECT cm.member_id, COUNT(DISTINCT hc.session_id) AS cnt
            FROM cached_member cm
            JOIN ha_contribution hc ON hc.member_id = cm.member_id
            JOIN ha_session hs ON hs.id = hc.session_id
            WHERE cm.house = 'Commons'
              AND hs.is_container IS FALSE
              AND {excl}
            GROUP BY cm.member_id
        """), ep).fetchall()
        member_total: dict[int, int] = {r[0]: r[1] for r in total_rows}

        # ── 5. Tagged session coverage per member (12 months) ──────────────────
        _log("Fetching tagged-session coverage…")
        tagged_rows = db.session.execute(text(f"""
            SELECT hc.member_id, COUNT(DISTINCT hc.session_id) AS cnt
            FROM ha_contribution hc
            JOIN ha_session_theme hst ON hst.session_id = hc.session_id
            JOIN ha_session hs ON hs.id = hc.session_id
            JOIN cached_member cm ON cm.member_id = hc.member_id
            WHERE cm.house = 'Commons'
              AND hs.date >= :cutoff_12m
              AND hs.is_container IS FALSE
              AND {excl}
            GROUP BY hc.member_id
        """), {"cutoff_12m": cutoff_12m, **ep}).fetchall()
        member_tagged_12m: dict[int, int] = {r[0]: r[1] for r in tagged_rows}

        # ── 6. Debate type distribution (12 months, Commons) ────────────────────
        _log("Fetching debate type distributions…")
        dtype_rows = db.session.execute(text(f"""
            SELECT hc.member_id, hs.debate_type, COUNT(DISTINCT hc.session_id) AS cnt
            FROM ha_contribution hc
            JOIN ha_session hs ON hs.id = hc.session_id
            JOIN cached_member cm ON cm.member_id = hc.member_id
            WHERE cm.house = 'Commons'
              AND hs.date >= :cutoff_12m
              AND hs.is_container IS FALSE
              AND {excl}
            GROUP BY hc.member_id, hs.debate_type
        """), {"cutoff_12m": cutoff_12m, **ep}).fetchall()
        member_dtype: dict[int, dict] = defaultdict(dict)
        for member_id, dtype, cnt in dtype_rows:
            member_dtype[member_id][dtype or "other"] = cnt

        # ── 7. Recent policy area shifts ─────────────────────────────────────────
        # Policy areas in last 3 months
        _log("Fetching recent policy area shifts…")
        recent_pa_rows = db.session.execute(text(f"""
            SELECT hc.member_id, hst.theme
            FROM ha_contribution hc
            JOIN ha_session_theme hst ON hst.session_id = hc.session_id
            JOIN ha_session hs ON hs.id = hc.session_id
            JOIN cached_member cm ON cm.member_id = hc.member_id
            WHERE cm.house = 'Commons'
              AND hs.date >= :cutoff_3m
              AND hs.is_container IS FALSE
              AND hst.theme_type = 'policy_area'
              AND {excl}
            GROUP BY hc.member_id, hst.theme
        """), {"cutoff_3m": cutoff_3m, **ep}).fetchall()
        member_recent_pa: dict[int, set] = defaultdict(set)
        for member_id, theme in recent_pa_rows:
            member_recent_pa[member_id].add(theme)

        # Policy areas in prior period (3–12 months ago)
        prior_pa_rows = db.session.execute(text(f"""
            SELECT hc.member_id, hst.theme
            FROM ha_contribution hc
            JOIN ha_session_theme hst ON hst.session_id = hc.session_id
            JOIN ha_session hs ON hs.id = hc.session_id
            JOIN cached_member cm ON cm.member_id = hc.member_id
            WHERE cm.house = 'Commons'
              AND hs.date >= :cutoff_9m
              AND hs.date < :cutoff_3m
              AND hs.is_container IS FALSE
              AND hst.theme_type = 'policy_area'
              AND {excl}
            GROUP BY hc.member_id, hst.theme
        """), {"cutoff_9m": cutoff_9m, "cutoff_3m": cutoff_3m, **ep}).fetchall()
        member_prior_pa: dict[int, set] = defaultdict(set)
        for member_id, theme in prior_pa_rows:
            member_prior_pa[member_id].add(theme)

        # ── 8. All Commons members ───────────────────────────────────────────────
        all_commons = db.session.execute(text(
            "SELECT member_id FROM cached_member WHERE house = 'Commons'"
        )).fetchall()
        all_commons_ids = [r[0] for r in all_commons]
        _log(f"Building analytics for {len(all_commons_ids)} Commons MPs…")

        # ── 9. Assemble and upsert ───────────────────────────────────────────────
        upserted = errors = 0

        for member_id in all_commons_ids:
            try:
                total_sessions  = member_total.get(member_id, 0)
                sessions_12m    = commons_sessions_12m.get(member_id, 0)
                sessions_3m     = member_sessions_3m.get(member_id, 0)
                thin_data       = total_sessions < TIER_FULL

                # Policy area distribution — top 5 by session count (12m)
                pa_dist = member_pa_counts.get(member_id, {})
                top_pa_raw = sorted(pa_dist.items(), key=lambda x: -x[1])[:5]

                top_policy_areas = []
                for theme, cnt in top_pa_raw:
                    ranked = theme_member_counts[theme]
                    rank = next(
                        (i + 1 for i, (mid, _) in enumerate(ranked) if mid == member_id),
                        None,
                    )
                    contributor_count = len(ranked)
                    top_policy_areas.append({
                        "theme":             theme,
                        "sessions":          cnt,
                        "rank":              rank,
                        "rank_ordinal":      _ordinal(rank) if rank else None,
                        "contributor_count": contributor_count,
                        "surface_rank":      rank is not None and rank <= VOICE_RANK_THRESHOLD,
                    })

                # HHI using full policy area distribution, not just top 5
                all_pa_vals = list(member_pa_counts.get(member_id, {}).values())
                hhi = round(_hhi(all_pa_vals), 4)
                spec_label = _specialism_label(hhi, top_policy_areas)

                # Recent policy area shifts
                new_areas = member_recent_pa.get(member_id, set()) - member_prior_pa.get(member_id, set())
                recent_shifts = [{"theme": t} for t in sorted(new_areas)]

                # Tagged coverage
                tagged_12m = member_tagged_12m.get(member_id, 0)
                tagged_pct = round(tagged_12m / sessions_12m * 100, 1) if sessions_12m > 0 else None

                # Rank and percentile
                rank_12m = commons_rank.get(member_id)
                pct_12m: float | None = None
                if rank_12m is not None and total_commons_ranked > 0:
                    pct_12m = round((1 - (rank_12m - 1) / total_commons_ranked) * 100, 1)

                if dry_run:
                    upserted += 1
                    continue

                existing = db.session.get(MpAnalytics, member_id)
                if existing:
                    existing.computed_at      = now
                    existing.sessions_12m     = sessions_12m
                    existing.sessions_3m      = sessions_3m
                    existing.commons_rank_12m = rank_12m
                    existing.commons_total    = total_commons_ranked
                    existing.commons_pct_12m  = pct_12m
                    existing.top_policy_areas = top_policy_areas
                    existing.recent_shifts    = recent_shifts
                    existing.debate_type_dist = dict(member_dtype.get(member_id, {}))
                    existing.specialism_score = hhi
                    existing.specialism_label = spec_label
                    existing.thin_data        = thin_data
                    existing.tagged_pct       = tagged_pct
                else:
                    db.session.add(MpAnalytics(
                        member_id        = member_id,
                        computed_at      = now,
                        sessions_12m     = sessions_12m,
                        sessions_3m      = sessions_3m,
                        commons_rank_12m = rank_12m,
                        commons_total    = total_commons_ranked,
                        commons_pct_12m  = pct_12m,
                        top_policy_areas = top_policy_areas,
                        recent_shifts    = recent_shifts,
                        debate_type_dist = dict(member_dtype.get(member_id, {})),
                        specialism_score = hhi,
                        specialism_label = spec_label,
                        thin_data        = thin_data,
                        tagged_pct       = tagged_pct,
                    ))

                upserted += 1
                if upserted % 100 == 0:
                    db.session.commit()
                    _log(f"  {upserted} committed…")

            except Exception as exc:
                _log(f"  ERROR member_id={member_id}: {exc}")
                db.session.rollback()
                errors += 1

        if not dry_run:
            db.session.commit()

        _log(
            f"Done. {'(dry run) ' if dry_run else ''}"
            f"{upserted} members processed, {errors} errors."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Run queries and print counts without writing to DB")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
