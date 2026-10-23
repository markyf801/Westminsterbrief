"""
Date-chunked walking for long Written Question catch-ups.

A long answered (or tabled) window means paging very deep into Parliament's
WQ API, which returns HTTP 500 on deep pages and seems to shift rows between
requests. scripts/ingest_pq_cron.py --answered-from/--answered-to (or
--tabled-from/--tabled-to) with --chunk-days instead walks the period in short
windows, so each window needs only a few shallow pages.

Chunks are walked newest first: recent questions are the ones people look at
most, so if a run stops early the most useful part is already done, and what
is left is the oldest end.

Progress lives in the run's ha_cron_run.notes (Railway one-off containers
have no lasting disk), in this form:

  pq-chunked: answered 2025-05-01..2026-10-23 every 7d newest-first | next=2026-03-12
  | failed=2026-09-10,2026-08-27 | short=- | ins=0 upd=1234 same=5000 full=1300 fetch_failed=3
  | beat=2026-10-23T12:34:56

(one line). next = start of the next chunk to walk ("end" when every chunk has
been walked once); failed = chunk starts still to retry; short = chunks where
the API's totalResults stayed above the unique questions seen; beat = last
progress (UTC), which keeps the run counted as active by the quick-check soft
lock however long it lasts.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

NOTE_PREFIX = "pq-chunked"
KINDS = ("answered", "tabled")

_BEAT_RE = re.compile(r"\| beat=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")


def plan_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Inclusive (from, to) windows covering start..end exactly once, newest
    first. Each is chunk_days long except possibly the oldest."""
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    chunks = []
    to = end
    while to >= start:
        frm = max(start, to - timedelta(days=chunk_days - 1))
        chunks.append((frm, to))
        to = frm - timedelta(days=1)
    return chunks


@dataclass
class ChunkProgress:
    kind: str
    start: date
    end: date
    chunk_days: int
    next_from: date | None = None          # None = every chunk walked once
    failed: list[date] = field(default_factory=list)
    short: list[date] = field(default_factory=list)
    totals: dict = field(default_factory=lambda: {"ins": 0, "upd": 0, "same": 0,
                                                  "full": 0, "fetch_failed": 0})

    @classmethod
    def fresh(cls, kind: str, start: date, end: date, chunk_days: int) -> "ChunkProgress":
        prog = cls(kind, start, end, chunk_days)
        prog.next_from = prog.chunks()[0][0]
        return prog

    @property
    def key(self) -> str:
        """Identifies the plan: a run resumes only a run with the same key."""
        return (f"{self.kind} {self.start}..{self.end} every {self.chunk_days}d "
                f"newest-first")

    def chunks(self) -> list[tuple[date, date]]:
        return plan_chunks(self.start, self.end, self.chunk_days)

    def remaining(self) -> list[tuple[date, date]]:
        """Chunks not walked yet, in walking order."""
        if self.next_from is None:
            return []
        chunks = self.chunks()
        starts = [c[0] for c in chunks]
        return chunks[starts.index(self.next_from):] if self.next_from in starts else []

    def failed_chunks(self) -> list[tuple[date, date]]:
        """Chunks to retry, in walking order."""
        return [c for c in self.chunks() if c[0] in self.failed]

    def done_count(self) -> int:
        return len(self.chunks()) - len(self.remaining()) - len(self.failed)

    def is_complete(self) -> bool:
        return self.next_from is None and not self.failed

    def mark(self, chunk: tuple[date, date], ok: bool) -> None:
        if ok:
            if chunk[0] in self.failed:
                self.failed.remove(chunk[0])
        elif chunk[0] not in self.failed:
            self.failed.append(chunk[0])

    def advance_past(self, chunk: tuple[date, date]) -> None:
        """chunk has been walked (well or not): next is the one after it."""
        chunks = self.chunks()
        i = [c[0] for c in chunks].index(chunk[0])
        self.next_from = chunks[i + 1][0] if i + 1 < len(chunks) else None

    def add_short(self, chunk: tuple[date, date]) -> None:
        if chunk[0] not in self.short:
            self.short.append(chunk[0])

    def format(self, beat: datetime) -> str:
        def dates(ds):
            return ",".join(d.isoformat() for d in ds) or "-"
        t = self.totals
        return (f"{NOTE_PREFIX}: {self.key} | "
                f"next={self.next_from.isoformat() if self.next_from else 'end'} | "
                f"done={self.done_count()}/{len(self.chunks())} | "
                f"failed={dates(self.failed)} | short={dates(self.short)} | "
                f"ins={t['ins']} upd={t['upd']} same={t['same']} full={t['full']} "
                f"fetch_failed={t['fetch_failed']} | beat={beat:%Y-%m-%dT%H:%M:%S}")


_NOTE_RE = re.compile(
    rf"^{NOTE_PREFIX}: (?P<kind>{'|'.join(KINDS)}) (?P<start>\d{{4}}-\d\d-\d\d)\.\."
    r"(?P<end>\d{4}-\d\d-\d\d) every (?P<days>\d+)d newest-first \| "
    r"next=(?P<next>\d{4}-\d\d-\d\d|end) \|.*?failed=(?P<failed>[-\d,]+) \| "
    r"short=(?P<short>[-\d,]+) \| ins=(?P<ins>\d+) upd=(?P<upd>\d+) same=(?P<same>\d+) "
    r"full=(?P<full>\d+) fetch_failed=(?P<ff>\d+)"
)


def parse_progress(notes: str | None) -> ChunkProgress | None:
    """The progress saved in a chunked run's notes, or None if unreadable."""
    m = _NOTE_RE.match(notes or "")
    if not m:
        return None

    def dates(raw):
        return [] if raw == "-" else [date.fromisoformat(x) for x in raw.split(",") if x]
    try:
        prog = ChunkProgress(
            m["kind"], date.fromisoformat(m["start"]), date.fromisoformat(m["end"]),
            int(m["days"]),
            next_from=None if m["next"] == "end" else date.fromisoformat(m["next"]),
            failed=dates(m["failed"]), short=dates(m["short"]),
            totals={"ins": int(m["ins"]), "upd": int(m["upd"]), "same": int(m["same"]),
                    "full": int(m["full"]), "fetch_failed": int(m["ff"])},
        )
    except ValueError:
        return None
    if prog.next_from is not None and prog.next_from not in [c[0] for c in prog.chunks()]:
        return None
    return prog


def last_beat(notes: str | None) -> datetime | None:
    """The beat= time in a chunked run's notes (UTC), or None."""
    m = _BEAT_RE.search(notes or "")
    return datetime.fromisoformat(m[1]) if m else None
