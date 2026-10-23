import debate_scanner


def test_summary_off_by_default(monkeypatch):
    monkeypatch.delenv('RESEARCH_AI_SUMMARY', raising=False)
    assert debate_scanner.research_ai_summary_enabled() is False


def test_summary_on_when_set(monkeypatch):
    for v in ('on', '1', 'true', 'YES'):
        monkeypatch.setenv('RESEARCH_AI_SUMMARY', v)
        assert debate_scanner.research_ai_summary_enabled() is True


def test_summary_off_for_other_values(monkeypatch):
    for v in ('off', '0', '', 'no'):
        monkeypatch.setenv('RESEARCH_AI_SUMMARY', v)
        assert debate_scanner.research_ai_summary_enabled() is False


def _render_results(**extra):
    from flask_app import app
    from flask import render_template
    rows = [{'speaker_name': 'A Member', 'hdate': '2026-10-01', 'body': 'x'}]
    with app.test_request_context('/debates/topic'):
        return render_template('debate_scanner.html', mode='topic', topic='ukraine',
                               topic_rows=rows, topic_briefing=None, **extra)


def test_no_error_box_when_summary_not_attempted():
    html = _render_results(ai_briefing_attempted=False)
    assert 'AI briefing unavailable' not in html


def test_error_box_when_attempt_failed():
    html = _render_results(ai_briefing_attempted=True)
    assert 'AI briefing unavailable' in html
