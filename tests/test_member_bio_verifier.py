"""
Biography verifier regression set (production sample, `refresh_members.py --sample 12`, 2026-10-22).

The verifier had become a wording checker: it dropped true claims because two facts sat on
different source lines, because a date was "June 2023" in the draft and "2023-06" in the source,
because "currently" was not spelled out, or because an absence ("no constituencies recorded")
is not a sentence in the source. Numbered markdown ("**1. Employment and earnings:**") was also
split into junk "claims" such as "**1.".

These tests pin the fixed behaviour: true claims are kept, false ones (wrong committee, wrong
year, wrong party, invented school, a past membership called current, a false absence) are still
dropped. The verification model is mocked: what is tested is the source pack, the deterministic
checks, which claims reach the model, the prompt it is given and how its verdicts are read.

Run: python -m pytest tests/test_member_bio_verifier.py -v
"""

import json
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import member_profiles as mp  # noqa: E402


# ── Source packs (shaped like stored member_profile rows) ────────────────────

def _profile(**kw):
    base = dict(name="X", house="Commons", party="Labour", membership_from="", membership_start=None,
                government_posts=[], opposition_posts=[], committees=[], interests=[], other_posts=[],
                top_policy_areas=[], wq_departments=[], representations=[], house_memberships=[],
                wikidata_id=None, wikidata_facts=None)
    base.update(kw)
    return SimpleNamespace(**base)


GARDINER = _profile(
    name="Barry Gardiner", house="Commons", party="Labour", membership_from="Brent West",
    membership_start=date(1997, 5, 1),
    house_memberships=[{"name": "Commons", "start": "1997-05-01", "end": None}],
    representations=[{"name": "Brent West", "start": "1997-05-01", "end": None}],
    committees=[
        {"name": "Energy Security and Net Zero Committee", "start": "2023-06-12", "end": "2024-05-30"},
        {"name": "Environmental Audit Committee", "start": "2024-10-21", "end": None},
    ],
    interests=[
        {"category": "Employment and earnings", "description": "Payments from Example Media Ltd for articles."},
        {"category": "Visits outside the UK",
         "description": "Visit to Taiwan funded by the Taipei Representative Office."},
    ],
)
GARDINER_WIKI = ("Barry Gardiner studied at the University of St Andrews.\n\n"
                 "He later worked as a shipbroker. In 2015 he spoke at a climate conference.")
GARDINER_INPUTS = mp.bio_inputs(GARDINER, GARDINER_WIKI, "https://en.wikipedia.org/wiki/Example",
                                "Barry Gardiner")

SHEFFIELD = _profile(name="The Lord Bishop of Sheffield", house="Lords", party="Bishops",
                     membership_from="Bishop of Sheffield", membership_start=date(2017, 6, 1),
                     house_memberships=[{"name": "Lords", "start": "2017-06-01", "end": None}])
SHEFFIELD_WIKI = "Pete Wilcox was Dean of Liverpool.\n\nHe became Bishop of Sheffield in 2017."
SHEFFIELD_INPUTS = mp.bio_inputs(SHEFFIELD, SHEFFIELD_WIKI, "https://en.wikipedia.org/wiki/Example2",
                                 "Pete Wilcox")


class ScriptedModel:
    """Stands in for the Gemini verification call: reads the numbered claims out of the prompt and
    answers 'supported' for each, except claims listed as false."""

    def __init__(self, false=()):
        self.false, self.prompts, self.claims = set(false), [], []

    def __call__(self, prompt, api_key, model, json_mode=False, schema=None):
        assert json_mode and schema["properties"]["verdicts"]["items"]["properties"]["supported"]["type"] == "BOOLEAN"
        self.prompts.append(prompt)
        claims = re.findall(r"^(\d+)\. \[([A-Z ]+)\] (.+)$", prompt.split("--- CLAIMS ---", 1)[1], re.M)
        self.claims.extend(text for _, _, text in claims)
        return json.dumps({"verdicts": [
            {"id": int(cid), "supported": text not in self.false,
             "reason": "wrong" if text in self.false else ""} for cid, _, text in claims]})


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(mp, "VERIFY_MODEL", "gemini-2.5-flash-lite")

    def install(false=()):
        scripted = ScriptedModel(false)
        monkeypatch.setattr(mp, "_gemini_call", scripted)
        return scripted
    return install


# ── Dates: the source pack is written the way a draft writes dates ───────────

