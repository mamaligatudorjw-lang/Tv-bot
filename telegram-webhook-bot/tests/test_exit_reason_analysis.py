import sqlite3

import pytest

from exit_reason_analysis import (
    annotate_rows,
    build_summary,
    classify_close_reason,
    load_resolved,
)


@pytest.mark.parametrize(
    ("status", "exit_method", "expected"),
    [
        ("tp", "poll", "tp"),
        ("sl", "wick", "sl"),
        ("manual", "manual", "admin"),
        ("manual", None, "admin"),
        ("ttl_expired", None, "other"),
    ],
)
def test_classify_close_reason(status, exit_method, expected):
    assert classify_close_reason(
        {"status": status, "exit_method": exit_method}
    ) == expected


def _row(
    row_id,
    strategy,
    direction,
    status,
    exit_method,
    exit_price,
):
    return {
        "id": row_id,
        "ts_open": row_id,
        "symbol": "TESTUSDT",
        "direction": direction,
        "entry_price": 100.0,
        "sl_price": 90.0,
        "tp_price": 120.0,
        "status": status,
        "exit_method": exit_method,
        "exit_price": exit_price,
        "alert_type": strategy,
    }


def test_annotation_keeps_missing_regime_unknown_and_excludes_admin_from_wr():
    positions = [
        _row(1, "strategy_a", "LONG", "tp", "poll", 120.0),
        _row(2, "strategy_a", "LONG", "sl", "poll", 90.0),
        _row(3, "strategy_a", "LONG", "manual", "manual", None),
        _row(4, "strategy_a", "LONG", "ttl_expired", None, None),
    ]

    annotated = annotate_rows(positions, {})
    summary = build_summary(annotated, ("strategy_a",), minimum_n=4)
    cohort = next(
        row
        for row in summary
        if row["scope"] == "strategy"
        and row["strategy"] == "strategy_a"
        and row["direction"] == "LONG"
        and row["trend_regime"] == "unknown"
    )

    assert cohort["n"] == 4
    assert cohort["tp_n"] == 1
    assert cohort["sl_n"] == 1
    assert cohort["admin_n"] == 1
    assert cohort["other_n"] == 1
    assert cohort["wr_pct"] == pytest.approx(50.0)
    assert cohort["avg_r"] == pytest.approx(0.5)
    assert cohort["sample_status"] == "ready"
    assert all(row["trend_regime"] == "unknown" for row in annotated)
    assert all(row["regime_reason"] == "snapshot_missing" for row in annotated)


def test_summary_keeps_empty_long_short_regime_cells():
    rows = annotate_rows(
        [_row(1, "strategy_a", "SHORT", "sl", "poll", 90.0)],
        {"1": {"id": "1", "trend_regime": "bear"}},
    )
    summary = build_summary(rows, ("strategy_a",), minimum_n=2)

    long_bull = next(
        row
        for row in summary
        if row["scope"] == "strategy"
        and row["direction"] == "LONG"
        and row["trend_regime"] == "bull"
    )
    short_bear = next(
        row
        for row in summary
        if row["scope"] == "strategy"
        and row["direction"] == "SHORT"
        and row["trend_regime"] == "bear"
    )

    assert long_bull["n"] == 0
    assert long_bull["sample_status"] == "insufficient"
    assert short_bear["n"] == 1
    assert short_bear["sample_status"] == "insufficient"


def test_load_resolved_is_read_only_and_includes_other_statuses(tmp_path):
    db_path = tmp_path / "alerts.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY, ts_open INTEGER, symbol TEXT,
            direction TEXT, entry_price REAL, sl_price REAL, tp_price REAL,
            size_usd REAL, status TEXT, ts_close INTEGER, exit_price REAL,
            pnl_usd REAL, is_shadow INTEGER, shadow_reason TEXT,
            alert_type TEXT, exit_method TEXT, wick_close INTEGER
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO demo_positions VALUES
        (?, ?, 'TESTUSDT', 'LONG', 100, 90, 120, 100, ?, ?, ?, NULL,
         1, NULL, 'strategy_a', ?, 0)
        """,
        [
            (1, 100, "manual", 200, None, "manual"),
            (2, 101, "ttl_expired", None, None, None),
            (3, 102, "open", None, None, None),
        ],
    )
    connection.commit()
    connection.close()
    before = db_path.read_bytes()

    rows = load_resolved(db_path, ("strategy_a",))

    assert [row["status"] for row in rows] == ["manual", "ttl_expired"]
    assert db_path.read_bytes() == before
    assert not (tmp_path / "alerts.db-wal").exists()
    assert not (tmp_path / "alerts.db-journal").exists()