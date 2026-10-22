"""Research Tool source buckets: statements respect the house filter (Lords statements exist now)."""

import pytest

from debate_scanner import _DB_SOURCE_FILTER, minister_sources, research_sources_for_house


@pytest.mark.parametrize("house_filter, expected", [
    ("lords_only", ["lords", "wms_lords"]),
    ("commons_only", ["commons", "westminsterhall", "wms_commons"]),
    ("all", ["commons", "westminsterhall", "lords", "wms"]),
])
def test_sources_for_house(house_filter, expected):
    assert research_sources_for_house(house_filter) == expected
    assert all(s in _DB_SOURCE_FILTER for s in expected)


def test_house_buckets_filter_on_house():
    assert "s.house = 'Commons'" in _DB_SOURCE_FILTER["wms_commons"]
    assert "s.house = 'Lords'" in _DB_SOURCE_FILTER["wms_lords"]
    for key in ("wms_commons", "wms_lords"):
        assert "ministerial_statement" in _DB_SOURCE_FILTER[key]


def test_minister_sources():
    every = research_sources_for_house("all")
    assert minister_sources(every, is_lord=True) == ["lords", "wms"]
    assert minister_sources(every, is_lord=False) == ["commons", "westminsterhall", "wms"]
    assert minister_sources(research_sources_for_house("lords_only"), is_lord=False) == []
    assert minister_sources(research_sources_for_house("commons_only"), is_lord=True) == []
