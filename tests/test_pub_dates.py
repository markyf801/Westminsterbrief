"""
Tests for the shared publication-date helper (hansard_archive.discovery.pub_dates).

This is the single source of truth both the discovery worker (A2) and the
backfill/compare script (A3) depend on, so it carries its own coverage:
  - GOV.UK Content API first_published_at extraction
  - ONS datasets API release_date extraction (both stored URL forms)
  - NO fallback to public_timestamp / any other field (the INC-007 bug)
  - host dispatch + None handling

Run: python -m pytest tests/test_pub_dates.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hansard_archive.discovery.pub_dates import (
    fetch_govuk_first_published,
    fetch_ons_release_date,
    resolve_publication_date,
    _parse_iso_date,
)


def _mock_resp(status_code=200, json_body=None):
    r = MagicMock()
    r.status_code = status_code
    r.ok = 200 <= status_code < 300
    r.json.return_value = json_body or {}
    return r


# ---------------------------------------------------------------------------
# _parse_iso_date
# ---------------------------------------------------------------------------

class TestParseIsoDate:

    def test_full_iso_datetime(self):
        assert _parse_iso_date("2025-05-29T09:30:00+01:00") == date(2025, 5, 29)

    def test_date_only(self):
        assert _parse_iso_date("2025-05-29") == date(2025, 5, 29)

    def test_empty_string_returns_none(self):
        assert _parse_iso_date("") is None

    def test_none_returns_none(self):
        assert _parse_iso_date(None) is None

    def test_garbage_returns_none(self):
        assert _parse_iso_date("not-a-date") is None


# ---------------------------------------------------------------------------
# GOV.UK Content API
# ---------------------------------------------------------------------------

class TestFetchGovukFirstPublished:

    def test_extracts_first_published_at(self):
        http = MagicMock()
        http.get.return_value = _mock_resp(json_body={
            "first_published_at": "2025-05-29T09:30:00+01:00",
            "public_updated_at":  "2026-05-28T09:30:19+01:00",
        })
        result = fetch_govuk_first_published(
            "https://www.gov.uk/government/statistics/foo", http)
        assert result == date(2025, 5, 29)

    def test_does_not_fall_back_to_public_updated_at(self):
        """INC-007: when first_published_at is absent, return None — NEVER the
        updated date."""
        http = MagicMock()
        http.get.return_value = _mock_resp(json_body={
            "public_updated_at": "2026-05-28T09:30:19+01:00",
            "public_timestamp":  "2026-05-28T09:30:19+01:00",
        })
        result = fetch_govuk_first_published(
            "https://www.gov.uk/government/statistics/foo", http)
        assert result is None

    def test_404_returns_none(self):
        http = MagicMock()
        http.get.return_value = _mock_resp(status_code=404)
        assert fetch_govuk_first_published(
            "https://www.gov.uk/government/statistics/gone", http) is None

    def test_constructs_content_api_path(self):
        http = MagicMock()
        http.get.return_value = _mock_resp(json_body={"first_published_at": "2024-01-01"})
        fetch_govuk_first_published(
            "https://www.gov.uk/government/statistics/some-pub", http)
        called_url = http.get.call_args[0][0]
        assert called_url == "https://www.gov.uk/api/content/government/statistics/some-pub"


# ---------------------------------------------------------------------------
# ONS datasets API
# ---------------------------------------------------------------------------

class TestFetchOnsReleaseDate:

    _ROOT_WITH_VERSION = {
        "title": "Wellbeing",
        "links": {"latest_version": {
            "href": "https://api.beta.ons.gov.uk/v1/datasets/wellbeing/editions/time-series/versions/9"}},
    }
    _VERSION = {"release_date": "2023-11-28T00:00:00.000Z", "last_updated": "2023-12-11"}

    def test_follows_latest_version_for_release_date(self):
        """release_date lives on the version, reached via the dataset root."""
        http = MagicMock()
        http.get.side_effect = [
            _mock_resp(json_body=self._ROOT_WITH_VERSION),   # dataset root
            _mock_resp(json_body=self._VERSION),             # latest version
        ]
        result = fetch_ons_release_date("https://www.ons.gov.uk/datasets/wellbeing", http)
        assert result == date(2023, 11, 28)
        # first call = dataset root
        assert http.get.call_args_list[0][0][0] == "https://api.beta.ons.gov.uk/v1/datasets/wellbeing"
        # second call = the version href from the root
        assert "versions/9" in http.get.call_args_list[1][0][0]

    def test_prefers_release_date_on_root_when_present(self):
        http = MagicMock()
        http.get.return_value = _mock_resp(json_body={"release_date": "2024-03-21"})
        result = fetch_ons_release_date("https://www.ons.gov.uk/datasets/x", http)
        assert result == date(2024, 3, 21)
        assert http.get.call_count == 1   # no need to follow the version

    def test_version_url_form_read_directly(self):
        http = MagicMock()
        http.get.return_value = _mock_resp(json_body={"release_date": "2024-03-21"})
        result = fetch_ons_release_date(
            "https://api.beta.ons.gov.uk/v1/datasets/abc/editions/time-series/versions/9", http)
        assert result == date(2024, 3, 21)
        assert http.get.call_count == 1   # direct version read, no root fetch

    def test_no_version_and_no_root_date_returns_none(self):
        http = MagicMock()
        http.get.return_value = _mock_resp(json_body={"title": "No dates", "links": {}})
        assert fetch_ons_release_date("https://www.ons.gov.uk/datasets/x", http) is None

    def test_unextractable_dataset_id_returns_none(self):
        http = MagicMock()
        assert fetch_ons_release_date("https://www.ons.gov.uk/economy/bulletin", http) is None
        http.get.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_publication_date dispatch
# ---------------------------------------------------------------------------

class TestResolvePublicationDate:

    def test_govuk_url_dispatches_to_content_api(self):
        with patch("hansard_archive.discovery.pub_dates.fetch_govuk_first_published",
                   return_value=date(2025, 1, 1)) as m:
            result = resolve_publication_date("https://www.gov.uk/government/statistics/x")
        assert result == date(2025, 1, 1)
        m.assert_called_once()

    def test_ons_url_dispatches_to_ons_api(self):
        with patch("hansard_archive.discovery.pub_dates.fetch_ons_release_date",
                   return_value=date(2024, 6, 1)) as m:
            result = resolve_publication_date("https://www.ons.gov.uk/datasets/x")
        assert result == date(2024, 6, 1)
        m.assert_called_once()

    def test_unknown_host_returns_none_without_fetch(self):
        with patch("hansard_archive.discovery.pub_dates.fetch_govuk_first_published") as g, \
             patch("hansard_archive.discovery.pub_dates.fetch_ons_release_date") as o:
            result = resolve_publication_date("https://www.hesa.ac.uk/data/foo")
        assert result is None
        g.assert_not_called()
        o.assert_not_called()
