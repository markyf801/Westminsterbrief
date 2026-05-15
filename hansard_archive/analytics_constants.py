"""
Constants for MP parliamentary activity analytics.

Procedural role filtering: we filter by member_name pattern rather than member_id.
This correctly handles role transitions — a person who later became Speaker has
their pre-Speaker contributions under their own name, and Speaker contributions
under "Mr Speaker". Name-pattern filtering excludes only the procedural-role
contributions, not the person's entire record.
"""

# Hansard member_name patterns that indicate a procedural chair role.
# Matched case-insensitively with ILIKE '%pattern%' in SQL.
PROCEDURAL_NAME_PATTERNS: list[str] = [
    "Mr Speaker",
    "Madam Speaker",
    "Deputy Speaker",     # covers "Mr Deputy Speaker", "Madam Deputy Speaker"
    "The Chairman",       # committee chairs
    "The Deputy Chairman",
]

# Commons MPs must have at least this many sessions to be included in the
# chamber-wide ranking baseline. Excludes very new MPs from distorting averages.
BASELINE_MIN_SESSIONS = 5

# Session thresholds for analytics panel rendering tiers:
#   < TIER_MINIMAL  → "limited data" note, no stats panel
#   TIER_MINIMAL–TIER_FULL-1 → basic panel (activity + policy areas only)
#   >= TIER_FULL    → full panel including voice ranking and recent shifts
TIER_MINIMAL = 5
TIER_FULL = 12

# Voice ranking: only surface rank when the MP places this high or better.
# Configurable here so we can tune after seeing real data.
VOICE_RANK_THRESHOLD = 10

# HHI specialism thresholds (Herfindahl-Hirschman Index, range 0–1):
#   >= HHI_SPECIALIST  → "Specialist on [top area]"
#   >= HHI_FOCUSED     → "Focused on [top 2-3 areas]"
#   < HHI_FOCUSED      → "Active across many policy areas"
HHI_SPECIALIST = 0.5
HHI_FOCUSED = 0.3

# Time windows (in days) used by the compute job.
WINDOW_LONG_DAYS = 365    # 12-month activity window
WINDOW_SHORT_DAYS = 90    # 3-month recent window
WINDOW_PRIOR_DAYS = 270   # 9-month "prior period" for shift detection
# Recent shift logic: theme appears in last 90 days but NOT in the 90–270 day window.
