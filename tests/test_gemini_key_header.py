"""The Gemini key goes in a header, never the URL (URLs end up in exception
text and logs)."""
from unittest.mock import MagicMock, patch

KEY = 'TEST-KEY-123'


def _ok(text='["a"]'):
    r = MagicMock(status_code=200)
    r.json.return_value = {'models': [], 'candidates': [{'content': {'parts': [{'text': text}]}}]}
    return r


def _assert_calls_clean(mock):
    assert mock.call_args_list
    for call in mock.call_args_list:
        url = call.args[0] if call.args else call.kwargs['url']
        assert KEY not in url
        assert call.kwargs['headers'] == {'x-goog-api-key': KEY}


def test_debate_scanner_model_lookup():
    import debate_scanner
    debate_scanner._MODEL_CACHE.pop(KEY, None)
    with patch('debate_scanner.requests.get', return_value=_ok()) as get:
        debate_scanner.get_working_model(KEY)
    _assert_calls_clean(get)


def test_tracker_generate():
    import tracker
    tracker._GEMINI_MODEL_CACHE.pop(KEY, None)
    with patch('tracker.requests.get', return_value=_ok()) as get, \
         patch('tracker.requests.post', return_value=_ok()) as post:
        tracker._gemini_generate(KEY, 'prompt')
    _assert_calls_clean(get)
    _assert_calls_clean(post)


def test_tagger_model_lookup():
    from hansard_archive import tagger
    tagger._MODEL_CACHE.pop(KEY, None)
    with patch('hansard_archive.tagger.requests.get', return_value=_ok()) as get:
        tagger._detect_model(KEY)
    _assert_calls_clean(get)


def test_tagger_generate():
    from hansard_archive import tagger
    ok = _ok('{"policy_areas": [], "topics": []}')
    with patch('hansard_archive.tagger.requests.post', return_value=ok) as post:
        tagger._gemini_json(KEY, 'gemini-2.5-flash-lite', 'prompt', tagger._RESPONSE_SCHEMA)
    _assert_calls_clean(post)
