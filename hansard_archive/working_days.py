"""
Working days for Written Question answer times: Monday to Friday, excluding
bank holidays in England and Wales.

The bank-holiday list is explicit and covers 2024 to 2027 only. Source:
https://www.gov.uk/bank-holidays (England and Wales). Add the next year's
dates before the end of COVERED_TO; until then, dates outside the covered
range are reported as not covered and the answer-time features show nothing
for them rather than guess.

Recess is not excluded: Parliament's monitoring counts working days, not
sitting days.
"""
from datetime import date, timedelta
from functools import lru_cache

BANK_HOLIDAYS_ENGLAND_WALES: frozenset[date] = frozenset({
    # 2024
    date(2024, 1, 1), date(2024, 3, 29), date(2024, 4, 1), date(2024, 5, 6),
    date(2024, 5, 27), date(2024, 8, 26), date(2024, 12, 25), date(2024, 12, 26),
    # 2025
    date(2025, 1, 1), date(2025, 4, 18), date(2025, 4, 21), date(2025, 5, 5),
    date(2025, 5, 26), date(2025, 8, 25), date(2025, 12, 25), date(2025, 12, 26),
    # 2026 (Boxing Day falls on a Saturday: substitute day Monday 28 December)
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4),
    date(2026, 5, 25), date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28),
    # 2027 (Christmas Day and Boxing Day fall at a weekend: substitute days 27 and 28 December)
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 3),
    date(2027, 5, 31), date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
})

COVERED_FROM = date(2024, 1, 1)
COVERED_TO = date(2027, 12, 31)


def covered(d: date) -> bool:
    """True if d is inside the years the bank-holiday list covers."""
    return COVERED_FROM <= d <= COVERED_TO


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in BANK_HOLIDAYS_ENGLAND_WALES


@lru_cache(maxsize=4096)
def add_working_days(start: date, n: int) -> date:
    """The n-th working day after start (n >= 0; n == 0 returns start unchanged)."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if is_working_day(d):
            n -= 1
    return d


@lru_cache(maxsize=65536)
def working_days_between(start: date, end: date) -> int:
    """Working days after start, up to and including end (0 if end <= start).

    Tabled on Monday and answered on Wednesday of the same week is 2.
    """
    if end <= start:
        return 0
    n = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_working_day(d):
            n += 1
    return n
