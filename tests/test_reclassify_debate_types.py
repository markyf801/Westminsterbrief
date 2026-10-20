"""scripts/reclassify_debate_types.py — dry run writes nothing; --apply writes; fetch errors skip the row."""

import argparse
import importlib.util
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "reclassify_debate_types.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("reclassify_debate_types", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded():
    # conftest.py forces sqlite:///:memory: before flask_app is imported.
    import flask_app as _fa
    from extensions import db
    from hansard_archive.models import HansardContribution, HansardSession

    with _fa.app.app_context():
        db.create_all()
        HansardContribution.query.delete()
        HansardSession.query.delete()
        rows = [
            # Lords question stored as 'other'
            HansardSession(ext_id="L-Q", title="Housing: National Targets", date=date(2026, 10, 14),
                           house="Lords", debate_type="other", location="Lords Chamber", hrs_tag="NewDebate",
                           is_container=False, slug="housing-national-targets-l-q"),
            # Lords take-note debate stored as committee_stage (Grand Committee venue)
            HansardSession(ext_id="L-TN", title="UK and Poland", date=date(2026, 10, 14),
                           house="Lords", debate_type="committee_stage", location="Grand Committee",
                           hrs_tag="NewDebate", is_container=False, slug="uk-poland-l-tn"),
            # Lords row whose API fetch fails — must be left alone
            HansardSession(ext_id="L-ERR", title="Drumcree", date=date(2026, 10, 14),
                           house="Lords", debate_type="other", location="Lords Chamber", hrs_tag="NewDebate",
                           is_container=False, slug="drumcree-l-err"),
            # Commons urgent question stored as 'other'
            HansardSession(ext_id="C-UQ", title="Sustainable Farming Incentive", date=date(2026, 10, 15),
                           house="Commons", debate_type="other", location="Commons Chamber",
                           hrs_tag="hs_2cUrgentQuestion", is_container=False, slug="sfi-c-uq"),
            # Commons oral question already right
            HansardSession(ext_id="C-OQ", title="Whistleblowing", date=date(2026, 10, 14),
                           house="Commons", debate_type="oral_questions", location="Commons Chamber",
                           hrs_tag="hs_8Question", is_container=False, slug="whistleblowing-c-oq"),
        ]
        db.session.add_all(rows)
        db.session.commit()
        yield _fa.app
        HansardSession.query.delete()
        db.session.commit()


def _args(**kw):
    base = dict(apply=False, house=None, since=None, until=None, limit=None, samples=2)
    base.update(kw)
    return argparse.Namespace(**base)


def _types(app):
    from hansard_archive.models import HansardSession
    with app.app_context():
        return {s.ext_id: s.debate_type for s in HansardSession.query.all()}


def _fake_labels(monkeypatch, script):
    labels = {"L-Q": (True, "Question"), "L-TN": (True, "Motion to Take Note"), "L-ERR": (False, "")}
    monkeypatch.setattr(script, "_fetch_label", lambda ext_id: labels[ext_id])
    monkeypatch.setattr(script, "_DELAY", 0)


def test_dry_run_writes_nothing(seeded, script, monkeypatch, capsys):
    _fake_labels(monkeypatch, script)
    before = _types(seeded)
    script.run(_args())
    assert _types(seeded) == before
    out = capsys.readouterr().out
    assert "Lords    other -> oral_questions" in out
    assert "Lords    committee_stage -> debate" in out
    assert "Commons  other -> urgent_question" in out
    assert "would_change=3" in out and "fetch_errors=1" in out and "written=0" in out


def test_apply_writes_changes_and_skips_fetch_errors(seeded, script, monkeypatch):
    _fake_labels(monkeypatch, script)
    script.run(_args(apply=True))
    assert _types(seeded) == {
        "L-Q": "oral_questions",
        "L-TN": "debate",
        "L-ERR": "other",          # unchanged: label could not be fetched
        "C-UQ": "urgent_question",
        "C-OQ": "oral_questions",
    }
