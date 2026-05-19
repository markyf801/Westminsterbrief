"""
Bills query layer — Phase 2A.5.

Infrastructure only — no user-facing surface in v1.
See docs/phase2a-hansard-archive.md (AI tagging reliability finding)
for quality caveats on bills_for_party_and_policy_area().
"""

from __future__ import annotations

from extensions import db
from hansard_archive.models import HaBill, HaBillSponsor, HaBillTheme


def bills_for_party_and_policy_area(
    party: str,
    policy_area: str,
    *,
    session: str | None = None,
    include_acts: bool = True,
    limit: int = 200,
) -> list[dict]:
    """
    Return bills tagged with policy_area that were sponsored by a member of party.

    Quality caveat: policy_area matching uses ha_bill_theme which was populated
    by AI classification. Tag quality is approximate — see the AI tagging
    reliability finding in docs/phase2a-hansard-archive.md. PMB classification
    is especially unreliable due to sparse title/summary context.

    Parameters
    ----------
    party        : party name as stored in ha_bill_sponsor.party
                   (e.g. "Labour", "Conservative", "Liberal Democrat")
    policy_area  : display name from the GOV.UK 23-item taxonomy
                   (e.g. "Health and social care") — NOT a slug
    session      : optional filter to a specific parliamentary session
                   (e.g. "2024-25" or "2025-26")
    include_acts : if False, exclude bills that have already received Royal Assent
    limit        : max rows returned; default 200

    Returns
    -------
    list of dicts, each with keys:
        id, parliament_bill_id, title, bill_type, session,
        house_of_origin, is_act, is_defeated, current_stage,
        introduced_date, parliament_url,
        primary_sponsor_name, themes
    """
    has_theme = (
        db.session.query(HaBillTheme.bill_id)
        .filter(HaBillTheme.theme == policy_area)
    )

    has_sponsor = (
        db.session.query(HaBillSponsor.bill_id)
        .filter(
            HaBillSponsor.is_primary == True,
            HaBillSponsor.party == party,
        )
    )

    query = (
        db.session.query(HaBill)
        .filter(
            HaBill.id.in_(has_theme),
            HaBill.id.in_(has_sponsor),
        )
        .order_by(HaBill.introduced_date.desc().nulls_last(), HaBill.id.desc())
    )

    if session is not None:
        query = query.filter(HaBill.session == session)
    if not include_acts:
        query = query.filter(HaBill.is_act == False)

    bills = query.limit(limit).all()
    if not bills:
        return []

    bill_ids = [b.id for b in bills]

    primary_sponsors: dict[int, str] = {}
    for row in (
        db.session.query(HaBillSponsor.bill_id, HaBillSponsor.member_name)
        .filter(
            HaBillSponsor.bill_id.in_(bill_ids),
            HaBillSponsor.is_primary == True,
            HaBillSponsor.party == party,
        )
        .all()
    ):
        primary_sponsors.setdefault(row.bill_id, row.member_name)

    themes_by_bill: dict[int, list[str]] = {bid: [] for bid in bill_ids}
    for row in (
        db.session.query(HaBillTheme.bill_id, HaBillTheme.theme)
        .filter(HaBillTheme.bill_id.in_(bill_ids))
        .order_by(HaBillTheme.theme)
        .all()
    ):
        themes_by_bill[row.bill_id].append(row.theme)

    return [
        {
            "id":                   b.id,
            "parliament_bill_id":   b.parliament_bill_id,
            "title":                b.title,
            "bill_type":            b.bill_type,
            "session":              b.session,
            "house_of_origin":      b.house_of_origin,
            "is_act":               b.is_act,
            "is_defeated":          b.is_defeated,
            "current_stage":        b.current_stage,
            "introduced_date":      b.introduced_date,
            "parliament_url":       b.parliament_url,
            "primary_sponsor_name": primary_sponsors.get(b.id),
            "themes":               themes_by_bill.get(b.id, []),
        }
        for b in bills
    ]
