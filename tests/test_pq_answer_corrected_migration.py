"""migrations/pq_answer_corrected_date.py: adds the column, keeps rows, re-run is a no-op."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).parents[1]


def _migration():
    spec = importlib.util.spec_from_file_location(
        "pq_answer_corrected_date", PROJECT_ROOT / "migrations" / "pq_answer_corrected_date.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_adds_column_once_and_keeps_rows():
    mig = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE ha_pq (id INTEGER PRIMARY KEY, uin VARCHAR(50))"))
        conn.execute(text("INSERT INTO ha_pq (id, uin) VALUES (1, '23043')"))

    mig.upgrade(engine)
    mig.upgrade(engine)          # second run: no error, nothing changes

    cols = {c["name"] for c in inspect(engine).get_columns("ha_pq")}
    assert "answer_corrected_date" in cols
    with engine.connect() as conn:
        assert conn.execute(text("SELECT uin, answer_corrected_date FROM ha_pq")).fetchall() == [
            ("23043", None)]


def test_postgres_steps_are_guarded():
    steps = _migration().steps("postgresql")
    sql = " ".join(s for _label, s in steps)
    assert "ADD COLUMN IF NOT EXISTS answer_corrected_date DATE" in sql
    assert "lock_timeout" in sql
    assert "DROP" not in sql.upper() and "DEFAULT" not in sql.upper()
