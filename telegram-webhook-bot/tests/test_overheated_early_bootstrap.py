import sqlite3

import pytest

from overheated_early_bootstrap import (
    BREAKEVEN_WR_PCT,
    bootstrap_metrics,
    load_resolved_rows,
)


def _row(signal_id, status, result_r, ts_close):
    return {
        "id": signal_id,
        "status": status,
        "result_r": result_r,
        "ts_close": ts_close,
    }


def test_bootstrap_resamples_unique_signal_rows_and_reports_both_metrics():
    rows = [
        _row(1, "tp", 2.0, 1787422679),
        _row(2, "sl", -1.0, 1787422679),
        _row(3, "sl", -1.0, 1787422679),
    ]

    result = bootstrap_metrics(rows, iterations=200, seed=7)

    assert result["n"] == 3
    assert result["wins"] == 1
    assert result["wr_pct"] == pytest.approx(100 / 3)
    assert result["avg_r"] == pytest.approx(0.0)
    assert result["breakeven_wr_pct"] == pytest.approx(BREAKEVEN_WR_PCT)
    assert result["delta_wr_minus_breakeven_pp"] == pytest.approx(0.0)
    assert result["avg_r_ci_crosses_zero"] is True
    assert result["delta_ci_crosses_zero"] is True
    assert result["bootstrap_iterations"] == 200


def test_load_resolved_rows_opens_sqlite_read_only(tmp_path):
    db_path = tmp_path / "alerts.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE demo_positions (
          id INTEGER PRIMARY KEY,
          ts_open INTEGER,
          ts_close INTEGER,
          symbol TEXT,
          direction TEXT,
          status TEXT,
          entry_price REAL,
          sl_price REAL,
          tp_price REAL,
          exit_price REAL,
          is_shadow INTEGER,
          alert_type TEXT
        );
        INSERT INTO demo_positions VALUES
          (1, 100, 200, 'AAAUSDT', 'LONG', 'tp', 100, 90, 120, 120, 1, 'overheated_early'),
          (2, 101, 201, 'BBBUSDT', 'LONG', 'open', 100, 90, 120, NULL, 1, 'overheated_early'),
          (3, 102, 202, 'tp', 'LONG', 'tp', 100, 90, 120, 120, 0, 'overheated_early');
        """
    )
    connection.commit()
    connection.close()

    rows = load_resolved_rows(db_path)

    assert [row["id"] for row in rows] == [1]
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro", uri=True
        ).execute("DELETE FROM demo_positions")