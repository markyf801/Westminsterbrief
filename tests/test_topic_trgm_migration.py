"""migrations/topic_tag_trgm_indexes.py: additive SQL and the INVALID-index check. No database."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def mig():
    path = Path(__file__).parents[1] / "migrations" / "topic_tag_trgm_indexes.py"
    spec = importlib.util.spec_from_file_location("topic_tag_trgm_indexes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sqlite_does_nothing(mig):
    assert mig.steps("sqlite") == []


def test_postgres_steps_are_additive_and_checked(mig):
    planned = mig.steps("postgresql")
    for _label, sql, index in planned:
        assert "IF NOT EXISTS" in sql
        assert not any(w in sql.upper() for w in ("DROP", "DELETE", "TRUNCATE", "UPDATE"))
        if index:
            assert "CONCURRENTLY" in sql and index in sql
    assert sum(1 for *_x, index in planned if index) == 2


def _engine(valid_rows):
    """Fake Postgres engine; index_valid() sees valid_rows in turn ((True,), (False,) or None)."""
    rows = iter(valid_rows)
    conn = MagicMock()

    def execute(stmt, params=None):
        res = MagicMock()
        res.first.return_value = next(rows) if params else None
        return res
    conn.execute.side_effect = execute
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.connect.return_value.execution_options.return_value.__enter__.return_value = conn
    return engine


def test_apply_ok_when_indexes_valid(mig):
    assert mig.apply(_engine([(True,), (True,)])) == []


@pytest.mark.parametrize("rows, word", [([(False,)], "INVALID"), ([None], "missing")])
def test_apply_reports_invalid_or_missing_index(mig, rows, word):
    problems = mig.apply(_engine(rows))
    assert len(problems) == 1 and word in problems[0]
