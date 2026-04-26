"""
Tests for tracker AI categorisation cache.

Run: python -m pytest tests/test_tracker_cache.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import tracker


def _clear_cache():
    tracker._AI_CATEGORY_CACHE.clear()


QUESTIONS_DFE = [{'uin': '111', 'text': 'What is the government doing about student loans?'}]
QUESTIONS_HO = [{'uin': '444', 'text': 'What is the government doing about asylum backlogs?'}]


class TestAICacheHelpers:
    def setup_method(self):
        _clear_cache()

    def test_cache_miss_returns_none(self):
        assert tracker._get_cached_categories('60', '2026-04-25') is None

    def test_set_then_get_returns_stored_value(self):
        tracker._set_cached_categories('60', '2026-04-25', {'111': 'Higher Education Finance'})
        result = tracker._get_cached_categories('60', '2026-04-25')
        assert result == {'111': 'Higher Education Finance'}

    def test_expired_entry_returns_none(self):
        key = tracker._ai_cache_key('60', '2026-01-01')
        tracker._AI_CATEGORY_CACHE[key] = ({'x': 'y'}, datetime.now() - timedelta(seconds=1))
        assert tracker._get_cached_categories('60', '2026-01-01') is None

    def test_expired_entry_is_removed_from_cache(self):
        key = tracker._ai_cache_key('60', '2026-01-02')
        tracker._AI_CATEGORY_CACHE[key] = ({'x': 'y'}, datetime.now() - timedelta(seconds=1))
        tracker._get_cached_categories('60', '2026-01-02')
        assert key not in tracker._AI_CATEGORY_CACHE

    def test_ttl_expires_at_midnight(self):
        tracker._set_cached_categories('60', '2026-04-25', {'a': 'b'})
        key = tracker._ai_cache_key('60', '2026-04-25')
        _, expires_at = tracker._AI_CATEGORY_CACHE[key]
        now = datetime.now()
        expected_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        # Allow 1-second tolerance for test execution time
        assert abs((expires_at - expected_midnight).total_seconds()) < 1


class TestCategoriseQuestions:
    def setup_method(self):
        _clear_cache()

    def test_two_requests_for_same_dept_date_make_one_ai_call(self):
        with patch('tracker._gemini_generate') as mock_gemini:
            mock_gemini.return_value = '{"111": "Higher Education Finance"}'
            result1 = tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-25', 'test-key')
            result2 = tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-25', 'test-key')
        assert mock_gemini.call_count == 1
        assert result1 == result2 == {'111': 'Higher Education Finance'}

    def test_cache_invalidates_after_midnight(self):
        # Pre-seed an expired entry
        key = tracker._ai_cache_key('60', '2026-04-24')
        tracker._AI_CATEGORY_CACHE[key] = ({'111': 'Old Theme'}, datetime.now() - timedelta(seconds=1))

        with patch('tracker._gemini_generate') as mock_gemini:
            mock_gemini.return_value = '{"111": "New Theme"}'
            result = tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-24', 'test-key')

        assert mock_gemini.call_count == 1
        assert result == {'111': 'New Theme'}

    def test_different_departments_get_separate_cache_entries(self):
        with patch('tracker._gemini_generate') as mock_gemini:
            mock_gemini.side_effect = ['{"111": "Education"}', '{"444": "Immigration"}']
            tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-25', 'test-key')
            tracker._categorise_questions(QUESTIONS_HO, '1', '2026-04-25', 'test-key')

        assert tracker._get_cached_categories('60', '2026-04-25') == {'111': 'Education'}
        assert tracker._get_cached_categories('1', '2026-04-25') == {'444': 'Immigration'}
        assert mock_gemini.call_count == 2

    def test_no_key_returns_empty_dict_without_api_call(self):
        with patch('tracker._gemini_generate') as mock_gemini:
            result = tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-25', gemini_key=None)
        assert result == {}
        assert mock_gemini.call_count == 0

    def test_result_is_stored_in_cache_after_api_call(self):
        with patch('tracker._gemini_generate') as mock_gemini:
            mock_gemini.return_value = '{"111": "SEND"}'
            tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-25', 'test-key')

        assert tracker._get_cached_categories('60', '2026-04-25') == {'111': 'SEND'}

    def test_ai_error_returns_empty_dict_and_does_not_cache(self):
        with patch('tracker._gemini_generate', side_effect=Exception("API down")):
            result = tracker._categorise_questions(QUESTIONS_DFE, '60', '2026-04-25', 'test-key')

        assert result == {}
        assert tracker._get_cached_categories('60', '2026-04-25') is None
