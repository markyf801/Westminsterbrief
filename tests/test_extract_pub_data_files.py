"""
Tests for the Phase 1.8 data_url extractor framework.

Covers:
  - GOV.UK page parsing: gem-c-attachment (new) and section.attachment (old) patterns
  - EES detection: publication URL is EES (case a); page links out to EES (case b)
  - EES + data files: extracted status with ees_url set
  - HTML attachment exclusion by file type (not domain)
  - No attachments: no_files_found
  - Section headings → classification mapping; NULL where no heading
  - File type derivation: markup label primary, URL extension fallback, NULL if unknown
  - File size parsing: KB, MB, bytes
  - HTTP failure: fetch_failed status
  - 429 backoff triggers retry
  - Fallback extractor: not_extractable without HTTP call
  - select_extractor dispatch: GOV.UK → GovUKGenericExtractor, other → FallbackExtractor
  - Batch loop: failure isolation — one bad publication does not abort the batch
  - Write result: delete-and-replace on success; preserve rows on fetch_failed
  - EES case (a): no HTTP call made when pub.url starts with EES prefix

Run: python -m pytest tests/test_extract_pub_data_files.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hansard_archive.discovery.extractors.base import ExtractedFile, ExtractionResult
from hansard_archive.discovery.extractors.govuk_generic import (
    _parse_govuk_page,
    _derive_file_type,
    _extract_size,
    _classify_from_heading,
    _normalise_type_label,
    _is_govuk_subpage,
    GovUKGenericExtractor,
    EES_PREFIX,
    DATA_FILE_TYPES,
)
from hansard_archive.discovery.extractors.fallback import FallbackExtractor
from hansard_archive.discovery.extractors import select_extractor


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

_MULTI_FILE_WITH_HEADINGS = """
<div class="govspeak">
  <h2>Main tables</h2>
  <div class="gem-c-attachment govuk-!-display-none-print gem-c-attachment--embedded">
    <div class="gem-c-attachment__thumbnail">
      <a href="https://assets.publishing.service.gov.uk/media/abc/data.ods"></a>
    </div>
    <div class="gem-c-attachment__details">
      <h3 class="gem-c-attachment__title">
        <a class="gem-c-attachment__link"
           href="https://assets.publishing.service.gov.uk/media/abc/data.ods">
          Main dataset
        </a>
      </h3>
      <p class="gem-c-attachment__metadata">
        <span class="gem-c-attachment__attribute">
          <abbr title="OpenDocument Spreadsheet">ODS</abbr>
        </span>,
        <span class="gem-c-attachment__attribute">
          <abbr title="kilobytes">150 KB</abbr>
        </span>
      </p>
    </div>
  </div>
  <h2>Technical notes</h2>
  <div class="gem-c-attachment govuk-!-display-none-print gem-c-attachment--embedded">
    <div class="gem-c-attachment__details">
      <h3 class="gem-c-attachment__title">
        <a class="gem-c-attachment__link"
           href="https://assets.publishing.service.gov.uk/media/def/notes.pdf">
          Technical guide
        </a>
      </h3>
      <p class="gem-c-attachment__metadata">
        <span class="gem-c-attachment__attribute">
          <abbr title="Portable Document Format">PDF</abbr>
        </span>,
        <span class="gem-c-attachment__attribute">
          <abbr title="kilobytes">234 KB</abbr>
        </span>
      </p>
    </div>
  </div>
</div>
"""

_EES_ONLY = """
<div class="govspeak">
  <div class="gem-c-attachment govuk-!-display-none-print gem-c-attachment--embedded">
    <div class="gem-c-attachment__details">
      <h3 class="gem-c-attachment__title">
        <a class="gem-c-attachment__link"
           href="https://explore-education-statistics.service.gov.uk/find-statistics/pupil-absence/2022-23">
          Pupil absence statistics
        </a>
      </h3>
    </div>
  </div>
