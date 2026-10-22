"""
Production cron windows (UTC) — one shared definition for long-running one-off
jobs (scripts/retag.py, the Parliament-start backfill) that must not write
while a scheduled cron is running or about to run.

Schedule (CLAUDE.md "Branches and workflow", docs/ops/railway-services.md):
  backup-cron-r2 03:00 daily; member-cache-refresh Sun 02:00;
  member-profile-refresh Sun 03:00; member-bio-refresh 2nd of month 04:00;
  edm-cron 04:30; upcoming-refresh 05:45; rediscovery 06:00; bills-cron 06:30;
  daily health report 07:30; archive morning-catchup 08:00 Mon-Fri;
  PQ crons 08:30, 09:30, 14:00 Mon-Fri and 09:00 Mon; PQ quick-check
  (pq-cron-quickcheck, once created) :15 and :45 past 06:00-19:00 Mon-Fri;
  archive daytime hourly 11:00-23:00 Mon-Thu and 09:00-19:00 Fri.

Deliberately wide: weekday daytime is blocked whole, because the archive cron
runs hourly and a capped tagging run can take most of an hour, so a gap between
two hourly runs is never a safe place for a long job. Jobs check
blocked_window(now) with a lookahead (default 20 minutes) before each chunk of
work, so a chunk started just before a window has time to finish.

All times are UTC. A push to master redeploys every cron (CLAUDE.md), which is
a separate rule this module doesn't cover.
"""

from __future__ import annotations

from datetime import datetime, timedelta

LOOKAHEAD_MINUTES = 20

_ALL_DAYS = frozenset(range(7))
MON_THU = frozenset({0, 1, 2, 3})
FRI = frozenset({4})

# (weekdays Mon=0, start "HH:MM", end "HH:MM", label)
CRON_WINDOWS: tuple[tuple[frozenset, str, str, str], ...] = (
    (frozenset({6}), "01:55", "02:20", "member-cache-refresh (Sun 02:00)"),
    (_ALL_DAYS, "02:55", "03:30", "backup-cron-r2 (03:00)"),
    (frozenset({6}), "02:55", "03:50", "member-profile-refresh (Sun 03:00)"),
    (_ALL_DAYS, "04:25", "07:45",
     "edm (04:30) / upcoming (05:45) / rediscovery (06:00) / bills (06:30-07:15) / health report (07:30)"),
    (MON_THU, "07:55", "23:30",
     "archive 08:00 + hourly 11:00-23:00, PQ 08:30/09:30/14:00 (Mon also 09:00), "
     "PQ quick-check 06:15-19:45"),
    (FRI, "07:55", "20:00",
     "Fri: archive 08:00 + hourly 09:00-19:00, PQ 08:30/09:30/14:00, PQ quick-check to 19:45"),
)
# (day of month, start, end, label)
MONTHLY_WINDOWS: tuple[tuple[int, str, str, str], ...] = (
    (2, "03:55", "05:00", "member-bio-refresh (2nd of month, 04:00)"),
)


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def blocked_window(now: datetime, lookahead_minutes: int = LOOKAHEAD_MINUTES) -> str | None:
    """Label of the cron window `now` (UTC) is in, or will be in within the lookahead; else None."""
    for moment in (now, now + timedelta(minutes=lookahead_minutes)):
        hm = (moment.hour, moment.minute)
        for days, start, end, label in CRON_WINDOWS:
            if moment.weekday() in days and _hhmm(start) <= hm < _hhmm(end):
                return label
        for mday, start, end, label in MONTHLY_WINDOWS:
            if moment.day == mday and _hhmm(start) <= hm < _hhmm(end):
                return label
    return None
