"""
Propose additions to hansard_archive/topic_canon.py from existing tag frequencies.

READ-ONLY: runs SELECT ... GROUP BY on ha_session_theme and ha_pq_theme
(theme_type='specific') and prints suggestions. It never writes to the database
and never edits topic_canon.py; a person reads the output and edits the map.

Three kinds of suggestion:
  1. already-mapped  stored spellings that topic_canon already canonicalises
                     (rows written before the map existed; /brief pages already
                     include them, a retag would rewrite them)
  2. variants        spellings that differ only by case, punctuation, "&"/"and",
                     stop words or a plural "s" ("chagos island" / "chagos islands")
  3. children        frequent topics with longer topics that start with them
                     ("chagos islands" -> "chagos islands sovereignty")

Usage (Railway one-off or locally against a copy):
    python scripts/propose_topic_merges.py                 # sessions + PQs
    python scripts/propose_topic_merges.py --min-count 5 --top 80
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STOP = frozenset({"the", "of", "and", "for", "in", "on", "to", "a", "an", "uk"})
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def variant_key(theme: str) -> str:
    """Spelling-insensitive key: lowercase, '&' -> 'and', punctuation and stop words gone, plurals singular."""
    t = (theme or "").lower().replace("&", " and ").replace("-", " ").replace("/", " ")
    t = _PUNCT.sub("", t)
    words = []
    for w in t.split():
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(words)


def propose_merges(counts: dict[str, int], min_count: int = 2, child_min_parent: int = 10) -> dict:
    """
    counts: {stored theme: number of rows}. Returns
      {"already_mapped": [(stored, canonical, n)],
       "variants":       [(suggested canonical, [(stored, n), ...], total)],
       "children":       [(parent, [(stored child, n), ...])]}
    sorted by size, largest first.
    """
    from hansard_archive.topic_canon import canonical_topic

    norm = collections.Counter()
    for theme, n in counts.items():
        t = " ".join((theme or "").lower().split())
        if t:
            norm[t] += n

    already = sorted(
        ((t, canonical_topic(t), n) for t, n in norm.items() if canonical_topic(t) != t),
        key=lambda r: -r[2],
    )

    groups: dict[str, list] = collections.defaultdict(list)
    for t, n in norm.items():
        if canonical_topic(t) != t:
            continue                       # already handled by the map
        groups[variant_key(t)].append((t, n))
    variants = []
    for members in groups.values():
        if len(members) < 2:
            continue
        total = sum(n for _, n in members)
        if total < min_count:
            continue
        members.sort(key=lambda r: (-r[1], len(r[0]), r[0]))
        variants.append((members[0][0], members, total))
    variants.sort(key=lambda r: -r[2])

    children = []
    parents = sorted((t for t, n in norm.items() if n >= child_min_parent and canonical_topic(t) == t),
                     key=lambda t: -norm[t])
    for p in parents:
        kids = sorted(((t, n) for t, n in norm.items()
                       if t != p and t.startswith(p + " ") and n >= min_count),
                      key=lambda r: -r[1])
        if kids:
            children.append((p, kids))
    return {"already_mapped": already, "variants": variants, "children": children}


def _load_counts(include_pqs: bool) -> dict[str, int]:
    from flask_app import app
    from extensions import db
    from sqlalchemy import func
    from hansard_archive.models import THEME_TYPE_SPECIFIC, HansardSessionTheme, HaPQTheme

    counts: collections.Counter = collections.Counter()
    with app.app_context():
        models = [HansardSessionTheme] + ([HaPQTheme] if include_pqs else [])
        for model in models:
            rows = (
                db.session.query(model.theme, func.count())
                .filter(model.theme_type == THEME_TYPE_SPECIFIC)
                .group_by(model.theme)
                .all()
            )
            for theme, n in rows:
                counts[theme] += n
        db.session.rollback()
    return dict(counts)


def main() -> None:
    p = argparse.ArgumentParser(description="Propose topic_canon merges (read-only).")
    p.add_argument("--min-count", type=int, default=3, help="ignore groups smaller than this")
    p.add_argument("--top", type=int, default=50, help="rows to print per section")
    p.add_argument("--sessions-only", action="store_true", help="skip ha_pq_theme")
    args = p.parse_args()

    result = propose_merges(_load_counts(not args.sessions_only), min_count=args.min_count)

    print("== 1. Stored spellings already mapped by topic_canon (fixed by a retag) ==")
    for stored, canon, n in result["already_mapped"][: args.top]:
        print(f"  {n:7d}  {stored!r} -> {canon!r}")
    print("\n== 2. Spelling variants (suggested canonical first) ==")
    for canon, members, total in result["variants"][: args.top]:
        others = ", ".join(f"{t!r} ({n})" for t, n in members[1:])
        print(f"  {total:7d}  {canon!r} <- {others}")
    print("\n== 3. Possible child topics ==")
    for parent, kids in result["children"][: args.top]:
        listed = ", ".join(f"{t!r} ({n})" for t, n in kids[:8])
        print(f"  {parent!r}: {listed}")
    print("\nNothing was written. Edit hansard_archive/topic_canon.py by hand to adopt a suggestion.")


if __name__ == "__main__":
    main()
