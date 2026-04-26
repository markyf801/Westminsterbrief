"""
Lobbying register ingester for the stakeholder directory.

Reads a quarterly file from the Office of the Registrar of Consultant
Lobbyists and stages each firm-client relationship as two records:
  - record_type='firm'   → source_type='lobbying_register'
  - record_type='client' → source_type='lobbying_register_client'

File format (as of 2022–2023 quarterly files):
  Excel (.xlsx), single sheet, two columns:
    Row 1: merged title  ("October to December 2022 Lobbying Return Clients")
    Row 2: description note
    Row 3: headers       ("Registered organisation name", "Client name")
    Row 4+: data
  No subject-matter or registration-ID columns are present in published files.
  CSV files (.csv) with the same two column names are also accepted (used by tests).

Download source: https://registrarofconsultantlobbyists.org.uk/the-register/
Note: the site blocks automated requests (403); files must be downloaded manually.
"""
import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from stakeholder_directory.vocab import load_internal_government

logger = logging.getLogger(__name__)

_REGISTER_URL = 'https://registrarofconsultantlobbyists.org.uk/the-register/'

_COL_FIRM = 'Registered organisation name'
_COL_CLIENT = 'Client name'

# Register placeholder values that carry no stakeholder information.
# These appear when a registrant has no clients to disclose for the quarter,
# or has declined to name a client, or has entered a dummy value.
_PLACEHOLDER_NAMES: frozenset[str] = frozenset({
    'intentionally blank',
    'none',
    'n/a',
    'nil return',
    'not applicable',
    'various',
})


@dataclass
class IngestionResult:
    rows_read: int = 0
    rows_staged: int = 0
    rows_skipped_internal_govt: int = 0
    rows_skipped_duplicate: int = 0
    rows_skipped_placeholder: int = 0
    rows_errored: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"IngestionResult("
            f"read={self.rows_read}, "
            f"staged={self.rows_staged}, "
            f"skipped_govt={self.rows_skipped_internal_govt}, "
            f"skipped_dup={self.rows_skipped_duplicate}, "
            f"skipped_placeholder={self.rows_skipped_placeholder}, "
            f"errored={self.rows_errored}, "
            f"errors={len(self.errors)})"
        )


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _read_register_file(source_path: str) -> list[dict]:
    """Return list of {_COL_FIRM: ..., _COL_CLIENT: ...} dicts from file."""
    path = Path(source_path)
    if path.suffix.lower() == '.xlsx':
        return _read_xlsx(path)
    return _read_csv(path)


def _read_xlsx(path: Path) -> list[dict]:
    try:
        import openpyxl
    except ImportError:
        raise ImportError(
            "openpyxl is required to read .xlsx files: pip install openpyxl"
        )
    wb = openpyxl.load_workbook(str(path))
    ws = wb.active
    rows = []
    # Row 1 = title, Row 2 = note, Row 3 = headers, Row 4+ = data
    for row in ws.iter_rows(min_row=4, values_only=True):
        firm = (str(row[0]).strip() if row[0] is not None else '')
        client = (str(row[1]).strip() if row[1] is not None else '')
        if firm or client:
            rows.append({_COL_FIRM: firm, _COL_CLIENT: client})
    return rows


def _read_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


# ---------------------------------------------------------------------------
# Placeholder filter
# ---------------------------------------------------------------------------

def _is_placeholder(name: str) -> bool:
    """Return True if name is a known register placeholder value."""
    return name.strip().lower() in _PLACEHOLDER_NAMES


# ---------------------------------------------------------------------------
# Internal-government filter (same word-boundary logic as other ingesters)
# ---------------------------------------------------------------------------

def _is_internal_government(name: str, variants: list[str]) -> bool:
    name_lower = name.lower()
    return any(
        re.search(r'\b' + re.escape(v.lower()) + r'\b', name_lower)
        for v in variants
    )


# ---------------------------------------------------------------------------
# Staging helpers
# ---------------------------------------------------------------------------

def _check_duplicate(staging_model, firm_name: str, client_name: str,
                     quarter: str, record_type: str) -> bool:
    """Return True if this (firm, client, quarter, record_type) already staged."""
    from extensions import db
    return db.session.query(staging_model).filter_by(
        firm_name=firm_name,
        client_name=client_name,
        quarter=quarter,
        record_type=record_type,
    ).first() is not None


