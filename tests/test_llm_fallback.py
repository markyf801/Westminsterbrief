"""
Tests verifying the Gemini → Claude fallback chain in debate_scanner.py.

Both _prep_one_pager() and generate_stakeholder_briefing() should call
_claude_fallback() when the Gemini API returns a non-200 response.

Run: python -m pytest tests/test_llm_fallback.py -v
"""

import json
import sys
from unittest.mock import MagicMock, patch

import debate_scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_ONE_PAGER_JSON = json.dumps({
    "why_now": "Relevant context",
    "sector_context": "Background",
    "major_criticisms": ["Issue A"],
    "opposition_position": "They oppose X",
    "sources": ["Source 1"],
})

VALID_STAKEHOLDER_JSON = json.dumps({
    "summary": "Stakeholder summary",
    "stated_positions": [{"org": "NUS", "position": "Opposes fees", "source_type": "Hansard", "source_date": "2026-01-01"}],
    "key_asks": ["Reduce fees"],
    "coverage_note": "Evidence from 2026.",
})

SAMPLE_HANSARD_ROWS = [
    {'speaker_name': 'Test MP', 'hdate': '2026-01-01', 'body': 'Test speech about education policy'}
]

_FAKE_GEMINI_KEY = 'fake-gemini-key'
_FAKE_CLAUDE_KEY = 'fake-claude-key'


def _make_mock_response(status_code, json_body=None):
    mock = MagicMock()
    mock.status_code = status_code
    if json_body is not None:
        mock.json.return_value = json_body
    return mock


# ---------------------------------------------------------------------------
# _claude_fallback itself
# ---------------------------------------------------------------------------

class TestClaudeFallback:
    def test_returns_none_when_no_api_key(self):
        with patch('debate_scanner.CLAUDE_API_KEY', None):
            result = debate_scanner._claude_fallback("test prompt")
        assert result is None

    def test_calls_anthropic_sdk_and_returns_text(self):
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="Claude response text")]
        mock_anthropic_module = MagicMock()
        mock_anthropic_module.Anthropic.return_value.messages.create.return_value = mock_msg
        with patch('debate_scanner.CLAUDE_API_KEY', _FAKE_CLAUDE_KEY), \
             patch.dict(sys.modules, {'anthropic': mock_anthropic_module}):
            result = debate_scanner._claude_fallback("test prompt", max_tokens=500)
        assert result == "Claude response text"
        mock_anthropic_module.Anthropic.return_value.messages.create.assert_called_once()
        call_kwargs = mock_anthropic_module.Anthropic.return_value.messages.create.call_args[1]
        assert call_kwargs['model'] == 'claude-haiku-4-5-20251001'
        assert call_kwargs['max_tokens'] == 500

    def test_returns_none_on_anthropic_exception(self):
        mock_anthropic_module = MagicMock()
        mock_anthropic_module.Anthropic.side_effect = Exception("API error")
        with patch('debate_scanner.CLAUDE_API_KEY', _FAKE_CLAUDE_KEY), \
             patch.dict(sys.modules, {'anthropic': mock_anthropic_module}):
            result = debate_scanner._claude_fallback("test prompt")
        assert result is None


# ---------------------------------------------------------------------------
# _prep_one_pager — fallback triggered on Gemini failure
# ---------------------------------------------------------------------------

class TestPrepOnePagerFallback:
    def test_claude_called_when_gemini_returns_non_200(self):
        with patch('debate_scanner.GEMINI_API_KEY', _FAKE_GEMINI_KEY), \
             patch('debate_scanner.CLAUDE_API_KEY', _FAKE_CLAUDE_KEY), \
             patch('debate_scanner.get_working_model', return_value='models/fake'), \
             patch('debate_scanner.requests.post', return_value=_make_mock_response(500)), \
             patch('debate_scanner._claude_fallback', return_value=VALID_ONE_PAGER_JSON) as mock_claude:
            result = debate_scanner._prep_one_pager("What is the government doing?", "student loans")
        mock_claude.assert_called_once()
        assert result is not None

    def test_claude_called_when_gemini_raises_exception(self):
        with patch('debate_scanner.GEMINI_API_KEY', _FAKE_GEMINI_KEY), \
             patch('debate_scanner.CLAUDE_API_KEY', _FAKE_CLAUDE_KEY), \
             patch('debate_scanner.get_working_model', return_value='models/fake'), \
             patch('debate_scanner.requests.post', side_effect=Exception("connection error")), \
             patch('debate_scanner._claude_fallback', return_value=VALID_ONE_PAGER_JSON) as mock_claude:
            result = debate_scanner._prep_one_pager("What is the government doing?", "student loans")
        mock_claude.assert_called_once()
        assert result is not None

    def test_claude_not_called_when_gemini_succeeds(self):
        gemini_response_body = {
            'candidates': [{'content': {'parts': [{'text': VALID_ONE_PAGER_JSON}]}}]
        }
        with patch('debate_scanner.GEMINI_API_KEY', _FAKE_GEMINI_KEY), \
             patch('debate_scanner.CLAUDE_API_KEY', _FAKE_CLAUDE_KEY), \
             patch('debate_scanner.get_working_model', return_value='models/fake'), \
             patch('debate_scanner.requests.post', return_value=_make_mock_response(200, gemini_response_body)), \
             patch('debate_scanner._claude_fallback') as mock_claude:
            result = debate_scanner._prep_one_pager("What is the government doing?", "student loans")
        mock_claude.assert_not_called()
        assert result is not None

    def test_returns_none_when_both_keys_absent(self):
        with patch('debate_scanner.GEMINI_API_KEY', None), \
             patch('debate_scanner.CLAUDE_API_KEY', None):
            result = debate_scanner._prep_one_pager("Question?", "topic")
        assert result is None


# ---------------------------------------------------------------------------
# generate_stakeholder_briefing — fallback triggered on Gemini non-200
# ---------------------------------------------------------------------------

class TestStakeholderBriefingFallback:
    def test_claude_called_when_gemini_returns_non_200(self):
        with patch('debate_scanner.GEMINI_API_KEY', _FAKE_GEMINI_KEY), \
             patch('debate_scanner.get_working_model', return_value='models/fake'), \
             patch('debate_scanner.requests.post', return_value=_make_mock_response(500)), \
             patch('debate_scanner._claude_fallback', return_value=VALID_STAKEHOLDER_JSON) as mock_claude:
            result = debate_scanner.generate_stakeholder_briefing(
                topic="student loans",
                hansard_rows=SAMPLE_HANSARD_ROWS,
            )
        mock_claude.assert_called_once()
        assert result is not None

    def test_claude_not_called_when_gemini_succeeds(self):
        gemini_response_body = {
            'candidates': [{'content': {'parts': [{'text': VALID_STAKEHOLDER_JSON}]}}]
        }
        with patch('debate_scanner.GEMINI_API_KEY', _FAKE_GEMINI_KEY), \
             patch('debate_scanner.get_working_model', return_value='models/fake'), \
             patch('debate_scanner.requests.post', return_value=_make_mock_response(200, gemini_response_body)), \
             patch('debate_scanner._claude_fallback') as mock_claude:
            result = debate_scanner.generate_stakeholder_briefing(
                topic="student loans",
                hansard_rows=SAMPLE_HANSARD_ROWS,
            )
        mock_claude.assert_not_called()
        assert result is not None

    def test_returns_none_when_gemini_key_absent(self):
        with patch('debate_scanner.GEMINI_API_KEY', None):
            result = debate_scanner.generate_stakeholder_briefing(
                topic="student loans",
                hansard_rows=SAMPLE_HANSARD_ROWS,
            )
        assert result is None
