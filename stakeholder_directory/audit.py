"""
Audit functions for the stakeholder directory database.

Each function returns {'issues': [...], 'summary': {...}}.
An empty issues list means that invariant holds.

Run standalone against the current database:
    python stakeholder_directory/audit.py
"""
import logging

logger = logging.getLogger(__name__)


def _aggregate_staging_counts(db) -> dict[str, int]:
    """Return status counts summed across all staging tables."""
    from stakeholder_directory.ingesters.staging import (
        StagingMinisterialMeeting,
        StagingCommitteeEvidence,
        StagingLobbyingEntry,
    )
    from sqlalchemy import func

    totals: dict[str, int] = {}
    for model in (StagingMinisterialMeeting, StagingCommitteeEvidence, StagingLobbyingEntry):
        rows = (
            db.session.query(model.processing_status, func.count().label('n'))
            .group_by(model.processing_status)
            .all()
        )
        for row in rows:
            totals[row.processing_status] = totals.get(row.processing_status, 0) + row.n
    return totals


def check_staging_consistency(app) -> dict:
    """Every committed staging row must correspond to exactly one engagement.
    No pending rows should remain after a completed normalisation run.
    Counts across all staging tables (ministerial_meeting + committee_evidence).
    """
    with app.app_context():
        from extensions import db
        from stakeholder_directory.models import Engagement

        status_counts = _aggregate_staging_counts(db)
        committed = status_counts.get('committed', 0)
        pending = status_counts.get('pending', 0)
        rejected = status_counts.get('rejected', 0)
        errored = status_counts.get('errored', 0)
        total_staging = sum(status_counts.values())

        engagement_count = db.session.query(Engagement).count()

        issues = []
        if committed != engagement_count:
            issues.append(
                f"committed staging rows ({committed}) != engagements ({engagement_count})"
            )
        if pending > 0:
            issues.append(
                f"{pending} staging rows still in 'pending' status (normalisation incomplete?)"
            )

        return {
            'issues': issues,
            'summary': {
                'staging_total': total_staging,
                'staging_committed': committed,
                'staging_pending': pending,
                'staging_rejected': rejected,
                'staging_errored': errored,
                'engagements': engagement_count,
            },
        }


def check_organisation_integrity(app) -> dict:
    """Referential integrity for organisations:
    - No organisations with zero engagements
    - No flags pointing to non-existent organisations
    - No aliases pointing to non-existent organisations
    """
    with app.app_context():
        from extensions import db
        from stakeholder_directory.models import Organisation, Engagement, Flag, Alias
        from sqlalchemy import func

        org_count = db.session.query(Organisation).count()
        engagement_count = db.session.query(Engagement).count()
        flag_count = db.session.query(Flag).count()
        alias_count = db.session.query(Alias).count()

        # Orgs with no engagements (LEFT JOIN + IS NULL)
        orgs_no_engagements = (
            db.session.query(Organisation)
            .outerjoin(Engagement, Engagement.organisation_id == Organisation.id)
            .group_by(Organisation.id)
            .having(func.count(Engagement.id) == 0)
            .all()
        )

        # Flags pointing to non-existent orgs
        all_org_ids = {r[0] for r in db.session.query(Organisation.id).all()}
        flag_org_ids = {r[0] for r in db.session.query(Flag.organisation_id).all()}
        orphaned_flag_org_ids = flag_org_ids - all_org_ids

        # Aliases pointing to non-existent orgs
        alias_org_ids = {r[0] for r in db.session.query(Alias.organisation_id).all()}
        orphaned_alias_org_ids = alias_org_ids - all_org_ids

        issues = []
        if orgs_no_engagements:
            names = [o.canonical_name for o in orgs_no_engagements[:5]]
            issues.append(
                f"{len(orgs_no_engagements)} org(s) with zero engagements "
                f"(first 5: {names})"
            )
        if orphaned_flag_org_ids:
            issues.append(
                f"{len(orphaned_flag_org_ids)} flag(s) reference non-existent org ids: "
                f"{sorted(orphaned_flag_org_ids)[:5]}"
            )
        if orphaned_alias_org_ids:
            issues.append(
                f"{len(orphaned_alias_org_ids)} alias(es) reference non-existent org ids: "
                f"{sorted(orphaned_alias_org_ids)[:5]}"
            )

        return {
            'issues': issues,
            'summary': {
                'organisations': org_count,
                'engagements': engagement_count,
                'flags': flag_count,
                'aliases': alias_count,
                'orgs_without_engagements': len(orgs_no_engagements),
                'orphaned_flag_org_ids': len(orphaned_flag_org_ids),
                'orphaned_alias_org_ids': len(orphaned_alias_org_ids),
            },
        }


def check_count_invariants(app) -> dict:
    """High-level count invariants:
    - engagement_count >= org_count (every org has at least one engagement)
    - committed_staging_count == engagement_count (across all staging tables)
    - no duplicate canonical_names among organisations
    """
    with app.app_context():
        from extensions import db
        from stakeholder_directory.models import Organisation, Engagement, Flag, Alias
        from sqlalchemy import func

        org_count = db.session.query(Organisation).count()
        engagement_count = db.session.query(Engagement).count()
        flag_count = db.session.query(Flag).count()
        alias_count = db.session.query(Alias).count()
        status_counts = _aggregate_staging_counts(db)
        committed_staging = status_counts.get('committed', 0)

        # Duplicate canonical_names
        dup_names = (
            db.session.query(Organisation.canonical_name)
            .group_by(Organisation.canonical_name)
            .having(func.count(Organisation.id) > 1)
            .all()
        )

        issues = []
        if engagement_count < org_count:
            issues.append(
                f"engagement_count ({engagement_count}) < org_count ({org_count}): "
                "some orgs have zero engagements"
            )
        if committed_staging != engagement_count:
            issues.append(
                f"committed_staging ({committed_staging}) != engagement_count ({engagement_count})"
            )
        if dup_names:
            issues.append(
                f"{len(dup_names)} duplicate canonical_name(s): "
                f"{[r[0] for r in dup_names[:5]]}"
            )

        return {
            'issues': issues,
            'summary': {
                'organisations': org_count,
                'engagements': engagement_count,
                'flags': flag_count,
                'aliases': alias_count,
                'committed_staging': committed_staging,
                'duplicate_canonical_names': len(dup_names),
            },
        }


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))

    from flask import Flask
    from extensions import db as _db
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    _DB_PATH = Path(__file__).parent.parent / 'instance' / 'dfe_real_run2.db'
    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{_DB_PATH}'
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    _db.init_app(_app)

    print('=' * 60)
    print('check_staging_consistency')
    print('=' * 60)
    r1 = check_staging_consistency(_app)
    print(f"  issues:  {r1['issues']}")
    for k, v in r1['summary'].items():
        print(f"  {k}: {v}")

    print()
    print('=' * 60)
    print('check_organisation_integrity')
    print('=' * 60)
    r2 = check_organisation_integrity(_app)
    print(f"  issues:  {r2['issues']}")
    for k, v in r2['summary'].items():
        print(f"  {k}: {v}")

    print()
    print('=' * 60)
    print('check_count_invariants')
    print('=' * 60)
    r3 = check_count_invariants(_app)
    print(f"  issues:  {r3['issues']}")
    for k, v in r3['summary'].items():
        print(f"  {k}: {v}")