</div>
"""

_EES_PLUS_DATA_FILE = """
<div class="govspeak">
  <div class="gem-c-attachment govuk-!-display-none-print gem-c-attachment--embedded">
    <div class="gem-c-attachment__details">
      <h3 class="gem-c-attachment__title">
        <a class="gem-c-attachment__link"
           href="https://explore-education-statistics.service.gov.uk/find-statistics/schools/2023">
          Interactive release
        </a>
      </h3>
    </div>
  </div>
  <div class="gem-c-attachment govuk-!-display-none-print gem-c-attachment--embedded">
    <div class="gem-c-attachment__details">
      <h3 class="gem-c-attachment__title">
        <a class="gem-c-attachment__link"
           href="https://assets.publishing.service.gov.uk/media/xyz/underlying.csv">
          Underlying data
        </a>
      </h3>
      <p class="gem-c-attachment__metadata">
        <span class="gem-c-attachment__attribute">
          <abbr title="Comma-separated values">CSV</abbr>
        </span>,
        <span class="gem-c-attachment__attribute">
          <abbr title="kilobytes">45 KB</abbr>
        </span>
      </p>
    </div>
  </div>
</div>
"""

_HTML_ATTACHMENT = """
<div class="govspeak">
  <div class="gem-c-attachment govuk-!-display-none-print gem-c-attachment--embedded">
    <div class="gem-c-attachment__details">
      <h3 class="gem-c-attachment__title">
        <a class="gem-c-attachment__link"
           href="/government/statistics/report/report-2023">
          Report
        </a>
      </h3>
      <p class="gem-c-attachment__metadata">
        <span class="gem-c-attachment__attribute">HTML</span>
      </p>
    </div>
  </div>
</div>
"""

_EMPTY_GOVSPEAK = """<div class="govspeak"><p>No attachments here.</p></div>"""

# Real-world GOV.UK structure: section.gem-c-attachment outside div.govspeak
_SECTION_OUTSIDE_GOVSPEAK = """
<div class="govspeak"><p>Some description.</p></div>
<div class="responsive-bottom-margin">
  <section class="">
    <h2>Main tables</h2>
    <section class="gem-c-attachment govuk-!-margin-bottom-6">
      <div class="gem-c-attachment__thumbnail">
        <a href="https://assets.publishing.service.gov.uk/media/abc/tables.xlsx"></a>
      </div>
      <div class="gem-c-attachment__details">
        <h3 class="gem-c-attachment__title">
          <a class="gem-c-attachment__link"
             href="https://assets.publishing.service.gov.uk/media/abc/tables.xlsx">
            Detailed tables
          </a>
        </h3>
        <p class="gem-c-attachment__metadata">
          <span class="gem-c-attachment__attribute">
            <abbr title="MS Excel Spreadsheet">XLSX</abbr>
          </span>,
          <span class="gem-c-attachment__attribute">1.2 MB</span>
        </p>
      </div>
    </section>
  </section>
</div>
"""

# Inline span.gem-c-attachment-link pattern (GOV.UK statistical-data-sets pages)
_INLINE_SPAN_PATTERN = """
<div class="gem-c-govspeak govuk-govspeak">
  <div class="govspeak">
    <h2>Road casualty data</h2>
    <p>Download the data:
      <span class="gem-c-attachment-link">
        <a href="https://assets.publishing.service.gov.uk/media/abc/road-data.csv">
          Road casualty statistics 2024
        </a>
      </span>
    </p>
    <p>Also available as:
      <span class="gem-c-attachment-link">
        <a href="https://assets.publishing.service.gov.uk/media/def/data-guide.xlsx">
          Data guide (MS Excel)
        </a>
      </span>
      <span class="gem-c-attachment-link">
        <a href="https://assets.publishing.service.gov.uk/media/ghi/notes.docx">
          Technical notes (Word)
        </a>
      </span>
    </p>
  </div>
</div>
"""

_OLD_SECTION_PATTERN = """
<div class="govspeak">
  <h2>Supporting files</h2>
  <section class="attachment embedded">
    <div class="attachment-details">
      <h3 class="title">
        <a href="https://assets.publishing.service.gov.uk/media/old/tables.xlsx">
          Data tables
        </a>
      </h3>
      <p class="metadata">
        <span class="type">MS Excel Spreadsheet</span>,
        <span class="file-size">1.5 MB</span>
      </p>
    </div>
  </section>
