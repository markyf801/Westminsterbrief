"""
Written Questions search: 50 results per page, slim list rows, full exports.

Re-uses the seeded app from test_routes and adds 70 "pagination probe" PQs.

Run: python -m pytest tests/test_wq_pagination.py -v
"""

import io
import json
import re
from datetime import date, timedelta

import pytest

from test_routes import app  # noqa: F401  (module-scoped seeded test app)

_N = 70
_LONG_ANSWER = '<p>' + 'The department keeps funding under review. ' * 40 + 'FINAL-SENTENCE.</p>'
_SHORT_ANSWER = '<p>A short <em>formatted</em> answer.</p>'


@pytest.fixture(scope='module')
def wq_client(app):  # noqa: F811
    from extensions import db
    from hansard_archive.models import HaPQ
    with app.app_context():
        db.session.add_all([
            HaPQ(uin=f'PGP{i:03d}', heading=f'Probe heading {i % 4}',
                 question_text=f'Pagination probe question number {i}?',
                 answer_text=_LONG_ANSWER if i == 0 else _SHORT_ANSWER,
                 tabled_date=date(2026, 3, 1) - timedelta(days=i),
                 answer_date=date(2026, 3, 5) - timedelta(days=i),
                 asking_member='Probe MP', answering_body='Department for Education',
                 answering_body_id=60, chamber='Commons', is_answered=True)
            for i in range(_N)])
        db.session.commit()
    return app.test_client()


def _page_uins(html):
    """UINs on the page (each card is rendered in both the flat and grouped views)."""
    return set(re.findall(r"""data-wq='\{"uin": "([A-Z0-9]+)"\}'""", html))


def _href(url):
    return f'href="{url.replace("&", "&amp;")}"'


def test_page_one_shows_fifty(wq_client):
    html = wq_client.get('/questions?q=pagination+probe').get_data(as_text=True)
    uins = _page_uins(html)
    assert len(uins) == 50
    assert 'PGP000' in uins and 'PGP049' in uins   # newest first
    assert 'Showing <strong>1&ndash;50</strong> of <strong>70</strong>' in html
    assert _href('/written-questions?q=pagination+probe&page=2') + ' rel="next"' in html
    assert 'rel="prev"' not in html


def test_page_two_shows_the_rest(wq_client):
    html = wq_client.get('/questions?q=pagination+probe&page=2').get_data(as_text=True)
    uins = _page_uins(html)
    assert uins == {f'PGP{i:03d}' for i in range(50, _N)}
    assert 'Showing <strong>51&ndash;70</strong> of <strong>70</strong>' in html
    assert 'href="/written-questions?q=pagination+probe" rel="prev"' in html
    assert 'rel="next"' not in html
    assert '<strong aria-current="page" style="padding:4px 8px;">2</strong>' in html


def test_pager_links_keep_filters(wq_client):
    qs = ('q=pagination+probe&department=60&house_filter=Commons'
          '&status_filter=answered&start_date=2025-01-01&end_date=2026-12-31')
    html = wq_client.get(f'/questions?{qs}').get_data(as_text=True)
    assert len(_page_uins(html)) == 50
    assert f'href="/written-questions?{qs}&page=2"'.replace('&', '&amp;') in html \
        or f'href="/written-questions?{qs}&page=2"' in html


def test_huge_or_bad_page_numbers_never_500(wq_client):
    r = wq_client.get('/questions?q=pagination+probe&department=60&page=99999999999999999999')
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/written-questions?q=pagination+probe&department=60&page=2')
    assert wq_client.get('/questions?q=pagination+probe&page=3').status_code == 302
    for bad in ('0', '-4', 'abc', '9' * 5000):
        r = wq_client.get(f'/questions?q=pagination+probe&page={bad}')
        assert r.status_code == 200
        assert 'Showing <strong>1&ndash;50</strong>' in r.get_data(as_text=True)


def test_no_matches_has_no_pager(wq_client):
    r = wq_client.get('/questions?q=zzznotfound&page=5')
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/written-questions?q=zzznotfound')
    html = wq_client.get('/questions?q=zzznotfound').get_data(as_text=True)
    assert 'No questions found' in html
    assert 'class="wq-pager"' not in html


def test_long_answer_is_truncated_short_answer_in_full(wq_client):
    html = wq_client.get('/questions?q=pagination+probe').get_data(as_text=True)
    assert 'FINAL-SENTENCE' not in html
    assert '<a href="/archive/pq/pgp000" target="_blank">Read full answer &rarr;</a>' in html
    assert 'The department keeps funding under review.' in html
    assert _SHORT_ANSWER in html


def test_csv_export_returns_all_matching_rows(wq_client):
    r = wq_client.post('/questions', data={'q': 'pagination probe', 'department': '60',
                                           'page': '2', 'action': 'csv'})
    assert r.status_code == 200
    assert 'attachment' in r.headers['Content-Disposition']
    body = r.get_data(as_text=True)
    assert len(set(re.findall(r'PGP\d{3}', body))) == _N
    assert 'FINAL-SENTENCE' in body   # full answer, not the list excerpt


def test_word_export_returns_all_matching_rows(wq_client):
    from docx import Document
    r = wq_client.post('/questions', data={'q': 'pagination probe', 'action': 'word'})
    assert r.status_code == 200
    assert r.headers['Content-Disposition'].endswith('.docx')
    text = '\n'.join(p.text for p in Document(io.BytesIO(r.get_data())).paragraphs)
    assert len(set(re.findall(r'UIN: (PGP\d{3})', text))) == _N
    assert 'FINAL-SENTENCE' in text


def test_download_selected_reads_full_record_by_uin(wq_client):
    items = json.dumps([{'uin': 'PGP000'}, {'uin': 'PGP001'}])
    r = wq_client.post('/questions/download_selected', data={'items_json': items, 'format': 'csv'})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'PGP000' in body and 'PGP001' in body
    assert 'FINAL-SENTENCE' in body
    assert 'Pagination probe question number 1?' in body


def test_page_url_for_empty_search_keeps_a_parameter():
    from werkzeug.datastructures import MultiDict
    from hansard import _wq_page_url
    assert _wq_page_url(MultiDict(), 2) == '/written-questions?q=&page=2'
    assert _wq_page_url(MultiDict({'q': 'x', 'house_filter': 'All'}), 1) == '/written-questions?q=x'