def _stage_record(
    staging_model,
    firm_name: str,
    client_name: str,
    firm_registration_id: str | None,
    record_type: str,
    quarter: str,
    quarter_start_date: date,
    quarter_end_date: date,
    source_url: str,
    internal_govt_variants: list[str],
    result: IngestionResult,
    dry_run: bool,
) -> bool:
    """Stage one firm or client record. Returns True if staged/counted."""
    raw_name = firm_name if record_type == 'firm' else client_name

    if _is_internal_government(raw_name, internal_govt_variants):
        result.rows_skipped_internal_govt += 1
        return False

    if dry_run:
        result.rows_staged += 1
        return True

    if _check_duplicate(staging_model, firm_name, client_name, quarter, record_type):
        result.rows_skipped_duplicate += 1
        return False

    from extensions import db
    if record_type == 'firm':
        notes = f"Client during {quarter}: {client_name}"
    else:
        notes = f"Represented by lobbyist during {quarter}: {firm_name}"

    row = staging_model(
        firm_name=firm_name,
        firm_registration_id=firm_registration_id or None,
        client_name=client_name,
        record_type=record_type,
        raw_organisation_name=raw_name,
        quarter=quarter,
        quarter_start_date=quarter_start_date,
        quarter_end_date=quarter_end_date,
        source_url=source_url,
        processing_status='pending',
        processing_notes=notes,
    )
    db.session.add(row)
    result.rows_staged += 1
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest_lobbying_register(
    source_path: str,
    quarter: str,
    quarter_start_date: date,
    quarter_end_date: date,
    source_url: str = '',
    dry_run: bool = False,
) -> IngestionResult:
    """Ingest a quarterly lobbying register file.

    Each row in the file represents a (firm, client) pair for the quarter.
    Two staging records are created per row:
      - record_type='firm':   the lobbying firm appears in the directory
      - record_type='client': the client appears as a lobbied organisation

    Args:
        source_path:         Local path to the downloaded register file (.xlsx or .csv).
        quarter:             Quarter label, e.g. "2022-Q4".
        quarter_start_date:  Inclusive start of the quarter.
        quarter_end_date:    Inclusive end of the quarter.
        source_url:          URL of the source register page (defaults to ORCL register).
        dry_run:             Count without writing to the database.

    Returns:
        IngestionResult with counts per action.
    """
    from stakeholder_directory.ingesters.staging import StagingLobbyingEntry

    if not source_url:
        source_url = _REGISTER_URL

    result = IngestionResult()
    internal_govt_variants = load_internal_government()

    try:
        raw_rows = _read_register_file(source_path)
    except Exception as exc:
        result.errors.append(f"Failed to read {source_path}: {exc}")
        return result

    for raw_row in raw_rows:
        firm_name = (raw_row.get(_COL_FIRM) or '').strip()
        client_name = (raw_row.get(_COL_CLIENT) or '').strip()
        result.rows_read += 1

        if not firm_name and not client_name:
            continue  # blank row

        if not firm_name:
            result.rows_errored += 1
            result.errors.append(
                f"Row {result.rows_read}: missing firm name (client={client_name!r})"
            )
            continue

        if not client_name:
            result.rows_errored += 1
            result.errors.append(
                f"Row {result.rows_read}: missing client name (firm={firm_name!r})"
            )
            continue

        if _is_placeholder(firm_name) or _is_placeholder(client_name):
            result.rows_skipped_placeholder += 1
            continue

        try:
            # Firm-as-organisation staging row
            _stage_record(
                StagingLobbyingEntry,
                firm_name=firm_name,
                client_name=client_name,
                firm_registration_id=None,
                record_type='firm',
                quarter=quarter,
                quarter_start_date=quarter_start_date,
                quarter_end_date=quarter_end_date,
                source_url=source_url,
                internal_govt_variants=internal_govt_variants,
                result=result,
                dry_run=dry_run,
            )
            # Client-as-organisation staging row
            _stage_record(
                StagingLobbyingEntry,
                firm_name=firm_name,
                client_name=client_name,
                firm_registration_id=None,
                record_type='client',
                quarter=quarter,
                quarter_start_date=quarter_start_date,
                quarter_end_date=quarter_end_date,
                source_url=source_url,
                internal_govt_variants=internal_govt_variants,
                result=result,
                dry_run=dry_run,
            )
        except Exception as exc:
            result.rows_errored += 1
            msg = f"Row {result.rows_read} ({firm_name!r} / {client_name!r}): {exc}"
            result.errors.append(msg)
            logger.exception("Error processing lobbying row %s", result.rows_read)

    if not dry_run:
        from extensions import db
        db.session.commit()

    return result
