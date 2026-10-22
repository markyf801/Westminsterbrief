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
