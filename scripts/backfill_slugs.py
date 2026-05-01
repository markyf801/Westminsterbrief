"""
One-shot slug backfill for existing ha_session rows.

Generates slugs for all sessions that currently have slug=NULL.
Collision guard: if two sessions produce the same 4-char slug, the
colliding sessions are upgraded to a 6-char suffix automatically.

Run once after the slug column has been added via the startup migration.
Safe to re-run: sessions with an existing slug are skipped.

Reports:
  - Total slugged
  - Collisions where 6-char fallback fired
  - 10 random sample slugs for spot-check
"""

import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_app import app, db
from hansard_archive.models import HansardSession
from hansard_archive.slugs import make_slug, title_to_slug


def _build_slug_map(sessions: list) -> tuple[dict, list]:
    """
    Build {session_id: slug} for all sessions, resolving collisions.

    Returns (slug_map, collision_log) where collision_log lists tuples of
    (session_id, colliding_slug, resolved_slug).
    """
    # First pass: 4-char suffix
    slug_map: dict[int, str] = {}
    slug_to_ids: dict[str, list[int]] = {}

    for s in sessions:
        slug = make_slug(s.title, s.ext_id, suffix_len=4)
        slug_map[s.id] = slug
        slug_to_ids.setdefault(slug, []).append(s.id)

    # Identify collisions
    collisions = {slug: ids for slug, ids in slug_to_ids.items() if len(ids) > 1}
    collision_log = []

    if collisions:
        # Build a lookup for sessions involved in collisions
        collision_ids = {sid for ids in collisions.values() for sid in ids}
        id_to_session = {s.id: s for s in sessions if s.id in collision_ids}

        for colliding_slug, ids in collisions.items():
            for sid in ids:
                s = id_to_session[sid]
                resolved = make_slug(s.title, s.ext_id, suffix_len=6)
                collision_log.append((sid, colliding_slug, resolved))
                slug_map[sid] = resolved

        # Second-order collisions: use full ext_id as guaranteed-unique last resort.
        # In practice these are containers with numeric ext_ids (e.g. "House of Lords"
        # sessions where both title and last-6 digits repeat across sitting days).
        # Full ext_id is always unique (it has its own unique constraint).
        slug_to_ids_2: dict[str, list[int]] = {}
        for sid, slug in slug_map.items():
            slug_to_ids_2.setdefault(slug, []).append(sid)
        second_order = {slug: ids for slug, ids in slug_to_ids_2.items() if len(ids) > 1}
        if second_order:
            id_to_session2 = {s.id: s for s in sessions}
            for colliding_slug, ids in second_order.items():
                for sid in ids:
                    s = id_to_session2[sid]
                    # Use full ext_id as slug suffix — guaranteed unique
                    resolved = f"{title_to_slug(s.title)}-{s.ext_id.lower()}"
                    collision_log.append((sid, colliding_slug, resolved))
                    slug_map[sid] = resolved

    return slug_map, collision_log


def run_backfill() -> None:
    with app.app_context():
        # Only sessions without a slug
        sessions = (
            db.session.query(HansardSession)
            .filter(HansardSession.slug.is_(None))
            .all()
        )

        if not sessions:
            print("No sessions without slugs — backfill already complete.")
            return

        print(f"Sessions to slug: {len(sessions)}")

        slug_map, collision_log = _build_slug_map(sessions)

        print(f"Collisions (4-char upgraded to 6-char): {len(collision_log)}")
        for sid, old_slug, new_slug in collision_log:
            print(f"  session {sid}: {old_slug!r} -> {new_slug!r}")

        # Bulk write
        print("Writing slugs...")
        written = 0
        errors = 0
        for s in sessions:
            slug = slug_map.get(s.id)
            if slug is None:
                continue  # second-order collision — skip
            s.slug = slug
            written += 1

        try:
            db.session.commit()
            print(f"Committed {written} slugs.")
        except Exception as e:
            db.session.rollback()
            print(f"ERROR on bulk commit: {e}")
            errors += 1

        # Verify
        null_remaining = (
            db.session.query(HansardSession)
            .filter(HansardSession.slug.is_(None))
            .count()
        )
        total_slugged = db.session.query(HansardSession).filter(
            HansardSession.slug.isnot(None)
        ).count()

        print(f"\n{'='*60}")
        print(f"Backfill complete.")
        print(f"  Sessions slugged (total in DB): {total_slugged}")
        print(f"  Sessions still without slug:   {null_remaining}")
        print(f"  Collisions resolved (6-char):  {len(collision_log)}")
        print(f"  Errors:                        {errors}")
        print(f"{'='*60}")

        # 10 random samples for spot-check
        all_slugged = (
            db.session.query(HansardSession)
            .filter(HansardSession.slug.isnot(None))
            .all()
        )
        sample = random.sample(all_slugged, min(10, len(all_slugged)))
        print("\n10 random sample slugs:")
        print(f"  {'Date':<14} {'House':<8} {'Slug'}")
        print(f"  {'-'*14} {'-'*8} {'-'*60}")
        for s in sorted(sample, key=lambda x: x.date, reverse=True):
            print(f"  {str(s.date):<14} {s.house:<8} {s.slug}")


if __name__ == "__main__":
    run_backfill()
