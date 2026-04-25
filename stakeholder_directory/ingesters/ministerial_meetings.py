"""
Ministerial meetings ingester for the stakeholder directory.

Reads GOV.UK quarterly transparency CSVs (ministers' meetings with external
organisations) and writes raw records to the staging table for normalisation.

Usage (within a Flask app context):
    from stakeholder_directory.ingesters.ministerial_meetings import ingest_ministerial_meetings

    result = ingest_ministerial_meetings(
        csv_path='downloads/dfe_meetings_q1_2025.csv',
        department='department_for_education',
        source_url='https://www.gov.uk/government/collections/dfe-ministers-transparency-data',
    )
    print(result)

Design spec: docs/stakeholder-directory-design.md, Sections 2.9 and 7.
"""
import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from stakeholder_directory.vocab import (
    load_internal_government,
    validate_against_vocab,
    InvalidVocabularyValueError,  # noqa: F401 — re-exported for callers
)
from stakeholder_directory.url_validator import validate_url_or_flag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class IngestionResult:
    rows_processed: int = 0
    rows_staged: int = 0       # records written to staging as 'pending'
    rows_excluded: int = 0     # records dropped as internal-government
    rows_flagged: int = 0      # subset of rows_staged with processing_notes set
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"IngestionResult(processed={self.rows_processed}, "
            f"staged={self.rows_staged}, "
            f"excluded={self.rows_excluded}, "
            f"flagged={self.rows_flagged}, "
            f"errors={len(self.errors)})"
        )


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> tuple[date, str | None]:
    """Parse a date string from GOV.UK transparency CSVs.

    Returns (date, flag_note). flag_note is non-None for imprecise dates
    (ranges, month-only). Raises ValueError if unparseable.
    """
    s = date_str.strip()
    if not s:
        raise ValueError('empty date string')

    # ISO: 2025-01-08
    try:
        return datetime.strptime(s, '%Y-%m-%d').date(), None
    except ValueError:
        pass

    # DD/MM/YYYY
    try:
        return datetime.strptime(s, '%d/%m/%Y').date(), None
    except ValueError:
        pass

    # Date range: "10-15 January 2024" or "10–15 January 2024"
    range_match = re.match(r'^(\d{1,2})\s*[-–]\s*\d{1,2}\s+([A-Za-z]+ \d{4})$', s)
    if range_match:
        start_day = range_match.group(1)
        month_year = range_match.group(2)
        try:
            d = datetime.strptime(f'{start_day} {month_year}', '%d %B %Y').date()
            return d, f'date range recorded as "{s}"; used start date'
        except ValueError:
            pass

    # Full date: "8 January 2025"
    try:
        return datetime.strptime(s, '%d %B %Y').date(), None
    except ValueError:
        pass

    # Month Year only: "January 2025"
    try:
        d = datetime.strptime(f'1 {s}', '%d %B %Y').date()
        return d, f'no specific day in date "{s}"; used 1st of month'
    except ValueError:
        pass

    raise ValueError(f'cannot parse date: {s!r}')


# ---------------------------------------------------------------------------
# Organisation parsing
# ---------------------------------------------------------------------------

def _split_orgs(org_str: str) -> list[str]:
    """Split a multi-organisation field on semicolons. Returns [] for blank input."""
    if not org_str or not org_str.strip():
        return []
    return [o.strip() for o in org_str.split(';') if o.strip()]


# ---------------------------------------------------------------------------
# Internal government filtering
# ---------------------------------------------------------------------------

def _matching_internal_govt_variant(org_name: str, internal_govt: list[str]) -> str | None:
    """Return the first internal-government variant that matches org_name as a whole word,
    or None if no match. Matching is case-insensitive word-boundary regex."""
    name_lower = org_name.lower()
    for variant in internal_govt:
        pattern = r'\b' + re.escape(variant.lower()) + r'\b'
        if re.search(pattern, name_lower):
            return variant
    return None


def _is_internal_government(org_name: str, internal_govt: list[str]) -> bool:
    """True if any internal-government variant matches org_name as a whole word."""
    return _matching_internal_govt_variant(org_name, internal_govt) is not None


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> list[dict]:
    """Read a CSV with automatic encoding handling.

    Tries UTF-8-sig first (transparently handles BOM and regular UTF-8).
    Falls back to CP1252 on UnicodeDecodeError (common for Windows Excel exports).
    """
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f))
    except UnicodeDecodeError:
        logger.debug('UTF-8 decode failed for %s, retrying as CP1252', path.name)

    with open(path, encoding='cp1252', newline='') as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

def _detect_columns(fieldnames: list[str]) -> dict[str, str | None]:
    """Map logical column roles to actual CSV header strings.

    Raises ValueError if required columns (minister, date, organisation) cannot be found.
    """
    def find(*keywords: str) -> str | None:
        for h in fieldnames:
            h_l = h.lower().strip()
            if any(kw in h_l for kw in keywords):
                return h
        return None

    minister_col = find('minister')
    date_col = find('date')
    org_col = find('organisation', 'organization')
    purpose_col = find('purpose', 'subject')

    missing = [name for name, col in [('minister', minister_col), ('date', date_col), ('organisation', org_col)] if not col]
    if missing:
        raise ValueError(
            f'Cannot detect required CSV columns: {missing}. '
            f'Available headers: {list(fieldnames)}'
        )

    return {
        'minister': minister_col,
        'date': date_col,
        'organisation': org_col,
        'purpose': purpose_col,  # may be None if column not present
    }


# ---------------------------------------------------------------------------
# Staging write (idempotent)
# ---------------------------------------------------------------------------

