"""scripts/eval_tagging.py: labelled sample is well-formed; scoring; a full run with a fake model."""

import importlib.util
import re
from pathlib import Path

import pytest

import hansard_archive.tagger as tg
from hansard_archive.policy_areas import HANSARD_POLICY_NAMES

_SCRIPT = Path(__file__).parents[1] / "scripts" / "eval_tagging.py"


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("eval_tagging", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sample_is_well_formed(ev):
    items = ev.load_sample()
    kinds = [i["kind"] for i in items]
    assert kinds.count("session") >= 40 and kinds.count("pq") >= 35
    valid = set(HANSARD_POLICY_NAMES)
    ids = set()
    for i in items:
        g = i["gold"]
        assert i["id"] not in ids
        ids.add(i["id"])
        assert set(g["policy_areas"]) <= valid and set(g["acceptable_areas"]) <= valid, i["id"]
        assert set(g.get("primary", [])) <= valid
        assert len(g["policy_areas"]) <= 3
        assert all(isinstance(t, list) and t for t in g["topics"] + g["acceptable_topics"])
        assert g["policy_areas"] or g["acceptable_areas"], i["id"]
        if i["kind"] == "session":
            assert i["contributions"] and i["title"]
        else:
            assert i["question_text"] and i["uin"]
    # The traps the sample exists for are labelled
    by_title = {i.get("title"): i for i in items if i["kind"] == "session"}
    assert by_title["Hares"]["gold"]["policy_areas"] == ["Environment"]
    assert "Welfare and benefits" not in by_title["Hares"]["gold"]["acceptable_areas"]
    assert by_title["Kinship Carers: Statutory Paid Employment Leave"]["gold"]["policy_areas"] == [
        "Children and families"]


@pytest.mark.parametrize("pred, phrasings, ok", [
    ("send", ["special educational needs"], True),              # canonical topic
    ("rail fare", ["rail fares"], True),                         # plural
    ("north sea oil and gas workers", ["north sea oil and gas"], True),   # containment
    ("hares", ["brown hares"], True),
    ("animal welfare", ["hare protection"], False),
    ("nhs waiting times", ["school funding"], False),
])
def test_topic_matches(ev, pred, phrasings, ok):
    assert ev.topic_matches(pred, phrasings) is ok


def test_score_item(ev):
    gold = {"policy_areas": ["Transport"], "acceptable_areas": ["Local government"],
            "topics": [["chetwynd bridge"]], "acceptable_topics": [["bridge maintenance"]]}
    s = ev.score_item(["Transport", "Local government", "Economy"],
                      ["chetwynd bridge", "bridge maintenance", "potholes"], gold)
    assert (s["area_pred"], s["area_correct"], s["area_required"], s["area_found"]) == (3, 2, 1, 1)
    assert s["primary_hit"] == 1 and s["wrong_areas"] == ["Economy"]
    assert (s["topic_pred"], s["topic_correct"], s["topic_found"]) == (3, 2, 1)
    assert s["wrong_topics"] == ["potholes"]


def test_full_run_with_an_oracle_model(ev, monkeypatch):
    """A fake model that returns the gold tags with real quotes scores 100%."""
    items = [i for i in ev.load_sample() if i["gold"]["policy_areas"] and i["gold"]["topics"]]
    sessions = [i for i in items if i["kind"] == "session"
                and not i["title"].lower().startswith(("engagements", "topical", "business of"))][:3]
    pqs = [i for i in items if i["kind"] == "pq"][:7]
    by_uin = {i["uin"]: i for i in pqs}

    def tags_for(item, quote):
        g = item["gold"]
        return {"policy_areas": [{"area": a, "evidence": quote} for a in g["policy_areas"]],
                "topics": [{"topic": t[0], "evidence": quote} for t in g["topics"]]}

    def fake(key, model, prompt, schema):
        usage = {"promptTokenCount": 1000, "candidatesTokenCount": 100}
        if schema is tg._BATCH_RESPONSE_SCHEMA:
            uins = re.findall(r"UIN: (\S+)", prompt)
            return [dict(uin=u, **tags_for(by_uin[u], " ".join(by_uin[u]["question_text"].split()[:5])))
                    for u in uins], usage
        item = next(i for i in sessions if f"Title: {i['title']}\n" in prompt)
        first = " ".join(item["contributions"][0][1].split()[:5])
        return tags_for(item, first), usage

    monkeypatch.setattr(tg, "_gemini_json", fake)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    rows = ev.evaluate(sessions + pqs, "gemini-2.5-flash-lite", "key")
    for kind in ("session", "pq"):
        s = ev.summarise([r for r in rows if r["kind"] == kind])
        assert s["area_precision"] == s["area_recall"] == s["topic_recall"] == 1.0, (kind, s)
        assert s["primary_accuracy"] == 1.0 and s["evidence_drop_rate"] == 0.0
        assert s["cost_per_1000"] > 0


def test_cli_dry_run_and_missing_key(ev, monkeypatch, capsys):
    assert ev.main(["--dry-run"]) == 0
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ev.main(["--kind", "pq", "--limit", "1"]) == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().out


def test_usage_cost():
    assert tg.usage_cost({"promptTokenCount": 1_000_000, "candidatesTokenCount": 500_000,
                          "thoughtsTokenCount": 500_000}, "gemini-2.5-flash") == pytest.approx(0.30 + 2.50)
    assert tg.model_prices("gemini-2.5-flash-lite-preview-09-2025") == (0.10, 0.40)
    assert tg.usage_cost({}, "unknown-model") is None
