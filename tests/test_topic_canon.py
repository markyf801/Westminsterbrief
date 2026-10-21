"""Canonical topic hierarchy (topic_canon), policy-area scope notes, merge proposals."""

import importlib.util
from pathlib import Path

import pytest

from hansard_archive import policy_areas, topic_canon
from hansard_archive.slugs import slugify_theme


@pytest.mark.parametrize("raw, canon", [
    ("SEND", "special educational needs"),
    ("send provision", "special educational needs"),
    ("Special Educational Needs and Disabilities", "special educational needs"),
    ("send funding", "special educational needs funding"),
    ("EHCP delays", "education health and care plans"),
    ("chagos", "chagos islands"),
    ("chagos deal", "chagos islands sovereignty"),
    ("two child limit", "two-child limit"),
    ("nhs waiting times", "nhs waiting times"),     # unknown topics pass through
])
def test_canonical_topic(raw, canon):
    assert topic_canon.canonical_topic(raw) == canon


def test_family_of_parent_includes_children_and_aliases():
    names = topic_canon.topic_family_names("special educational needs")
    assert {"special educational needs", "send", "sen", "send funding", "ehcp",
            "special educational needs funding"} <= names
    child = topic_canon.topic_family_names("send funding")
    assert "send funding" in child and "send" not in child
    assert topic_canon.parent_topic("send funding") == "special educational needs"
    assert topic_canon.topic_family_names("bus services") == {"bus services"}


def test_no_spelling_belongs_to_two_topics():
    seen = {}
    for parent, spec in topic_canon.TOPIC_FAMILIES.items():
        groups = [(parent, [parent, *spec.get("aliases", [])])]
        groups += [(c, [c, *a]) for c, a in (spec.get("children") or {}).items()]
        for canon, names in groups:
            for n in names:
                n = " ".join(n.lower().split())
                assert seen.setdefault(n, canon) == canon, f"{n!r} in {seen[n]!r} and {canon!r}"


def test_alias_slugs_redirect_to_canonical_slug_and_never_shadow_policy_pages():
    assert topic_canon.alias_slug_target("send") == "special-educational-needs"
    assert topic_canon.alias_slug_target("send-funding") == "special-educational-needs-funding"
    assert topic_canon.alias_slug_target("special-educational-needs") is None
    assert topic_canon.alias_slug_target("two-child-limit") is None    # alias slug == canonical slug
    for slug, target in topic_canon._ALIAS_SLUG_REDIRECT.items():
        assert slug not in policy_areas.valid_slugs()
        assert topic_canon.canonical_for_slug(target)
        assert policy_areas.brief_alias(slug) == target


def test_policy_slug_aliases_still_win():
    assert policy_areas.brief_alias("housing") == "housing-and-planning"
    assert policy_areas.brief_alias("economy") is None
    assert policy_areas.brief_alias("bus-services") is None


def test_every_policy_area_has_a_scope_note():
    assert set(policy_areas.POLICY_AREA_SCOPE) == set(policy_areas.HANSARD_POLICY_NAMES)
    for name, note in policy_areas.POLICY_AREA_SCOPE.items():
        assert len(note) > 40, name


def test_every_brief_page_maps_to_valid_hansard_names():
    valid = set(policy_areas.HANSARD_POLICY_NAMES)
    for pa in policy_areas.all_areas():
        assert pa.hansard_names and set(pa.hansard_names) <= valid, pa.slug


def test_canonical_slugs_are_stable():
    for canon in topic_canon.known_canonical_topics():
        assert topic_canon.canonical_for_slug(slugify_theme(canon)) == canon


# --- scripts/propose_topic_merges.py (pure part; the script itself is read-only) ---

@pytest.fixture(scope="module")
def merges():
    path = Path(__file__).parents[1] / "scripts" / "propose_topic_merges.py"
    spec = importlib.util.spec_from_file_location("propose_topic_merges", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_propose_merges_groups_variants_children_and_known(merges):
    counts = {
        "send": 40, "send funding": 9,                       # already mapped
        "rail fares": 12, "Rail fares": 3, "rail fare": 2,   # variants
        "bus services": 30, "bus services funding": 4, "bus services franchising": 3,
        "one-off": 1,
    }
    out = merges.propose_merges(counts, min_count=2)
    assert ("send", "special educational needs", 40) in out["already_mapped"]
    canon, members, total = out["variants"][0]
    assert canon == "rail fares" and total == 17 and ("rail fare", 2) in members
    parents = dict(out["children"])
    assert [t for t, _ in parents["bus services"]] == ["bus services funding", "bus services franchising"]


def test_variant_key(merges):
    assert merges.variant_key("Women's State Pensions") == merges.variant_key("womens state pension")
    assert merges.variant_key("arts & culture") == merges.variant_key("arts and culture")
