"""
hansard_archive/stats_queries.py

Reusable query helpers for HeadlineStat data.

SWR note: HeadlineStat rows follow a stale-while-revalidate pattern —
on a refresh failure, last_refreshed is updated but latest_value keeps
its previous value. Queries here filter on latest_value.isnot(None)
to exclude rows that have never successfully fetched a value, while
rows with stale-but-non-null values are intentionally included so
pages continue rendering during transient source outages.
"""

from __future__ import annotations

from hansard_archive.models import HeadlineStat


def get_headline_stat(theme_slug: str) -> HeadlineStat | None:
    """
    Return the first HeadlineStat for the given theme slug that has a
    non-null latest_value, or None if no such row exists.

    Used on /brief/<slug> (teaser line) and /stats/<slug> (full card).
    Rows where latest_value is None have never been successfully fetched
    and should not be displayed.
    """
    return (
        HeadlineStat.query
        .filter_by(theme_slug=theme_slug)
        .filter(HeadlineStat.latest_value.isnot(None))
        .first()
    )