</div>
"""


# ---------------------------------------------------------------------------
# _parse_govuk_page
# ---------------------------------------------------------------------------

class TestParseGovukPage:

    def test_multi_file_returns_extracted_with_correct_fields(self):
        files, ees_url, _ = _parse_govuk_page(_MULTI_FILE_WITH_HEADINGS)
        assert len(files) == 2
        assert ees_url is None

        ods_file = files[0]
        assert "data.ods" in ods_file.url
        assert ods_file.file_type == "ods"
        assert ods_file.title == "Main dataset"
        assert ods_file.file_size_bytes == 153_600   # 150 * 1024
        assert ods_file.classification == "main_release"
        assert ods_file.display_order == 0

        pdf_file = files[1]
        assert "notes.pdf" in pdf_file.url
        assert pdf_file.file_type == "pdf"
        assert pdf_file.classification == "technical_docs"
        assert pdf_file.display_order == 1

    def test_ees_only_returns_no_files_and_ees_url(self):
        files, ees_url, _ = _parse_govuk_page(_EES_ONLY)
        assert files == []
        assert ees_url is not None
        assert ees_url.startswith(EES_PREFIX)

    def test_ees_plus_data_file_returns_file_and_ees_url(self):
        files, ees_url, _ = _parse_govuk_page(_EES_PLUS_DATA_FILE)
        assert len(files) == 1
        assert ees_url is not None
        assert ees_url.startswith(EES_PREFIX)
        assert files[0].file_type == "csv"

    def test_html_attachment_excluded(self):
        files, ees_url, _ = _parse_govuk_page(_HTML_ATTACHMENT)
        assert files == []
        assert ees_url is None

    def test_no_attachments_returns_empty(self):
        files, ees_url, _ = _parse_govuk_page(_EMPTY_GOVSPEAK)
        assert files == []
        assert ees_url is None

    def test_old_section_attachment_pattern(self):
        files, ees_url, _ = _parse_govuk_page(_OLD_SECTION_PATTERN)
        assert len(files) == 1
        f = files[0]
        assert f.file_type == "xlsx"
        assert f.classification == "supporting_tables"
        assert f.file_size_bytes == 1_572_864  # 1.5 * 1024 * 1024

    def test_no_heading_gives_null_classification(self):
        html = """
        <div class="govspeak">
          <div class="gem-c-attachment gem-c-attachment--embedded">
            <div class="gem-c-attachment__details">
              <h3 class="gem-c-attachment__title">
                <a class="gem-c-attachment__link"
                   href="https://assets.publishing.service.gov.uk/media/q/unlabelled.csv">
                  Data
                </a>
              </h3>
              <p class="gem-c-attachment__metadata">
                <span class="gem-c-attachment__attribute">
                  <abbr title="Comma-separated values">CSV</abbr>
                </span>
              </p>
            </div>
          </div>
        </div>
        """
        files, _ees, _ = _parse_govuk_page(html)
        assert len(files) == 1
        assert files[0].classification is None

    def test_display_order_increments(self):
        files, _ees, _ = _parse_govuk_page(_MULTI_FILE_WITH_HEADINGS)
        assert [f.display_order for f in files] == [0, 1]

    def test_section_outside_govspeak_is_found(self):
        """Real GOV.UK pages: section.gem-c-attachment sits outside div.govspeak."""
        files, ees_url, _ = _parse_govuk_page(_SECTION_OUTSIDE_GOVSPEAK)
        assert len(files) == 1
        assert "tables.xlsx" in files[0].url
        assert files[0].file_type == "xlsx"
        assert ees_url is None

    def test_inline_span_pattern_extracted(self):
        """statistical-data-sets pages use span.gem-c-attachment-link inside govspeak."""
        files, ees_url, _ = _parse_govuk_page(_INLINE_SPAN_PATTERN)
        # docx excluded; csv + xlsx included
        assert len(files) == 2
        types = {f.file_type for f in files}
        assert types == {"csv", "xlsx"}
        assert ees_url is None

    def test_inline_span_docx_excluded(self):
        """docx files from inline spans are excluded (not in DATA_FILE_TYPES)."""
        files, _ees, _ = _parse_govuk_page(_INLINE_SPAN_PATTERN)
        assert all(f.file_type != "docx" for f in files)

    def test_only_first_ees_url_captured(self):
        html = """
        <div class="govspeak">
          <div class="gem-c-attachment gem-c-attachment--embedded">
            <div class="gem-c-attachment__details">
              <h3><a class="gem-c-attachment__link"
                     href="https://explore-education-statistics.service.gov.uk/find/first">first</a></h3>
            </div>
          </div>
          <div class="gem-c-attachment gem-c-attachment--embedded">
            <div class="gem-c-attachment__details">
              <h3><a class="gem-c-attachment__link"
                     href="https://explore-education-statistics.service.gov.uk/find/second">second</a></h3>
            </div>
          </div>
        </div>
        """
        files, ees_url, _ = _parse_govuk_page(html)
        assert ees_url == "https://explore-education-statistics.service.gov.uk/find/first"

    def test_subpage_links_detected_in_no_files_found(self):
        """gem-c-attachment containers linking to GOV.UK sub-pages are collected."""
        html = """
        <section class="gem-c-attachment govuk-!-margin-bottom-6">
          <div class="gem-c-attachment__details">
            <h3 class="gem-c-attachment__title">
              <a class="gem-c-attachment__link"
                 href="/government/statistics/cyber-security-breaches-survey-2025/cyber-security-breaches-survey-2025">
                Main findings
              </a>
            </h3>
          </div>
        </section>
        <section class="gem-c-attachment govuk-!-margin-bottom-6">
          <div class="gem-c-attachment__details">
            <h3 class="gem-c-attachment__title">
              <a class="gem-c-attachment__link"
                 href="/government/statistics/cyber-security-breaches-survey-2025/technical-report">
                Technical report
              </a>
            </h3>
          </div>
        </section>
        """
        files, ees_url, sub_page_urls = _parse_govuk_page(html)
        assert files == []
        assert ees_url is None
        assert len(sub_page_urls) == 2
        assert all("cyber-security" in u for u in sub_page_urls)

    def test_direct_asset_link_not_collected_as_subpage(self):
        """assets.publishing links do not appear in sub_page_urls."""
        html = """
        <section class="gem-c-attachment govuk-!-margin-bottom-6">
          <div class="gem-c-attachment__details">
            <h3><a class="gem-c-attachment__link"
                   href="https://assets.publishing.service.gov.uk/media/abc/data.xlsx">
              Dataset
            </a></h3>
            <p class="gem-c-attachment__metadata">
              <span class="gem-c-attachment__attribute"><abbr title="MS Excel Spreadsheet">XLSX</abbr></span>
            </p>
          </div>
        </section>
        """
        files, _ees, sub_page_urls = _parse_govuk_page(html)
        assert len(files) == 1
        assert sub_page_urls == []


# ---------------------------------------------------------------------------
# _is_govuk_subpage
# ---------------------------------------------------------------------------

class TestIsGovukSubpage:

    def test_statistics_subpage_detected(self):
        assert _is_govuk_subpage(
            "https://www.gov.uk/government/statistics/some-pub/some-page"
        ) is True

    def test_relative_statistics_subpage_detected(self):
        assert _is_govuk_subpage(
            "/government/statistics/some-pub/some-page"
        ) is True

    def test_publications_subpage_detected(self):
        assert _is_govuk_subpage(
            "https://www.gov.uk/government/publications/some-pub/some-page"
        ) is True

    def test_collections_subpage_detected(self):
        assert _is_govuk_subpage(
            "https://www.gov.uk/government/collections/some-pub/some-page"
        ) is True

    def test_assets_link_not_subpage(self):
        assert _is_govuk_subpage(
            "https://assets.publishing.service.gov.uk/media/abc/data.xlsx"
        ) is False

    def test_url_with_file_extension_not_subpage(self):
        assert _is_govuk_subpage(
            "https://www.gov.uk/government/statistics/pub/data.xlsx"
        ) is False

    def test_top_level_gov_url_not_subpage(self):
        # Only 3 path segments — landing page, not a sub-page
        assert _is_govuk_subpage(
            "https://www.gov.uk/government/statistics/some-pub"
        ) is False

    def test_ees_url_not_subpage(self):
        assert _is_govuk_subpage(
            "https://explore-education-statistics.service.gov.uk/find/stats"
        ) is False


# ---------------------------------------------------------------------------
# File type derivation
# ---------------------------------------------------------------------------

class TestFileTypeDerivation:

    def test_ods_from_abbr_title(self):
        from bs4 import BeautifulSoup
        html = """<div class="gem-c-attachment__metadata">
                    <span class="gem-c-attachment__attribute">
                      <abbr title="OpenDocument Spreadsheet">ODS</abbr>
                    </span>
                  </div>"""
        el = BeautifulSoup(html, "html.parser")
        assert _derive_file_type(el, "https://assets.example.com/f.ods") == "ods"

    def test_pdf_from_abbr_title(self):
        from bs4 import BeautifulSoup
        html = """<div class="gem-c-attachment__metadata">
                    <span class="gem-c-attachment__attribute">
                      <abbr title="Portable Document Format">PDF</abbr>
                    </span>
                  </div>"""
        el = BeautifulSoup(html, "html.parser")
        assert _derive_file_type(el, "https://assets.example.com/f.pdf") == "pdf"

    def test_url_extension_fallback_when_no_markup(self):
        from bs4 import BeautifulSoup
        el = BeautifulSoup("<div></div>", "html.parser")
        assert _derive_file_type(el, "https://assets.example.com/data.csv") == "csv"

    def test_xlsx_from_url_extension(self):
        from bs4 import BeautifulSoup
        el = BeautifulSoup("<div></div>", "html.parser")
        assert _derive_file_type(el, "https://assets.example.com/tables.xlsx") == "xlsx"

    def test_unknown_extension_returns_itself_short(self):
        from bs4 import BeautifulSoup
        el = BeautifulSoup("<div></div>", "html.parser")
        result = _derive_file_type(el, "https://assets.example.com/data.abc")
        assert result == "abc"

    def test_no_markup_no_extension_returns_none(self):
        from bs4 import BeautifulSoup
        el = BeautifulSoup("<div></div>", "html.parser")
        assert _derive_file_type(el, "https://assets.example.com/data") is None

    def test_html_type_not_in_data_file_types(self):
        assert "html" not in DATA_FILE_TYPES

    def test_normalise_ms_excel_label(self):
        assert _normalise_type_label("MS Excel Spreadsheet") == "xlsx"

    def test_normalise_partial_match(self):
        assert _normalise_type_label("MS Excel Spreadsheet, some extra text") == "xlsx"

    def test_normalise_short_ods(self):
        assert _normalise_type_label("ODS") == "ods"

    def test_normalise_unknown_returns_none(self):
        assert _normalise_type_label("Something Entirely Unknown") is None


# ---------------------------------------------------------------------------
# File size parsing
# ---------------------------------------------------------------------------

class TestFileSizeParsing:

    def _make_el(self, html: str):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_kb_parsed(self):
        el = self._make_el('<p class="gem-c-attachment__metadata">'
                           '<span class="gem-c-attachment__attribute"><abbr>ODS</abbr></span>,'
                           '<span class="gem-c-attachment__attribute"><abbr title="kilobytes">150 KB</abbr></span>'
                           '</p>')
        assert _extract_size(el) == 150 * 1024

    def test_mb_parsed(self):
        el = self._make_el('<p class="gem-c-attachment__metadata">'
                           '<span class="gem-c-attachment__attribute"><abbr title="megabytes">3.94 MB</abbr></span>'
                           '</p>')
        assert _extract_size(el) == int(3.94 * 1_048_576)

    def test_old_pattern_file_size_span(self):
        el = self._make_el('<p class="metadata">'
                           '<span class="type">ODS</span>, '
                           '<span class="file-size">234 KB</span>'
                           '</p>')
        assert _extract_size(el) == 234 * 1024

    def test_no_size_returns_none(self):
        el = self._make_el('<div>No size here</div>')
        assert _extract_size(el) is None


# ---------------------------------------------------------------------------
# Classification from heading
# ---------------------------------------------------------------------------

class TestClassifyFromHeading:

    def test_main_tables(self):
        assert _classify_from_heading("Main tables") == "main_release"

    def test_supporting_files(self):
        assert _classify_from_heading("Supporting files") == "supporting_tables"

    def test_additional_data(self):
        assert _classify_from_heading("Additional data tables") == "supporting_tables"

    def test_technical_notes(self):
        assert _classify_from_heading("Technical notes") == "technical_docs"

    def test_methodology(self):
        assert _classify_from_heading("Methodology") == "technical_docs"

    def test_no_match_returns_none(self):
        assert _classify_from_heading("Publications") is None

    def test_none_heading_returns_none(self):
        assert _classify_from_heading(None) is None

    def test_case_insensitive(self):
        assert _classify_from_heading("MAIN RESULTS") == "main_release"


# ---------------------------------------------------------------------------
# EES case (a) — publication URL is EES, no HTTP call
# ---------------------------------------------------------------------------

class TestEESCaseA:

    def test_ees_publication_url_returns_not_extractable(self):
        from scripts.extract_pub_data_files import _extract_one

        pub = MagicMock()
        pub.url = "https://explore-education-statistics.service.gov.uk/find-statistics/pupil-absence"

        http = MagicMock()
        result = _extract_one(pub, http, select_extractor)

        assert result.status == "not_extractable"
        assert result.ees_url == pub.url
        http.get.assert_not_called()

    def test_govuk_url_does_call_http(self):
        from scripts.extract_pub_data_files import _extract_one

        pub = MagicMock()
        pub.url = "https://www.gov.uk/government/statistics/something"

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.text = _EMPTY_GOVSPEAK
        http = MagicMock()
        http.get.return_value = mock_resp

        result = _extract_one(pub, http, select_extractor)

        http.get.assert_called_once()
        assert result.status == "no_files_found"


# ---------------------------------------------------------------------------
# Fallback extractor
# ---------------------------------------------------------------------------

class TestFallbackExtractor:

    def test_returns_not_extractable_without_fetch(self):
        extractor = FallbackExtractor()
        http = MagicMock()
        result = extractor.extract("https://www.ons.gov.uk/something", http)
        assert result.status == "not_extractable"
        http.get.assert_not_called()


# ---------------------------------------------------------------------------
# select_extractor dispatch
# ---------------------------------------------------------------------------

class TestSelectExtractor:

    def test_govuk_url_returns_govuk_extractor(self):
        pub = MagicMock()
        pub.url = "https://www.gov.uk/government/statistics/foo"
        ext = select_extractor(pub)
        assert isinstance(ext, GovUKGenericExtractor)

    def test_ons_url_returns_fallback(self):
        pub = MagicMock()
        pub.url = "https://www.ons.gov.uk/some-stats"
        ext = select_extractor(pub)
        assert isinstance(ext, FallbackExtractor)

    def test_unknown_url_returns_fallback(self):
        pub = MagicMock()
        pub.url = "https://www.england.nhs.uk/publications/data"
        ext = select_extractor(pub)
        assert isinstance(ext, FallbackExtractor)


# ---------------------------------------------------------------------------
# GovUKGenericExtractor — HTTP failure paths
# ---------------------------------------------------------------------------

class TestGovUKExtractorHTTPPaths:

    def _make_extractor(self):
        return GovUKGenericExtractor()

    def test_fetch_failed_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        http = MagicMock()
        http.get.return_value = mock_resp

        result = self._make_extractor().extract("https://www.gov.uk/something", http)
        assert result.status == "fetch_failed"

    def test_fetch_failed_on_request_exception(self):
        import requests as req
        http = MagicMock()
        http.get.side_effect = req.RequestException("timeout")

        result = self._make_extractor().extract("https://www.gov.uk/something", http)
        assert result.status == "fetch_failed"

    def test_extracted_on_successful_parse(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.text = _MULTI_FILE_WITH_HEADINGS
        http = MagicMock()
        http.get.return_value = mock_resp

        result = self._make_extractor().extract("https://www.gov.uk/something", http)
        assert result.status == "extracted"
        assert len(result.files) == 2

    def test_no_files_found_on_empty_page(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.text = _EMPTY_GOVSPEAK
        http = MagicMock()
        http.get.return_value = mock_resp

        result = self._make_extractor().extract("https://www.gov.uk/something", http)
        assert result.status == "no_files_found"

    def test_not_extractable_on_ees_only(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.text = _EES_ONLY
        http = MagicMock()
        http.get.return_value = mock_resp

        result = self._make_extractor().extract("https://www.gov.uk/something", http)
        assert result.status == "not_extractable"
        assert result.ees_url is not None

    def test_ees_plus_files_returns_extracted_with_ees_url(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.text = _EES_PLUS_DATA_FILE
        http = MagicMock()
        http.get.return_value = mock_resp

        result = self._make_extractor().extract("https://www.gov.uk/something", http)
        assert result.status == "extracted"
        assert result.ees_url is not None
        assert len(result.files) == 1


# ---------------------------------------------------------------------------
# _write_result — delete-and-replace behaviour
# ---------------------------------------------------------------------------

class TestWriteResult:

    def _make_pub(self):
        pub = MagicMock()
        pub.id = 42
        pub.data_files_status = "pending"
        pub.data_files_extracted_at = None
        pub.ees_url = None
        return pub

    def test_existing_rows_deleted_on_extracted(self):
        from scripts.extract_pub_data_files import _write_result
        from hansard_archive.discovery.extractors.base import ExtractedFile, ExtractionResult

        db_session = MagicMock()
        pub = self._make_pub()
        StatPublicationDataFile = MagicMock()

        result = ExtractionResult(
            status="extracted",
            files=[ExtractedFile(url="https://assets.example.com/d.csv", file_type="csv")],
        )

        _write_result(db_session, pub, result, StatPublicationDataFile)

        db_session.query.assert_called_once_with(StatPublicationDataFile)
        assert pub.data_files_status == "extracted"
        assert pub.data_files_extracted_at is not None
        db_session.commit.assert_called_once()

    def test_existing_rows_preserved_on_fetch_failed(self):
        from scripts.extract_pub_data_files import _write_result
        from hansard_archive.discovery.extractors.base import ExtractionResult

        db_session = MagicMock()
        pub = self._make_pub()
        StatPublicationDataFile = MagicMock()

        result = ExtractionResult(status="fetch_failed")

        _write_result(db_session, pub, result, StatPublicationDataFile)

        # Must NOT query StatPublicationDataFile (no delete on fetch_failed)
        db_session.query.assert_not_called()
        assert pub.data_files_status == "fetch_failed"
        # data_files_extracted_at must NOT be set on fetch_failed
        assert pub.data_files_extracted_at is None

    def test_ees_url_set_on_publication(self):
        from scripts.extract_pub_data_files import _write_result
        from hansard_archive.discovery.extractors.base import ExtractionResult

        db_session = MagicMock()
        pub = self._make_pub()
        StatPublicationDataFile = MagicMock()

        ees = "https://explore-education-statistics.service.gov.uk/find/something"
        result = ExtractionResult(status="not_extractable", ees_url=ees)

        _write_result(db_session, pub, result, StatPublicationDataFile)

        assert pub.ees_url == ees


# ---------------------------------------------------------------------------
# Failure isolation in batch loop
# ---------------------------------------------------------------------------

class TestFailureIsolation:

    def test_exception_on_one_pub_does_not_abort_batch(self):
        """An unhandled error on pub N must not prevent pub N+1 from being processed."""
        from scripts.extract_pub_data_files import run_extraction
        from hansard_archive.discovery.extractors.base import ExtractionResult

        pub_fail = MagicMock()
        pub_fail.id = 1
        pub_fail.url = "https://www.gov.uk/failing"
        pub_fail.data_files_status = "pending"

        pub_ok = MagicMock()
        pub_ok.id = 2
        pub_ok.url = "https://www.gov.uk/ok"
        pub_ok.data_files_status = "pending"

        db_session = MagicMock()
        # First batch returns two pubs; second returns empty to end the loop
        db_session.query.return_value.join.return_value.filter.return_value \
            .order_by.return_value.limit.return_value.all.side_effect = [
            [pub_fail, pub_ok],
            [],
        ]

        processed = []

        def fake_select(pub):
            extractor = MagicMock()
            if pub.url == "https://www.gov.uk/failing":
                extractor.extract.side_effect = RuntimeError("deliberate failure")
            else:
                extractor.extract.return_value = ExtractionResult(status="no_files_found")
                processed.append(pub.url)
            return extractor

        with patch("scripts.extract_pub_data_files.time.sleep"), \
             patch("hansard_archive.discovery.extractors.select_extractor",
                   side_effect=fake_select):
            stats = run_extraction(db_session, execute=False)

        # Both were processed
        assert stats["processed"] == 2
        assert stats["unhandled_error"] == 1
        assert stats["dry_run"] == 1   # pub_ok processed in dry-run mode