class TestDates:
    @pytest.mark.parametrize("value,expected", [
        ("2023-06-12", "June 2023"), ("2023-06", "June 2023"), ("2023", "2023"), ("", ""), (None, ""),
        ("2023-13", "2023"),
    ])
    def test_month_year(self, value, expected):
        assert mp.month_year(value) == expected

    def test_long_date_and_humanise(self):
        assert mp.long_date("2024-07-04") == "4 July 2024"
        assert mp.humanise_iso_dates("began 2024-07-04; 2023-24 session") == "began 4 July 2024; 2023-24 session"

    def test_posts_lines(self):
        lines = mp._posts_lines(GARDINER.committees + [{"name": "Undated Committee", "start": None, "end": None}])
        assert lines == ["  - Environmental Audit Committee (October 2024–present)",
                         "  - Energy Security and Net Zero Committee (June 2023–May 2024)",
                         "  - Undated Committee (current)"]

    def test_record_uses_words_not_iso(self):
        record = mp.bio_source_texts(GARDINER_INPUTS)["record"]
        assert "Current membership began: 1 May 1997" in record
        assert "(June 2023–May 2024)" in record and "2023-06" not in record
        assert "'present' means current" in record

    def test_draft_prompt_asks_for_source_dates_and_plain_bullets(self):
        prompt = mp.bio_prompt(GARDINER_INPUTS)
        assert "June 2023" in prompt and "No numbered lists" in prompt
        assert "one \"-\" bullet per category" in prompt


# ── Parsing: numbering and category labels are not claims ─────────────────────

class TestParsing:
    def test_sentence_split_ignores_initials_and_abbreviations(self):
        assert mp._sentences_of("He studied under J. K. Smith at St. Albans. He then left.") == [
            "He studied under J. K. Smith at St. Albans.", "He then left."]

    def test_numbered_bold_inline_label_is_one_claim(self):
        items = mp._parse_bio("## Registered interests\n"
                              "**1. Employment and earnings:** Payments from Example Media Ltd for articles.\n"
                              "**3. Visits outside the UK:** Visit to Taiwan funded by the Taipei "
                              "Representative Office.")
        texts = [s["text"] for s in mp._claim_sentences(items)]
        assert texts == ["**Employment and earnings:** Payments from Example Media Ltd for articles.",
                         "**Visits outside the UK:** Visit to Taiwan funded by the Taipei Representative Office."]
        assert mp._rebuild(items, set()).splitlines()[1] == (
            "- **Employment and earnings:** Payments from Example Media Ltd for articles.")

    def test_label_lines_are_kept_only_with_surviving_entries(self):
        bio = ("## Registered interests\n"
               "**1. Employment and earnings:**\n"
               "- Payments from Example Media Ltd for articles.\n"
               "**2. Gifts, benefits and hospitality:**\n"
               "- Tickets from Imaginary Holdings Ltd.\n"
               "**3.**\n"
               "---")
        clean, dropped, kept = mp.deterministic_check(bio, mp.bio_source_texts(GARDINER_INPUTS))
        assert kept == 1 and [d["sentence"] for d in dropped] == ["Tickets from Imaginary Holdings Ltd."]
        assert clean == ("## Registered interests\n**Employment and earnings:**\n"
                         "- Payments from Example Media Ltd for articles.")

    @pytest.mark.parametrize("line", ["**MP for Brent West since 2019**", "Member since 2019:",
                                      "**Brent West MP**"])
    def test_bold_or_dated_lines_are_still_claims(self, line):
        items = mp._parse_bio("## Parliamentary career\n" + line)
        assert [s["text"] for s in mp._claim_sentences(items)] == [line]

    def test_labels_are_not_counted_or_sent_to_the_model(self, model):
        scripted = model()
        bio = ("## Registered interests\nEmployment and earnings:\n"
               "- Payments from Example Media Ltd for articles and more.\n")
        clean, report = mp.verify_bio(bio, GARDINER_INPUTS, "key")
        assert report["checked_claims"] == 1 and report["dropped_claims"] == 0
        assert "Employment and earnings:" in clean
        assert scripted.claims == ["Payments from Example Media Ltd for articles and more."]


# ── The 2026-10-22 sample: true claims kept, false claims dropped ────────────

