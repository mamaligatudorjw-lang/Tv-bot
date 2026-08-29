import csv
import json
import sqlite3
from pathlib import Path

import pytest

from confirmation_level_analysis import (
    LEVELS,
    build_cohort_rows,
    infer_level,
    load_resolved,
)


@pytest.mark.parametrize(
    ("ratio", "level", "breakeven"),
    [(2.0, "1/3", 100 / 3), (1.5, "2/3", 40.0), (1.0, "3/3", 50.0)],
)
def test_infer_level_uses_the_persisted_tp_sl_ratio(ratio, level, breakeven):
    result = infer_level(100.0, 90.0, 100.0 + 10.0 * ratio)

    assert result["confirmation_level"] == level
    assert result["rr_multiple"] == ratio
    assert result["breakeven_wr_pct"] == pytest.approx(breakeven)


def test_infer_level_rejects_a_ratio_outside_the_ladder():
    with pytest.raises(ValueError, match="does not match confirmation ladder"):
        infer_level(100.0, 90.0, 117.0)


def _annotated(strategy, direction, regime, level, status, result_r):
    return {
        "strategy": strategy,
        "direction": direction,
        "trend_regime": regime,
        "confirmation_level": level,
        "status": status,
        "result_r": result_r,
    }


def test_cohort_report_keeps_empty_cells_and_calculates_breakeven_delta():
    rows = [
        _annotated("overheated_confirmed", "LONG", "bull", "1/3", "tp", 2.0),
        _annotated("overheated_confirmed", "LONG", "bull", "1/3", "sl", -1.0),
        _annotated("overheated_confirmed", "LONG", "bull", "2/3", "sl", -1.0),
    ]

    report = build_cohort_rows(rows, minimum_n=2)
    level_1 = next(
        row
        for row in report
        if row["sample"] == "confirmation_level"
        and row["strategy"] == "overheated_confirmed"
        and row["direction"] == "LONG"
        and row["regime"] == "bull"
        and row["confirmation_level"] == "1/3"
    )
    level_3_empty = next(
        row
        for row in report
        if row["sample"] == "confirmation_level"
        and row["strategy"] == "overheated_confirmed"
        and row["direction"] == "SHORT"
        and row["regime"] == "bear"
        and row["confirmation_level"] == "3/3"
    )

    assert level_1["n"] == 2
    assert level_1["wr_pct"] == pytest.approx(50.0)
    assert level_1["breakeven_wr_pct"] == pytest.approx(100 / 3)
    assert level_1["delta_wr_minus_breakeven_pp"] == pytest.approx(50 - 100 / 3)
    assert level_1["sample_status"] == "ready"
    assert level_3_empty["n"] == 0
    assert level_3_empty["sample_status"] == "insufficient"


def test_load_resolved_is_read_only(tmp_path):
    db_path = tmp_path / "alerts.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY, ts_open INTEGER, symbol TEXT,
            direction TEXT, entry_price REAL, sl_price REAL, tp_price REAL,
            status TEXT, ts_close INTEGER, exit_price REAL, alert_type TEXT,
            is_shadow INTEGER, shadow_reason TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO demo_positions VALUES
        (1, 100, 'TESTUSDT', 'LONG', 100, 90, 120, 'tp', 200, 120,
         'overheated_confirmed', 1, NULL)
        """
    )
    connection.commit()
    connection.close()
    before = db_path.read_bytes()

    rows = load_resolved(db_path)

    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert db_path.read_bytes() == before
    assert not (tmp_path / "alerts.db-wal").exists()
    assert not (tmp_path / "alerts.db-journal").exists()