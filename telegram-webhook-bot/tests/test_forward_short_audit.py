import json
import sqlite3

import pytest

from forward_short_audit import (
    FORWARD_REPORT_DIR,
    MIN_FORWARD_N,
    build_report,
    classify_verdict,
    load_forward_rows,
    load_or_create_cutoff,
    metrics,
    parse_cutoff,
    write_report,
)


def _db(tmp_path):
    connection = sqlite3.connect(tmp_path / "alerts.db")
    connection.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY,
            ts_open INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            tp_price REAL NOT NULL,
            status TEXT NOT NULL,
            ts_close INTEGER,
            exit_price REAL,
            is_shadow INTEGER NOT NULL DEFAULT 0,
            alert_type TEXT
        )
        """
    )
    return connection


def _insert(
    connection,
    source_id,
    *,
    strategy="ema_cross",
    direction="SHORT",
    ts_open=100,
    status="tp",
    exit_price=95.0,
    is_shadow=1,
):
    connection.execute(
        """
        INSERT INTO demo_positions (
            id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
            status, ts_close, exit_price, is_shadow, alert_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            ts_open,
            f"COIN{source_id}USDT",
            direction,
            100.0,
            105.0,
            90.0,
            status,
            ts_open + 10,
            exit_price,
            is_shadow,
            strategy,
        ),
    )


def test_parse_cutoff_requires_timezone_for_iso():
    assert parse_cutoff("1970-01-01T00:00:20Z") == 20
    assert parse_cutoff(20.9) == 20
    with pytest.raises(ValueError, match="timezone"):
        parse_cutoff("1970-01-01T00:00:20")


def test_cutoff_is_persisted_and_reused_until_explicitly_replaced(tmp_path):
    output = tmp_path / "report"
    assert load_or_create_cutoff(output, now_ts=1234) == 1234
    assert load_or_create_cutoff(output, now_ts=9999) == 1234
    assert load_or_create_cutoff(output, explicit_cutoff="1970-01-01T00:33:20Z") == 2000
    saved = json.loads((output / "cutoff.json").read_text())
    assert saved["cutoff_ts"] == 2000


def test_loader_is_strict_about_cutoff_direction_strategy_and_resolved_shadow(tmp_path):
    db = _db(tmp_path)
    _insert(db, 1, ts_open=100, status="tp")
    _insert(db, 2, ts_open=101, status="sl", exit_price=105.0)
    _insert(db, 3, ts_open=200, direction="LONG")
    _insert(db, 4, ts_open=200, strategy="overheated_early")
    _insert(db, 5, ts_open=200, status="open", exit_price=None)
    _insert(db, 6, ts_open=200, is_shadow=0)
    db.commit()

    rows = load_forward_rows(tmp_path / "alerts.db", 100)

    assert [row["id"] for row in rows] == [2]


def test_metrics_direction_adjusts_short_r_and_excludes_unresolved():
    rows = [
        {
            "status": "tp",
            "entry_price": 100.0,
            "sl_price": 105.0,
            "exit_price": 90.0,
        },
        {
            "status": "sl",
            "entry_price": 100.0,
            "sl_price": 105.0,
            "exit_price": 105.0,
        },
        {"status": "open", "result_r": None},
    ]
    result = metrics(rows)

    assert result["resolved_n"] == 2
    assert result["tp"] == 1
    assert result["sl"] == 1
    assert result["unresolved_n"] == 1
    assert result["resolved_wr_pct"] == 50.0
    assert result["avg_r"] == 0.5


def test_verdict_requires_n20_and_uses_unrounded_sign():
    assert classify_verdict({"resolved_n": MIN_FORWARD_N - 1, "avg_r_unrounded": -1.0})[0] == "INSUFFICIENT"
    assert classify_verdict({"resolved_n": MIN_FORWARD_N, "avg_r_unrounded": -0.000001})[0] == "CONFIRMED"
    assert classify_verdict({"resolved_n": MIN_FORWARD_N, "avg_r_unrounded": 0.000001})[0] == "REFUTED"
    assert classify_verdict({"resolved_n": MIN_FORWARD_N, "avg_r_unrounded": 0.0})[0] == "AMBIGUOUS"


def test_report_has_independent_strategy_verdicts_and_baselines():
    rows = []
    for strategy in ("ema_cross_confirmed", "ema_cross"):
        for index in range(MIN_FORWARD_N):
            rows.append(
                {
                    "id": index,
                    "alert_type": strategy,
                    "status": "sl" if strategy == "ema_cross_confirmed" else "tp",
                    "entry_price": 100.0,
                    "sl_price": 105.0,
                    "tp_price": 90.0,
                    "exit_price": 105.0 if strategy == "ema_cross_confirmed" else 90.0,
                }
            )

    report = build_report(rows, cutoff_ts=100, generated_ts=200)

    assert report["strategies"]["ema_cross_confirmed"]["forward"]["resolved_n"] == 20
    assert report["strategies"]["ema_cross_confirmed"]["verdict"] == "CONFIRMED"
    assert report["strategies"]["ema_cross"]["verdict"] == "REFUTED"
    assert report["strategies"]["ema_cross"]["in_sample_avg_r_display"] == -0.18
    assert report["production_changes"] is False


def test_write_report_is_standalone_and_read_only(tmp_path):
    db = _db(tmp_path)
    _insert(db, 1, ts_open=101, status="tp")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM demo_positions").fetchone()[0]

    report = write_report(
        tmp_path / "alerts.db",
        tmp_path / "report",
        cutoff=100,
        generated_ts=300,
    )

    assert report["strategies"]["ema_cross"]["forward"]["resolved_n"] == 1
    assert report["strategies"]["ema_cross"]["verdict"] == "INSUFFICIENT"
    assert (tmp_path / "report" / "report.json").exists()
    assert (tmp_path / "report" / "report.md").exists()
    assert (tmp_path / "report" / "rows.csv").exists()
    assert (tmp_path / "report" / "cutoff.json").exists()
    check = sqlite3.connect(tmp_path / "alerts.db")
    assert check.execute("SELECT COUNT(*) FROM demo_positions").fetchone()[0] == before
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%forward%'"
    ).fetchone() is None
    check.close()