GARDINER_TRUE = {
    "Barry Gardiner is a Labour MP for Brent West.": "parliamentary career",
    "Energy Security and Net Zero Committee (June 2023–May 2024)": "committee memberships",
    "He currently serves on the Environmental Audit Committee.": "committee memberships",
    "His registered interests include payments from Example Media Ltd for articles and a visit to "
    "Taiwan funded by the Taipei Representative Office.": "registered interests",
    "Before his election in 1997 he worked as a shipbroker.": "background",
}
GARDINER_FALSE_DETERMINISTIC = {
    "He served on the Treasury Committee (June 2023–May 2024).": ("committee memberships", "Treasury Committee"),
    "Energy Security and Net Zero Committee (June 2019–May 2024)": ("committee memberships", "2019"),
    "He was educated at Glasgow Academy.": ("background", "Glasgow Academy"),
    # 2015 is only in Wikipedia: Parliament's own dates are the ones that count for committees.
    "He joined the Environmental Audit Committee in 2015.": ("committee memberships", "2015"),
}
GARDINER_FALSE_MODEL = {
    "Barry Gardiner is a Conservative MP for Brent West.": "parliamentary career",
    "He currently serves on the Energy Security and Net Zero Committee.": "committee memberships",
    "No committee memberships are recorded.": "committee memberships",
}

SHEFFIELD_TRUE = {
    "Before becoming Bishop of Sheffield in 2017 he was Dean of Liverpool.": "background",
    "No constituencies represented are recorded.": "parliamentary career",
    "He has held no government or opposition roles.": "government and opposition roles",
    "No committee memberships are recorded.": "committee memberships",
}
SHEFFIELD_FALSE = {
    "He was previously Dean of Durham Cathedral.": "background",
}

SECTION_TITLES = {"background": "Background", "parliamentary career": "Parliamentary career",
                  "government and opposition roles": "Government and opposition roles",
                  "committee memberships": "Committee memberships", "registered interests": "Registered interests"}


def _draft(claims: dict) -> str:
    """One bullet per claim under its section heading."""
    by_section: dict = {}
    for text, section in claims.items():
        by_section.setdefault(section if isinstance(section, str) else section[0], []).append(text)
    return "\n".join(f"## {SECTION_TITLES[sec]}\n" + "\n".join(f"- {t}" for t in texts)
                     for sec, texts in by_section.items())


class TestSampleRegression:
    def test_true_claims_pass_the_deterministic_checks(self):
        texts = mp.bio_source_texts(GARDINER_INPUTS)
        clean, dropped, kept = mp.deterministic_check(_draft(GARDINER_TRUE), texts)
        assert dropped == [] and kept == len(GARDINER_TRUE)
        texts = mp.bio_source_texts(SHEFFIELD_INPUTS)
        clean, dropped, kept = mp.deterministic_check(_draft(SHEFFIELD_TRUE), texts)
        assert dropped == [] and kept == len(SHEFFIELD_TRUE)

    def test_false_claims_dropped_deterministically_with_reason(self):
        texts = mp.bio_source_texts(GARDINER_INPUTS)
        clean, dropped, kept = mp.deterministic_check(_draft(GARDINER_FALSE_DETERMINISTIC), texts)
        reasons = {d["sentence"]: d["reason"] for d in dropped}
        assert kept == 0
        for text, (_, missing) in GARDINER_FALSE_DETERMINISTIC.items():
            assert missing in reasons[text]

    def test_gardiner_full_draft(self, model):
        scripted = model(false=GARDINER_FALSE_MODEL)
        draft = _draft({**GARDINER_TRUE, **GARDINER_FALSE_DETERMINISTIC, **GARDINER_FALSE_MODEL})
        clean, report = mp.verify_bio(draft, GARDINER_INPUTS, "key")
        for text in GARDINER_TRUE:
            assert text in clean
        for text in list(GARDINER_FALSE_DETERMINISTIC) + list(GARDINER_FALSE_MODEL):
            assert text not in clean
        assert report["dropped_claims"] == len(GARDINER_FALSE_DETERMINISTIC) + len(GARDINER_FALSE_MODEL)
        assert report["verified_claims"] == len(GARDINER_TRUE)
        # The committee line matches the source word for word now that dates are in words: no model call.
        assert "Energy Security and Net Zero Committee (June 2023–May 2024)" not in scripted.claims
        # Claims already dropped deterministically are not sent to the model either.
        assert not set(GARDINER_FALSE_DETERMINISTIC) & set(scripted.claims)
        assert len(scripted.prompts) == 1

    def test_all_true_gardiner_draft_is_published(self, model):
        model()
        clean, report = mp.verify_bio(_draft(GARDINER_TRUE), GARDINER_INPUTS, "key")
        assert report["dropped_claims"] == 0 and not report["needs_review"]

    def test_bishop_absences_and_split_paragraphs(self, model):
        scripted = model(false=SHEFFIELD_FALSE)
        clean, report = mp.verify_bio(_draft({**SHEFFIELD_TRUE, **SHEFFIELD_FALSE}), SHEFFIELD_INPUTS, "key")
        for text in SHEFFIELD_TRUE:
            assert text in clean
        assert "Dean of Durham" not in clean
        assert report["dropped_claims"] == 1 and not report["needs_review"]
        assert "Durham Cathedral" in report["dropped"][0]["reason"]          # caught before the model
        assert "No constituencies represented are recorded." in scripted.claims
        assert "Constituencies represented:\n  None recorded." in scripted.prompts[0]


