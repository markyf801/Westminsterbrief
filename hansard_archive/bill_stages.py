"""
Bill stage display data — descriptors, normalisation, and status computation.

Two public surfaces:

  1. get_stage_descriptor(stage_name, house) -> StageDescriptor | None
     Returns display metadata for a stage type.  Used by the timeline template
     to populate tooltip/expand content.

  2. compute_bill_status(bill) -> BillStatus
     Returns a structured status object used by the bill detail page.
     NOT called here — deferred to Step 8 (display pages).  The dataclass
     definitions are here so the helper can be written alongside the template.

Stage names stored in ha_bill_stage.stage_name come from the Parliament Bills
API `description` field.  Normalisation (lowercase + strip) is applied before
lookup so minor API casing variations resolve correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hansard_archive.models import HaBill, HaBillStage


# ---------------------------------------------------------------------------
# StageDescriptor — one entry per stage type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageDescriptor:
    display_name: str           # Canonical parliamentary display name
    description: str            # 1–2 sentences shown in tooltip/expand
    house_context: str          # "Commons", "Lords", "Either", "None"
    # rough canonical order within a house — used as tiebreak only;
    # actual ordering uses ha_bill_stage.stage_order from the API
    canonical_order: int = 99


# ---------------------------------------------------------------------------
# Stage descriptor registry
# ---------------------------------------------------------------------------
# Keys are normalised (lowercase, stripped) stage names as returned by the
# Parliament Bills API `description` field.
#
# Two entries with the same canonical display name exist for Commons / Lords
# variants that carry different descriptions.
# ---------------------------------------------------------------------------

_STAGE_REGISTRY: dict[str, StageDescriptor] = {

    # ── First reading ────────────────────────────────────────────────────────

    "1st reading": StageDescriptor(
        display_name="First reading",
        description=(
            "The bill is formally introduced and its title read aloud. "
            "There is no debate at this stage; it simply marks the start "
            "of the bill's parliamentary journey."
        ),
        house_context="Either",
        canonical_order=10,
    ),
    "introduction and 1st reading": StageDescriptor(
        display_name="Introduction and first reading",
        description=(
            "The bill is introduced and its title read — usually the form "
            "used for bills originating in the House of Lords. "
            "No debate takes place."
        ),
        house_context="Lords",
        canonical_order=10,
    ),

    # ── Second reading ───────────────────────────────────────────────────────

    "2nd reading": StageDescriptor(
        display_name="Second reading",
        description=(
            "The main debate on the bill's general principles and purpose. "
            "Members vote on whether the bill should proceed; the debate "
            "signals concerns that may lead to amendments at later stages."
        ),
        house_context="Either",
        canonical_order=20,
    ),
    "2nd reading (day 1)": StageDescriptor(
        display_name="Second reading (day 1)",
        description=(
            "First day of an extended Second Reading debate on the bill's "
            "general principles. The full debate continues on a subsequent day."
        ),
        house_context="Either",
        canonical_order=20,
    ),
    "2nd reading (day 2)": StageDescriptor(
        display_name="Second reading (day 2)",
        description=(
            "Continuation of the Second Reading debate. Members vote at the "
            "end of this session on whether the bill should proceed."
        ),
        house_context="Either",
        canonical_order=21,
    ),

    # ── Committee stage ──────────────────────────────────────────────────────

    "committee stage": StageDescriptor(
        display_name="Committee stage",
        description=(
            "The bill is examined line by line and clause by clause. "
            "In the Commons this is usually a Public Bill Committee of 16–50 MPs; "
            "in the Lords all peers may attend and move amendments."
        ),
        house_context="Either",
        canonical_order=30,
    ),
    "public bill committee": StageDescriptor(
        display_name="Public Bill Committee",
        description=(
            "A Commons committee of typically 16–50 MPs examines the bill "
            "in detail, clause by clause. Witnesses may be called at the "
            "start; members vote on proposed amendments."
        ),
        house_context="Commons",
        canonical_order=30,
    ),
    "committee of the whole house": StageDescriptor(
        display_name="Committee of the Whole House",
        description=(
            "All MPs (or all Lords) act as the committee — used for "
            "constitutionally significant bills, Finance Bills, and "
            "emergency legislation. All members may participate in debates "
            "and vote on amendments."
        ),
        house_context="Either",
        canonical_order=30,
    ),
    "grand committee": StageDescriptor(
        display_name="Grand Committee",
        description=(
            "A Lords committee stage held in a separate committee room "
            "rather than the Chamber. Proceedings must be unanimous — "
            "any Lord may object to a vote — so controversial amendments "
            "are rarely pressed here."
        ),
        house_context="Lords",
        canonical_order=30,
    ),

    # ── Report stage ─────────────────────────────────────────────────────────

    "report stage": StageDescriptor(
        display_name="Report stage",
        description=(
            "The bill as amended in committee is considered by the full House. "
            "Further amendments may be proposed; this is often the last "
            "opportunity for significant changes before Third Reading."
        ),
        house_context="Either",
        canonical_order=40,
    ),
    "report stage (day 1)": StageDescriptor(
        display_name="Report stage (day 1)",
        description=(
            "First day of Report Stage — the full House considers amendments "
            "to the committee text. The stage continues on further days."
        ),
        house_context="Either",
        canonical_order=40,
    ),
    "report stage (day 2)": StageDescriptor(
        display_name="Report stage (day 2)",
        description=(
            "Continuation of Report Stage; further amendments are considered "
            "and voted on."
        ),
        house_context="Either",
        canonical_order=41,
    ),
    "report stage (day 3)": StageDescriptor(
        display_name="Report stage (day 3)",
        description=(
            "Continuation of Report Stage for a particularly complex or "
            "contested bill."
        ),
        house_context="Either",
        canonical_order=42,
    ),

    # ── Third reading ────────────────────────────────────────────────────────

    "3rd reading": StageDescriptor(
        display_name="Third reading",
        description=(
            "Final consideration in the originating House. "
            "In the Commons, amendments are very rarely permitted; "
            "in the Lords, further amendments are still possible. "
            "A vote approves the bill before it passes to the other House."
        ),
        house_context="Either",
        canonical_order=50,
    ),

    # ── Cross-house amendment stages ─────────────────────────────────────────

    "consideration of amendments": StageDescriptor(
        display_name="Consideration of amendments",
        description=(
            "After one House amends the bill, the other House considers "
            "those amendments. It may accept them, reject them, or propose "
            "alternatives — beginning a process of negotiation between "
            "the two Houses."
        ),
        house_context="Either",
        canonical_order=60,
    ),
    "lords amendments": StageDescriptor(
        display_name="Consideration of Lords amendments",
        description=(
            "The Commons considers amendments made by the Lords. "
            "It may accept, reject, or propose alternative wording "
            "for each amendment."
        ),
        house_context="Commons",
        canonical_order=60,
    ),
    "commons amendments": StageDescriptor(
        display_name="Consideration of Commons amendments",
        description=(
            "The Lords considers amendments or disagreements returned by "
            "the Commons. Further rounds of exchange may follow until both "
            "Houses agree on the final text."
        ),
        house_context="Lords",
        canonical_order=60,
    ),
    "ping pong": StageDescriptor(
        display_name="Ping-pong",
        description=(
            "Repeated exchanges of amendments between the Commons and Lords "
            "until both Houses agree on the final text. Each round of "
            "disagreement generates a new exchange; most bills resolve "
            "within one or two rounds."
        ),
        house_context="Either",
        canonical_order=65,
    ),

    # ── Royal Assent ─────────────────────────────────────────────────────────

    "royal assent": StageDescriptor(
        display_name="Royal Assent",
        description=(
            "The Monarch formally approves the bill, making it an Act of "
            "Parliament. Royal Assent is now signified by notification "
            "rather than in person."
        ),
        house_context="None",
        canonical_order=70,
    ),

    # ── Procedural / other ───────────────────────────────────────────────────

    "lords stages": StageDescriptor(
        display_name="Lords stages",
        description=(
            "The bill has passed to the House of Lords for consideration. "
            "It will follow the standard Lords legislative stages: "
            "First Reading, Second Reading, Committee, Report, and "
            "Third Reading."
        ),
        house_context="Lords",
        canonical_order=55,
    ),
    "commons stages": StageDescriptor(
        display_name="Commons stages",
        description=(
            "The bill has passed to the House of Commons for consideration. "
            "It will follow the standard Commons legislative stages: "
            "First Reading, Second Reading, Committee, Report, and "
            "Third Reading."
        ),
        house_context="Commons",
        canonical_order=55,
    ),
    "programme motion": StageDescriptor(
        display_name="Programme motion",
        description=(
            "A timetabling motion that sets out how much parliamentary time "
            "will be allocated to the bill's remaining stages. "
            "Agreed immediately after Second Reading in the Commons."
        ),
        house_context="Commons",
        canonical_order=25,
    ),
    "money resolution": StageDescriptor(
        display_name="Money resolution",
        description=(
            "A Commons resolution authorising public expenditure required "
            "by the bill. Must be passed before Committee Stage can begin "
            "if the bill involves public spending."
        ),
        house_context="Commons",
        canonical_order=26,
    ),
    "ways and means resolution": StageDescriptor(
        display_name="Ways and Means resolution",
        description=(
            "A Commons resolution authorising the imposition of taxation "
            "required by the bill. Required before a Finance Bill or any "
            "bill creating a new tax can proceed."
        ),
        house_context="Commons",
        canonical_order=27,
    ),
    "carry-over motion": StageDescriptor(
        display_name="Carry-over motion",
        description=(
            "A motion to carry the bill over to the next parliamentary "
            "session rather than letting it lapse at the end of the current "
            "session. Allows multi-session bills to continue without "
            "starting again from First Reading."
        ),
        house_context="Either",
        canonical_order=98,
    ),
    "withdrawal": StageDescriptor(
        display_name="Withdrawn",
        description=(
            "The bill has been formally withdrawn by its sponsors. "
            "It will make no further progress unless reintroduced in a "
            "future session."
        ),
        house_context="Either",
        canonical_order=99,
    ),
}

# ---------------------------------------------------------------------------
# Normalisation and lookup
# ---------------------------------------------------------------------------

def _normalise(name: str) -> str:
    return name.lower().strip()


def get_stage_descriptor(stage_name: str, house: str | None = None) -> StageDescriptor | None:
    """
    Return the StageDescriptor for a stage name, or None if unknown.

    stage_name: value from ha_bill_stage.stage_name (API description field)
    house:      value from ha_bill_stage.house — used only for future
                house-specific overrides; currently informational.
    """
    return _STAGE_REGISTRY.get(_normalise(stage_name))


def get_display_name(stage_name: str) -> str:
    """
    Return the display name for a stage, or the raw name capitalised if
    not in the registry (graceful degradation for new API values).
    """
    descriptor = get_stage_descriptor(stage_name)
    if descriptor:
        return descriptor.display_name
    # Fallback: title-case the raw name
    return stage_name.title()


# ---------------------------------------------------------------------------
# BillStatus dataclass — populated by compute_bill_status() in Step 8
# ---------------------------------------------------------------------------
# Defined here so template authors and tests can import the type without
# importing the full display module.
# ---------------------------------------------------------------------------

# Stage display groups — matches Parliament's own three-column layout:
#   "originating_house"  — stages in the house where the bill started
#   "second_house"       — stages in the other house
#   "final"              — Consideration of Amendments, Ping-pong, Royal Assent
#
# compute_bill_status() assigns group based on ha_bill_stage.house vs
# ha_bill.house_of_origin.  Template renders three columns in this order.

STAGE_GROUP_ORIGINATING = "originating_house"
STAGE_GROUP_SECOND      = "second_house"
STAGE_GROUP_FINAL       = "final"

# Stage names that always belong to the final group regardless of house
_FINAL_STAGE_NAMES: frozenset[str] = frozenset({
    "royal assent",
    "consideration of amendments",
    "lords amendments",
    "commons amendments",
    "ping pong",
})


def stage_group(stage_name: str, stage_house: str, originating_house: str) -> str:
    """
    Return the display group for a stage.
    Used by compute_bill_status() in Step 8.
    """
    if _normalise(stage_name) in _FINAL_STAGE_NAMES:
        return STAGE_GROUP_FINAL
    if _normalise(stage_house) == _normalise(originating_house):
        return STAGE_GROUP_ORIGINATING
    return STAGE_GROUP_SECOND


# ---------------------------------------------------------------------------
# Stages that have no substantive parliamentary debate record.
# Session-link matching is skipped for these.
# ---------------------------------------------------------------------------
NO_DEBATE_STAGE_NAMES: frozenset[str] = frozenset({
    "1st reading",
    "introduction and 1st reading",
    "programme motion",
    "money resolution",
    "ways and means resolution",
    "carry-over motion",
    "withdrawal",
    "royal assent",
})


@dataclass
class StageState:
    """
    Per-stage state for the three-column timeline display.

    state values match Parliament's own four-state key:
      "completed"      — stage is done (tick)
      "current"        — stage is in progress (hourglass)
      "not_applicable" — stage cannot apply to this bill's path (strikethrough circle)
      "pending"        — stage not yet reached (empty circle)

    Session link fields (populated after compute_bill_status by attach_session_links):
      has_debate         — False for procedural stages with no Hansard record
      local_session_slug — set when a matching ha_session exists in the local archive
      local_session_date — paired with slug to build /archive/debate/<date>/<slug> URL
      hansard_fallback_url — set for debate stages outside the archive window
    """
    stage_name: str
    display_name: str
    house: str
    stage_order: int
    stage_date: date | None
    state: str          # "completed" | "current" | "not_applicable" | "pending"
    group: str          # STAGE_GROUP_* constant — drives three-column layout
    descriptor: StageDescriptor | None
    # Session link fields — defaults None; populated by attach_session_links()
    has_debate: bool = True
    local_session_slug: str | None = None
    local_session_date: "date | None" = None
    local_session_url_date: str | None = None   # e.g. "12-may-2025"
    hansard_fallback_url: str | None = None


@dataclass
class BillStatus:
    """
    Structured status object returned by compute_bill_status(bill).

    fate values:
      "in_progress"  — bill is currently progressing
      "act"          — bill received Royal Assent; is now an Act
      "defeated"     — bill was defeated (division lost or killed)
      "withdrawn"    — bill was formally withdrawn
      "carried_over" — bill carried over to a subsequent session
      "lapsed"       — session ended without carry-over; bill did not pass
    """
    fate: str
    banner_text: str                      # single-line for the status banner
    current_stage_name: str | None        # display name of the current stage
    current_stage_house: str | None
    last_action_date: date | None
    royal_assent_date: date | None
    # stages grouped for template: access via stages_by_group() helper below
    stages: list[StageState] = field(default_factory=list)
    is_act: bool = False
    is_defeated: bool = False
    is_carried_over: bool = False

    def stages_by_group(self) -> dict[str, list[StageState]]:
        """Return stages partitioned into the three display columns."""
        groups: dict[str, list[StageState]] = {
            STAGE_GROUP_ORIGINATING: [],
            STAGE_GROUP_SECOND:      [],
            STAGE_GROUP_FINAL:       [],
        }
        for s in self.stages:
            groups[s.group].append(s)
        return groups


# ---------------------------------------------------------------------------
# Fate determination helpers (used by compute_bill_status in Step 8)
# ---------------------------------------------------------------------------

_SESSION_LABELS: dict[int, str] = {
    36: "2022-23",
    37: "2023-24",
    38: "2024-25",
    39: "2025-26",
}


def _is_carried_over(bill: "HaBill") -> bool:
    """
    Detect carry-over from raw_data.  The clearest signal is that
    introducedSessionId in the raw JSON differs from the bill's current
    session label — i.e. the bill was introduced in a prior Parliament
    session and carried over into the current one.
    """
    if not bill.raw_data:
        return False
    intro_session = bill.raw_data.get("introducedSessionId")
    if intro_session and bill.session:
        intro_label = _SESSION_LABELS.get(intro_session)
        if intro_label and intro_label != bill.session:
            return True
    return False


def _format_date(d: "date") -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def compute_bill_status(bill: "HaBill") -> BillStatus:
    """
    Build a BillStatus for the bill detail page.

    Derives royal_assent_date from ha_bill_stage if not on the bill record.
    Stage states are assigned by finding the current stage index; all prior
    stages are marked completed, subsequent stages pending.
    """
    stages_raw: list["HaBillStage"] = bill.stages.order_by("stage_order").all()

    # Royal assent date: bill field first, then fall back to stage record
    royal_assent_date = bill.royal_assent_date
    if not royal_assent_date:
        for s in stages_raw:
            if _normalise(s.stage_name) == "royal assent" and s.stage_date:
                royal_assent_date = s.stage_date
                break

    # Fate + banner text
    if bill.is_act:
        fate = "act"
        banner = (
            f"Passed — Royal Assent {_format_date(royal_assent_date)}"
            if royal_assent_date
            else "Passed — now an Act of Parliament"
        )
    elif bill.is_defeated:
        fate = "defeated"
        banner = "Defeated in Parliament"
    elif _is_carried_over(bill):
        fate = "carried_over"
        intro_id = bill.raw_data.get("introducedSessionId") if bill.raw_data else None
        intro_label = _SESSION_LABELS.get(intro_id, "a previous session")
        banner = f"Carried over from {intro_label}"
    else:
        fate = "in_progress"
        stage_text = (
            get_display_name(bill.current_stage) if bill.current_stage else "In progress"
        )
        house_text = f" · {bill.current_house}" if bill.current_house else ""
        banner = f"{stage_text}{house_text}"

    # Last action date (most recent stage with a date)
    last_action_date = None
    for s in reversed(stages_raw):
        if s.stage_date:
            last_action_date = s.stage_date
            break

    # Build per-stage state list
    bill_is_done = bill.is_act or bill.is_defeated
    norm_current = _normalise(bill.current_stage) if bill.current_stage else None

    def _make_stage(s: "HaBillStage", state: str) -> StageState:
        return StageState(
            stage_name=s.stage_name,
            display_name=get_display_name(s.stage_name),
            house=s.house,
            stage_order=s.stage_order,
            stage_date=s.stage_date,
            state=state,
            group=stage_group(s.stage_name, s.house, bill.house_of_origin),
            descriptor=get_stage_descriptor(s.stage_name, s.house),
            has_debate=(_normalise(s.stage_name) not in NO_DEBATE_STAGE_NAMES),
        )

    if bill_is_done:
        stage_states = [
            _make_stage(s, "completed" if s.stage_date else "pending")
            for s in stages_raw
        ]
    else:
        # Find index of the current stage to split completed / current / pending
        current_idx = None
        if norm_current:
            for i, s in enumerate(stages_raw):
                if _normalise(s.stage_name) == norm_current:
                    current_idx = i
                    break

        stage_states = []
        for i, s in enumerate(stages_raw):
            if current_idx is not None:
                if i < current_idx:
                    state = "completed"
                elif i == current_idx:
                    state = "current"
                else:
                    state = "pending"
            else:
                state = "completed" if s.stage_date else "pending"
            stage_states.append(_make_stage(s, state))

    return BillStatus(
        fate=fate,
        banner_text=banner,
        current_stage_name=(
            get_display_name(bill.current_stage) if bill.current_stage else None
        ),
        current_stage_house=bill.current_house,
        last_action_date=last_action_date,
        royal_assent_date=royal_assent_date,
        stages=stage_states,
        is_act=bill.is_act,
        is_defeated=bill.is_defeated,
        is_carried_over=(fate == "carried_over"),
    )