def _write_to_staging(records: list[dict], source_url: str) -> int:
    """Insert records into staging, skipping any that already exist.

    Each record must carry a '_status' key ('pending', 'rejected', or 'errored').
    Bulk-fetches existing keys for source_url to avoid N+1 queries.
    Returns count of records actually written.
    """
    from extensions import db
    from stakeholder_directory.ingesters.staging import StagingMinisterialMeeting

    existing_rows = db.session.query(
        StagingMinisterialMeeting.raw_organisation_name,
        StagingMinisterialMeeting.minister_name,
        StagingMinisterialMeeting.meeting_date,
    ).filter(StagingMinisterialMeeting.source_url == source_url).all()
    existing_keys = {(r[0], r[1], r[2]) for r in existing_rows}

    written = 0
    for rec in records:
        status = rec.pop('_status')
        key = (rec['raw_organisation_name'], rec['minister_name'], rec['meeting_date'])
        if key in existing_keys:
            logger.debug('Skipping duplicate: %s / %s / %s', *key)
            continue
        db.session.add(StagingMinisterialMeeting(
            **rec,
            ingested_at=datetime.utcnow(),
            processing_status=status,
        ))
        written += 1

    if written:
        db.session.commit()

    return written


# ---------------------------------------------------------------------------
# Main ingester
# ---------------------------------------------------------------------------

def ingest_ministerial_meetings(
    csv_path,
    department: str,
    source_url: str,
    dry_run: bool = False,
) -> IngestionResult:
    """Ingest a GOV.UK ministerial meetings transparency CSV into the staging table.

    Args:
        csv_path:   Path to the CSV file (str or Path).
        department: Snake-case key from the departments vocab
                    (e.g. 'department_for_education'). Must be valid.
        source_url: GOV.UK collection page or direct CSV URL this file came from.
        dry_run:    If True, parse and count without writing to the database.

    Returns:
        IngestionResult with counts and any row-level errors.

    Raises:
        InvalidVocabularyValueError: if department is not in the departments vocab.
    """
    # Fail immediately on bad department — before any I/O
    validate_against_vocab(department, 'departments')

    # URL validation: once per run (stub until full impl; see url_validator.py)
    validate_url_or_flag(source_url, org_id=0)

    internal_govt = load_internal_government()
    path = Path(csv_path)
    rows = _read_csv(path)

    if not rows:
        return IngestionResult()

    cols = _detect_columns(list(rows[0].keys()))

    result = IngestionResult()
    all_records: list[dict] = []  # all rows: pending + rejected + errored

    for row_num, row in enumerate(rows, start=2):  # row 1 is the CSV header
        # Skip fully-blank rows
        if not any((v or '').strip() for v in row.values()):
            continue

        result.rows_processed += 1

        minister = (row.get(cols['minister']) or '').strip()
        raw_date = (row.get(cols['date']) or '').strip()
        raw_orgs = (row.get(cols['organisation']) or '').strip()
        purpose_col = cols.get('purpose')
        purpose = (row.get(purpose_col) or '').strip() if purpose_col else ''

        csv_row_json = json.dumps(dict(row))

        # Parse date — on failure persist an errored row
        try:
            meeting_date, date_flag = _parse_date(raw_date)
        except ValueError as exc:
            error_msg = f'Row {row_num}: {exc}'
            result.errors.append(error_msg)
            all_records.append({
                '_status': 'errored',
                'raw_organisation_name': raw_orgs or '',
                'minister_name': minister,
                'department': department,
                'meeting_date': None,
                'meeting_purpose': purpose or None,
                'source_url': source_url,
                'source_csv_row': csv_row_json,
                'processing_notes': f'date parse error: {exc}',
            })
            continue

        # Parse organisations — on empty persist an errored row
        orgs = _split_orgs(raw_orgs)
        if not orgs:
            error_msg = f'Row {row_num} ({minister}, {raw_date}): empty organisation field'
            result.errors.append(error_msg)
            all_records.append({
                '_status': 'errored',
                'raw_organisation_name': '',
                'minister_name': minister,
                'department': department,
                'meeting_date': meeting_date,
                'meeting_purpose': purpose or None,
                'source_url': source_url,
                'source_csv_row': csv_row_json,
                'processing_notes': 'empty organisation field',
            })
            continue

        for org_name in orgs:
            matched_variant = _matching_internal_govt_variant(org_name, internal_govt)
            if matched_variant:
                logger.info(
                    'Row %d: rejecting internal-government attendee "%s" (matched "%s")',
                    row_num, org_name, matched_variant,
                )
                result.rows_excluded += 1
                all_records.append({
                    '_status': 'rejected',
                    'raw_organisation_name': org_name,
                    'minister_name': minister,
                    'department': department,
                    'meeting_date': meeting_date,
                    'meeting_purpose': purpose or None,
                    'source_url': source_url,
                    'source_csv_row': csv_row_json,
                    'processing_notes': f'excluded: internal government ("{matched_variant}")',
                })
                continue

            notes_parts = []
            if date_flag:
                notes_parts.append(date_flag)
            notes_str = '; '.join(notes_parts) if notes_parts else None

            if notes_str:
                result.rows_flagged += 1

            all_records.append({
                '_status': 'pending',
                'raw_organisation_name': org_name,
                'minister_name': minister,
                'department': department,
                'meeting_date': meeting_date,
                'meeting_purpose': purpose or None,
                'source_url': source_url,
                'source_csv_row': csv_row_json,
                'processing_notes': notes_str,
            })
            result.rows_staged += 1

    if not dry_run and all_records:
        _write_to_staging(all_records, source_url)

    return result