# ── The verification prompt and verdict parsing ──────────────────────────────

class TestVerificationPrompt:
    def _prompt(self, claims):
        return mp.verification_prompt(claims, mp.bio_source_texts(GARDINER_INPUTS))

    def test_judges_support_from_the_sources_taken_together(self):
        prompt = self._prompt([{"id": 0, "text": "A claim.", "section": "summary"}])
        for rule in ("TAKEN TOGETHER", "different lines, paragraphs or blocks",
                     '"2023-06", "June 2023" and "Jun 2023" are the same month',
                     'marked "present" or "current"', '"None recorded." or "None declared."',
                     "the PARLIAMENT RECORD is correct", "not to police wording"):
            assert rule in prompt
        for strict in ("invented school", "wrong date, year, number, name, party, seat, committee",
                       "wrong person", "as current when the source gives it an end date",
                       "when the matching block does list entries"):
            assert strict in prompt

    def test_sources_and_claims_rendered_for_comparison(self):
        prompt = self._prompt([
            {"id": 3, "text": "**Employment and earnings:** Payments from Example Media Ltd.",
             "section": "registered interests"},
            {"id": 4, "text": "He has sat on the Environmental Audit Committee since 2024-10-21.",
             "section": "committee memberships"},
            {"id": 5, "text": "He is an MP.", "section": None},
        ])
        assert "Environmental Audit Committee (October 2024–present)" in prompt
        assert "University of St Andrews" in prompt                              # background included
        claims = prompt.split("--- CLAIMS ---", 1)[1]
        assert "3. [REGISTERED INTERESTS] Employment and earnings: Payments from Example Media Ltd." in claims
        assert "4. [COMMITTEE MEMBERSHIPS] He has sat on the Environmental Audit Committee since 21 October 2024." in claims
        assert "5. [PROFILE] He is an MP." in claims

    def test_parse_verdicts(self):
        claims = [{"id": 0}, {"id": 1}]
        raw = json.dumps({"verdicts": [{"id": 0, "supported": True},
                                       {"id": 1, "supported": False, "reason": "wrong party"},
                                       {"id": 9, "supported": False}]})
        assert mp.parse_verdicts(raw, claims) == {0: (True, ""), 1: (False, "wrong party")}
        with pytest.raises(mp.VerificationIncomplete):
            mp.parse_verdicts("not json", claims)
        with pytest.raises(mp.VerificationIncomplete):
            mp.parse_verdicts(json.dumps({"verdicts": [{"id": 0, "supported": "true"},
                                                       {"id": 1, "supported": True}]}), claims)
        with pytest.raises(mp.VerificationIncomplete):
            mp.parse_verdicts(json.dumps([]), claims)

    def test_extra_ids_from_the_model_do_not_drop_anything(self, monkeypatch):
        monkeypatch.setattr(mp, "VERIFY_MODEL", "gemini-2.5-flash-lite")
        monkeypatch.setattr(mp, "_gemini_call", lambda *a, **k: json.dumps({"verdicts": [
            {"id": 0, "supported": True}, {"id": 7, "supported": False}]}))
        clean, report = mp.verify_bio("## Summary\nBarry Gardiner is a Labour MP for Brent West.",
                                      GARDINER_INPUTS, "key")
        assert "Barry Gardiner is a Labour MP for Brent West." in clean and report["dropped_claims"] == 0
