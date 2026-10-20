"""
Strict validation of headline statistic refreshes (Oct 2026 fix).

The old govuk_bulletin path sent the first 4,000 characters of raw HTML (the
<head>) to Gemini with a prompt containing example values, and accepted
whatever came back: placeholder copies ("3.2 %"), years as values ("2015
None"), word counts ("7257 words"). These tests pin the replacement:

- real release snippets (tests/fixtures/stats/) are read by per-stat patterns
  and structured parsers, and accepted only with verbatim evidence;
- bad Gemini answers (placeholder copy, year as value, wrong unit, evidence
  not in the source) are rejected;
- a rejected or unconfirmed value never overwrites the stored one.

Run: python -m pytest tests/test_stats_validation.py -v
All network calls are mocked.
"""

from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "stats"
TODAY = date(2026, 10, 20)


@pytest.fixture(scope="module")
def test_app():
    from flask import Flask
    from extensions import db as _db
    import hansard_archive.models      # noqa: F401
    import cache_models                # noqa: F401
    import stakeholder_directory.models  # noqa: F401
    import stakeholder_directory.ingesters.staging  # noqa: F401

    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
    app.config.update({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope="module")
def db(test_app):
    from extensions import db as _db
    return _db


def _fixture_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _resp(json_data=None, text=None, content=None):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    if json_data is not None:
        r.json.return_value = json_data
        r.text = json.dumps(json_data)
    else:
        r.json.side_effect = ValueError("not json")
    if text is not None:
        r.text = text
    r.content = content if content is not None else (r.text or "").encode("utf-8")
    return r


def _router(routes):
    """requests.get replacement: first route whose key is in the URL (or uri param) wins."""
    def _get(url, params=None, headers=None, timeout=None):
        target = url + " " + json.dumps(params or {})
        for key, resp in routes.items():
            if key in target:
                return resp
        raise AssertionError(f"unexpected GET {url} {params}")
    return _get


def _gemini(**out):
    return _resp({"candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}]})


_seq = iter(range(10_000))


def _make_stat(db, source_id, source_type="govuk_bulletin", latest_value=None,
               unit=None, period_label=None, extraction_config=None):
    from hansard_archive.models import HeadlineStat
    stat = HeadlineStat(
        theme_slug=f"theme-{next(_seq)}", source_type=source_type, source_id=source_id,
        display_label="Test stat", display_hint="raw", geography="UK",
        latest_value=latest_value, unit=unit, period_label=period_label,
        source_url="https://www.gov.uk/old-landing-page",
        extraction_config=json.dumps(extraction_config) if extraction_config else None,
        last_refreshed=datetime(2026, 1, 1), last_success=datetime(2026, 1, 1),
    )
    db.session.add(stat)
    db.session.commit()
    return stat


def _refresh(test_app, stat, gemini_key="", dry_run=False):
    from stats_refresh import _refresh_stat
    with test_app.app_context():
        return _refresh_stat(stat, gemini_key=gemini_key, dry_run=dry_run, today=TODAY)


# ── Routes built from the real (trimmed) fixtures ─────────────────────────────

def _crime_routes():
    return {"crimeinenglandandwales": _resp(_fixture_json("ons_crime_bulletin_ye_mar_2026.json"))}


def _housing_routes():
    fx = _fixture_json("govuk_housing_2024_25.json")
    html_path = fx["edition"]["details"]["attachments"][0]["url"]
    return {
        "search.json": _resp({"results": []}),
        "/collections/net-supply-of-housing": _resp(fx["collection"]),
        html_path: _resp(fx["html_publication"]),
        fx["edition"]["base_path"]: _resp(fx["edition"]),
    }


def _road_routes():
    base = "/government/statistics/reported-road-casualties-great-britain-final-results-2025"
    html = base + "/rrcgb-2025-introduction-and-main-findings"
    return {
        "search.json": _resp({"results": []}),
        "/collections/road-safety-statistics": _resp({"links": {"documents": [
            {"base_path": base, "title": "Reported road casualties Great Britain, final results: 2025",
             "public_updated_at": "2026-07-30T09:30:00+01:00"},
            {"base_path": "/government/statistics/reported-road-casualties-great-britain-annual-report-2024",
             "title": "Reported road casualties Great Britain, annual report: 2024",
             "public_updated_at": "2025-09-24T09:30:00+01:00"},
        ]}}),
        html: _resp({"details": {"body": (FIXTURES / "govuk_rrcgb_2025_main_findings.html").read_text(encoding="utf-8")}}),
        base: _resp({"base_path": base, "first_published_at": "2026-07-30T09:30:00+01:00",
                     "details": {"attachments": [
                         {"title": "RRCGB 2025: Introduction and main findings", "url": html}]}}),
    }


def _fbi_routes(csv_text=None):
    csv_url = "https://assets.publishing.service.gov.uk/media/x/fbi_timeseries_2024_25.csv"
    if csv_text is None:
        csv_text = (FIXTURES / "defra_fbi_timeseries_trimmed.csv").read_text(encoding="utf-8")
    return {
        "/statistics/farm-business-income": _resp({
            "base_path": "/government/statistics/farm-business-income",
            "public_updated_at": "2026-09-08T15:38:03+01:00",
            "details": {"attachments": [
                {"title": "Farm Income Dataset (machine readable version)", "url": csv_url}]}}),
        csv_url: _resp(text=csv_text),
    }


# ── Real release snippets, read by patterns / parsers (no model) ─────────────

class TestRealSourcesAccepted:

    def test_crime_bulletin_pattern(self, test_app, db):
        from hansard_archive.models import StatObservation
        with test_app.app_context():
            stat = _make_stat(db, "ons-crime-survey", latest_value="7257", unit="words")
            with patch("requests.get", side_effect=_router(_crime_routes())), \
                 patch("requests.post") as post:
                assert _refresh(test_app, stat) == "ok"
            post.assert_not_called()      # no model needed when the pattern matches
            assert stat.latest_value == "9.6"
            assert stat.unit == "million incidents"            # from config, not the source
            assert stat.period_label == "year ending March 2026"
            assert stat.source_wording == "9.6 million incidents of CSEW headline crime"
            obs = StatObservation.query.filter_by(headline_stat_id=stat.id).one()
            assert (obs.period_start, obs.period_end) == (date(2025, 4, 1), date(2026, 3, 31))
            assert obs.release_date == date(2026, 7, 22)

    def test_housing_latest_edition_html_publication(self, test_app, db):
        with test_app.app_context():
            stat = _make_stat(db, "dclg-housebuilding", latest_value="244,000")
            with patch("requests.get", side_effect=_router(_housing_routes())):
                assert _refresh(test_app, stat) == "ok"
            assert stat.latest_value == "208,600"
            assert stat.period_label == "2024-25"
            assert stat.release_date == date(2025, 11, 20)
            assert stat.source_wording.startswith("Annual housing supply in England amounted to 208,600")
            assert "2024-to-2025" in stat.source_url

    def test_road_casualties_picks_newest_edition(self, test_app, db):
        with test_app.app_context():
            stat = _make_stat(db, "dft-road-casualties", latest_value="2023")
            with patch("requests.get", side_effect=_router(_road_routes())):
                assert _refresh(test_app, stat) == "ok"
            assert stat.latest_value == "29,918"
            assert stat.period_label == "2025"
            assert stat.unit == "people"

    def test_farm_income_csv_row_filters(self, test_app, db):
        with test_app.app_context():
            stat = _make_stat(db, "defra-farm-income", latest_value="3.2", unit="%")
            with patch("requests.get", side_effect=_router(_fbi_routes())):
                assert _refresh(test_app, stat) == "ok"
            # All Types / current prices / latest year — not Cereals, not 2023/24
            assert stat.latest_value == "71,200"
            assert stat.period_label == "2024/25"
            assert stat.unit == "£ per farm"
            assert stat.source_url == "https://www.gov.uk/government/statistics/farm-business-income"


# ── Gemini fallback: bad answers rejected, good answer accepted ───────────────

def _crime_cfg_without_patterns():
    from stats_sources import STAT_SOURCES
    cfg = copy.deepcopy(STAT_SOURCES["ons-crime-survey"])
    cfg["patterns"] = [r"(?P<value>\d+) wording that no longer exists"]
    return cfg


class TestGeminiFallback:

    def _run(self, test_app, db, gemini_out, previous="9.4"):
        from stats_sources import STAT_SOURCES
        with test_app.app_context():
            stat = _make_stat(db, "ons-crime-survey", latest_value=previous,
                              unit="million incidents", period_label="year ending March 2025")
            with patch.dict(STAT_SOURCES, {"ons-crime-survey": _crime_cfg_without_patterns()}), \
                 patch("requests.get", side_effect=_router(_crime_routes())), \
                 patch("requests.post", return_value=_gemini(**gemini_out)) as post:
                result = _refresh(test_app, stat, gemini_key="fake-key")
            return result, stat, post

    def test_good_answer_accepted(self, test_app, db):
        result, stat, post = self._run(test_app, db, {
            "value": "9.6", "unit": "million incidents", "period_label": "year ending March 2026",
            "evidence": "9.6 million incidents of CSEW headline crime"})
        assert result == "ok"
        assert stat.latest_value == "9.6"
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        assert "9.6 million incidents" in prompt          # model sees body text, not <head>
        assert "<script" not in prompt

    def test_placeholder_copy_rejected(self, test_app, db):
        # What the old prompt produced: its own example value and period.
        result, stat, _ = self._run(test_app, db, {
            "value": "3.2", "unit": "%", "period_label": "Q3 2024",
            "evidence": "The headline figure was 3.2% in Q3 2024."})
        assert result == "failed"
        assert stat.latest_value == "9.4"

    def test_year_as_value_rejected(self, test_app, db):
        result, stat, _ = self._run(test_app, db, {
            "value": "2026", "unit": "million incidents", "period_label": "year ending March 2026",
            "evidence": ("The latest figures from the Crime Survey for England and Wales (CSEW) "
                         "year ending (YE) March 2026 showed that in the last year there were an estimated:")})
        assert result == "failed"
        assert stat.latest_value == "9.4"

    def test_wrong_unit_rejected(self, test_app, db):
        # Verbatim sentence, value in range, but a percentage, not incidents.
        result, stat, _ = self._run(test_app, db, {
            "value": "10", "unit": "%", "period_label": "year ending March 2026",
            "evidence": ("however, there was a 10% increase in the number of fraud victims "
                         "(to 3.8 million victims) compared with the previous year's survey (3.4 million victims)")})
        assert result == "failed"
        assert stat.latest_value == "9.4"

    def test_invented_evidence_rejected(self, test_app, db):
        result, stat, _ = self._run(test_app, db, {
            "value": "9.9", "unit": "million incidents", "period_label": "year ending March 2026",
            "evidence": "9.9 million incidents of CSEW headline crime"})
        assert result == "failed"
        assert stat.latest_value == "9.4"

    def test_unparseable_period_needs_review(self, test_app, db):
        result, stat, _ = self._run(test_app, db, {
            "value": "9.6", "unit": "million incidents", "period_label": "the latest year",
            "evidence": "9.6 million incidents of CSEW headline crime"})
        assert result == "needs_review"
        assert stat.latest_value == "9.4"

    def test_no_gemini_key_fails_without_guessing(self, test_app, db):
        from stats_sources import STAT_SOURCES
        with test_app.app_context():
            stat = _make_stat(db, "ons-crime-survey", latest_value="9.4")
            with patch.dict(STAT_SOURCES, {"ons-crime-survey": _crime_cfg_without_patterns()}), \
                 patch("requests.get", side_effect=_router(_crime_routes())), \
                 patch("requests.post") as post:
                assert _refresh(test_app, stat, gemini_key="") == "failed"
            post.assert_not_called()
            assert stat.latest_value == "9.4"


class TestOldFailureModes:

    def test_head_only_page_cannot_yield_a_value(self, test_app, db):
        """The 2015 defence page head produced '2015 None'. It must now be rejected."""
        from stats_sources import html_to_text
        head = (FIXTURES / "govuk_defence_in_numbers_2015_head.html").read_text(encoding="utf-8")
        assert "lux-measurer" not in html_to_text(head)      # scripts/meta never reach the model
        with test_app.app_context():
            stat = _make_stat(db, "unconfigured-bulletin", latest_value="2.3", unit="% of GDP")
            with patch("requests.get", return_value=_resp(text=head)), \
                 patch("requests.post", return_value=_gemini(
                     value="2015", unit="None", period_label="2015",
                     evidence="UK defence in numbers 2015 - GOV.UK")):
                assert _refresh(test_app, stat, gemini_key="fake-key") == "failed"
            assert stat.latest_value == "2.3"

    def test_manual_config_is_skipped_and_not_fetched(self, test_app, db):
        with test_app.app_context():
            stat = _make_stat(db, "mod-defence-spending-gdp", latest_value="2015 None")
            with patch("requests.get") as get:
                assert _refresh(test_app, stat) == "skipped"
            get.assert_not_called()


# ── Previous value retained; dry run writes nothing ──────────────────────────

class TestStoredValueProtection:

    def test_failure_keeps_every_stored_field(self, test_app, db):
        from hansard_archive.models import HeadlineStat, StatObservation
        with test_app.app_context():
            stat = _make_stat(db, "defra-farm-income", latest_value="47,900", unit="£ per farm",
                              period_label="2023/24")
            stat.source_wording = "previous wording"
            db.session.commit()
            bad_csv = (FIXTURES / "defra_fbi_timeseries_trimmed.csv").read_text(encoding="utf-8")
            bad_csv = bad_csv.replace('"71,200"', '"2,071,200"')        # out of bounds
            with patch("requests.get", side_effect=_router(_fbi_routes(bad_csv))):
                assert _refresh(test_app, stat) == "failed"
            row = db.session.get(HeadlineStat, stat.id)
            assert (row.latest_value, row.unit, row.period_label, row.source_wording) == \
                ("47,900", "£ per farm", "2023/24", "previous wording")
            assert row.last_success == datetime(2026, 1, 1)
            assert row.last_refreshed > datetime(2026, 1, 1)
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0

    def test_dry_run_writes_nothing(self, test_app, db):
        from hansard_archive.models import StatObservation
        with test_app.app_context():
            stat = _make_stat(db, "ons-crime-survey", latest_value="9.4")
            with patch("requests.get", side_effect=_router(_crime_routes())):
                assert _refresh(test_app, stat, dry_run=True) == "ok"
            db.session.refresh(stat)
            assert stat.latest_value == "9.4"
            assert stat.last_refreshed == datetime(2026, 1, 1)
            assert StatObservation.query.filter_by(headline_stat_id=stat.id).count() == 0

    def test_ons_stale_next_release_needs_review(self, test_app, db):
        with test_app.app_context():
            stat = _make_stat(db, "IHYQ", source_type="ons_timeseries", latest_value="0.4", unit="%")
            stale = {"description": {"title": "GDP q/q", "cdid": "IHYQ", "releaseDate": "2026-08-12T23:00:00.000Z",
                                     "nextRelease": "30 September 2026"},
                     "quarters": [{"label": "2026 Q2", "value": "0.4"}]}
            with patch("requests.get", return_value=_resp(stale)):
                assert _refresh(test_app, stat) == "needs_review"
            assert stat.latest_value == "0.4"


# ── validate_candidate unit tests ─────────────────────────────────────────────

class _Stat:
    source_id, theme_slug, unit, display_label, geography = "x", "t", None, "x", "UK"


def _text_cand(value, evidence, source_text, **kw):
    cand = {"value": value, "evidence": evidence, "evidence_kind": "text",
            "source_text": source_text, "period_label": "2025",
            "period_start": date(2025, 1, 1), "period_end": date(2025, 12, 31), "review": []}
    cand.update(kw)
    return cand


class TestValidateCandidate:
    CFG = {"unit": "%", "min": 0, "max": 100}

    def _v(self, cand, cfg=None):
        from stats_refresh import validate_candidate
        return validate_candidate(_Stat(), self.CFG if cfg is None else cfg, cand, TODAY)

    def test_value_must_be_whole_number_in_evidence(self):
        status, reasons = self._v(_text_cand("3.2", "Rate was 13.2% in 2025.", "Rate was 13.2% in 2025."))
        assert status == "failed"
        assert any("does not contain the value" in r for r in reasons)

    def test_words_are_not_values(self):
        for bad in ("7257 words", "2015 None", "£2,500", "2023 casualties"):
            status, _ = self._v(_text_cand(bad, bad, bad))
            assert status == "failed", bad

    def test_no_bounds_needs_review(self):
        status, reasons = self._v(_text_cand("5", "It was 5 in 2025.", "It was 5 in 2025."), cfg={"unit": "x"})
        assert status == "needs_review"
        assert any("bounds" in r for r in reasons)

    def test_structured_value_must_match_cell(self):
        cand = {"value": "55.8", "evidence": "sheet 6_1 ... = 45.79", "evidence_kind": "structured",
                "evidence_value": "45.79", "scale": 1, "period_label": "Q2 2026",
                "period_start": date(2026, 4, 1), "period_end": date(2026, 6, 30), "review": []}
        assert self._v(cand)[0] == "failed"
        cand.update(evidence="sheet 6_1 ... = 55.797147107727305", evidence_value="55.797147107727305")
        assert self._v(cand)[0] == "ok"

    def test_stale_period_needs_review(self):
        cand = _text_cand("5", "It was 5 in 2019.", "It was 5 in 2019.",
                          period_start=date(2019, 1, 1), period_end=date(2019, 12, 31))
        status, reasons = self._v(cand, cfg={**self.CFG, "max_period_age_days": 400})
        assert status == "needs_review"
        assert any("moved or stopped" in r for r in reasons)

    def test_prompt_has_no_example_values(self):
        from stats_refresh import _EXTRACT_PROMPT
        assert not re.search(r"\d", _EXTRACT_PROMPT)


# ── Structured parsers ────────────────────────────────────────────────────────

def _ods(rows_by_sheet):
    t, txt = "urn:oasis:names:tc:opendocument:xmlns:table:1.0", "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    off = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    sheets = []
    for name, rows in rows_by_sheet.items():
        xml_rows = []
        for r in rows:
            cells = "".join(
                f'<table:table-cell office:value-type="float" office:value="{c}"><text:p>{c}</text:p></table:table-cell>'
                if isinstance(c, float) else f"<table:table-cell><text:p>{c}</text:p></table:table-cell>"
                for c in r)
            xml_rows.append(f"<table:table-row>{cells}</table:table-row>")
        sheets.append(f'<table:table table:name="{name}">{"".join(xml_rows)}</table:table>')
    content = (f'<office:document-content xmlns:office="{off}" xmlns:table="{t}" xmlns:text="{txt}">'
               f'<office:body><office:spreadsheet>{"".join(sheets)}</office:spreadsheet></office:body>'
               f'</office:document-content>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", content)
    return buf.getvalue()


class TestStructuredParsers:

    def test_renewables_ods_last_quarter(self):
        from stats_sources import STAT_SOURCES, fetch_govuk_ods
        ods = _ods({"6_1": [
            ["ELECTRICITY GENERATED (GWh)", "2026 1st quarter", "2026 2nd quarter"],
            ["TOTAL", 44054.3, 38255.64],
            [],
            ["SHARES OF ELECTRICITY GENERATED (%)", "2026 1st quarter", "2026 2nd quarter"],
            ["Onshore wind", 15.04, 11.56],
            ["All renewables", 53.262798750350612, 55.797147107727305],
        ]})
        page = {"base_path": "/government/statistics/energy-trends-section-6-renewables",
                "public_updated_at": "2026-10-08T09:30:13+01:00",
                "details": {"attachments": [{"title": "Energy Trends renewables quarterly tables (ODS)",
                                             "url": "https://assets.example/Renewables.ods"}]}}
        routes = {"energy-trends-section-6-renewables": _resp(page), "Renewables.ods": _resp(content=ods)}
        with patch("requests.get", side_effect=_router(routes)):
            cand = fetch_govuk_ods(_Stat(), STAT_SOURCES["desnz-renewables-share"], TODAY)
        assert cand["value"] == "55.8"
        assert cand["period_label"] == "Q2 2026"
        assert (cand["period_start"], cand["period_end"]) == (date(2026, 4, 1), date(2026, 6, 30))

    def test_nhs_total_row_and_mismatch(self):
        from stats_sources import STAT_SOURCES, fetch_nhs_ae
        cols = ["Period", "Org Code"] + [f"A&E attendances {k}" for k in "ABCDEF"] + \
               [f"Attendances over 4hrs {k}" for k in "ABCDEF"]

        def csv_text(total_over):
            rows = [",".join(cols),
                    ",".join(["MSitAE-SEPTEMBER-2026", "R1"] + ["100", "0", "0", "0", "0", "0"] + ["25", "0", "0", "0", "0", "0"]),
                    ",".join(["MSitAE-SEPTEMBER-2026", "R2"] + ["300", "0", "0", "0", "0", "0"] + ["75", "0", "0", "0", "0", "0"]),
                    ",".join(["TOTAL", "TOTAL"] + ["400", "0", "0", "0", "0", "0"] + [str(total_over), "0", "0", "0", "0", "0"])]
            return "\n".join(rows)
        index = '<a href="https://www.england.nhs.uk/x/ae-waiting-times-and-activity/ae-attendances-and-emergency-admissions-2026-27/">'
        year = '<a href="https://www.england.nhs.uk/s/September-2026-CSV.csv">Monthly A&amp;E September 2026 (CSV, 28KB)</a>' \
               '<a href="https://www.england.nhs.uk/s/August-2026-CSV.csv">Monthly A&amp;E August 2026 (CSV, 28KB)</a>'
        cfg = STAT_SOURCES["nhs-ae-performance"]
        for over, expect_review in ((100, False), (90, True)):
            routes = {"September-2026-CSV.csv": _resp(text=csv_text(over)),
                      "admissions-2026-27": _resp(text=year),
                      "ae-waiting-times-and-activity/": _resp(text=index)}
            with patch("requests.get", side_effect=_router(routes)):
                cand = fetch_nhs_ae(_Stat(), cfg, TODAY)
            assert cand["period_label"] == "September 2026"
            assert bool(cand["review"]) is expect_review
        assert cand["value"] == "77.5"   # 1 - 90/400 from the TOTAL row


# ── Period parsing additions ──────────────────────────────────────────────────

class TestPeriodParsing:

    def test_year_ending(self):
        from stats_refresh import _parse_govuk_period
        assert _parse_govuk_period("year ending March 2026") == (date(2025, 4, 1), date(2026, 3, 31))
        assert _parse_govuk_period("year ending December 2025") == (date(2025, 1, 1), date(2025, 12, 31))

    def test_govuk_title_financial_year(self):
        from stats_refresh import _parse_govuk_period
        assert _parse_govuk_period("2024 to 2025") == (date(2024, 4, 1), date(2025, 3, 31))

    def test_ons_rolling_three_months(self):
        from stats_refresh import _parse_ons_period
        assert _parse_ons_period("2026 JUN-AUG", "months") == (date(2026, 6, 1), date(2026, 8, 31))
        assert _parse_ons_period("2025 NOV-JAN", "months") == (date(2025, 11, 1), date(2026, 1, 31))
