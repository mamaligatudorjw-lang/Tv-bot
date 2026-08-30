import json
import sqlite3

from forward_ema_confirmed_sl_audit import (
    EXPLORATORY_CUTOFF_TS,
    FORWARD_THRESHOLD_PCT,
    MIN_GROUP_N,
    build_report,
    classify_rule,
    load_forward_rows,
    write_report,
)


def _db(tmp_path):
    connection = sqlite3.connect(tmp_path / "alerts.db")
    connection.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY,
            ts_open INTEGER NOT NULL,
            ts_close INTEGER,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            tp_price REAL NOT NULL,
            status TEXT NOT NULL,
            exit_price REAL,
            is_shadow INTEGER NOT NULL DEFAULT 0,
            alert_type TEXT
        )
        """
    )
    return connection


def _insert(
    connection,
    row_id,
    *,
    risk,
    status="tp",
    direction="LONG",
    ts_open=EXPLORATORY_CUTOFF_TS + 100,
    strategy="ema_cross_confirmed",
    is_shadow=1,
):
    entry = 100.0
    sl = entry * (1.0 - risk / 100.0) if direction == "LONG" else entry * (1.0 + risk / 100.0)
    exit_price = 110.0 if status == "tp" and direction == "LONG" else sl
    if status == "sl" and direction == "SHORT":
        exit_price = sl
    connection.execute(
        """
        INSERT INTO demo_positions (
            id, ts_open, ts_close, symbol, direction, entry_price, sl_price,
            tp_price, status, exit_price, is_shadow, alert_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            ts_open,
            ts_open + 10 if status in ("tp", "sl") else None,
            f"COIN{row_id}USDT",
            direction,
            entry,
            sl,
            110.0 if direction == "LONG" else 90.0,
            status,
            exit_price,
            is_shadow,
            strategy,
        ),
    )


def test_threshold_is_inclusive_and_cutoff_is_strict():
    assert classify_rule({"entry_price": 100.0, "sl_price": 100.0 - FORWARD_THRESHOLD_PCT}) == "candidate_small_sl"
    assert classify_rule({"entry_price": 100.0, "sl_price": 100.0 - FORWARD_THRESHOLD_PCT - 0.00001}) == "control_large_sl"


def test_loader_reads_only_new_resolved_shadow_strategy_rows(tmp_path):
    db = _db(tmp_path)
    _insert(db, 1, risk=3.0, ts_open=EXPLORATORY_CUTOFF_TS)
    _insert(db, 2, risk=3.0, ts_open=EXPLORATORY_CUTOFF_TS + 1)
    _insert(db, 3, risk=3.0, strategy="ema_cross")
    _insert(db, 4, risk=3.0, is_shadow=0)
    db.commit()

    rows = load_forward_rows(tmp_path / "alerts.db")

    assert [row["id"] for row in rows] == [2]


def test_report_confirms_only_with_both_outcome_classes_at_minimum():
    rows = []
    for row_id in range(MIN_GROUP_N):
        row = {
            "id": row_id,
            "ts_open": EXPLORATORY_CUTOFF_TS + row_id + 1,
            "direction": "LONG",
            "entry_price": 100.0,
            "sl_price": 97.0,
            "tp_price": 110.0,
            "status": "tp",
            "exit_price": 110.0,
        }
        rows.append(row)
    for row_id in range(MIN_GROUP_N):
        row = {
            "id": 100 + row_id,
            "ts_open": EXPLORATORY_CUTOFF_TS + 100 + row_id,
            "direction": "LONG",
            "entry_price": 100.0,
            "sl_price": 95.0,
            "tp_price": 110.0,
            "status": "sl",
            "exit_price": 95.0,
        }
        rows.append(row)

    report = build_report(rows, generated_ts=EXPLORATORY_CUTOFF_TS + 1000)

    assert report["cohorts"]["overall"]["verdict"] == "CONFIRMED"
    assert report["cohorts"]["LONG"]["verdict"] == "CONFIRMED"
    assert report["cohorts"]["overall"]["candidate_small_sl"]["tp_rate_pct"] == 100.0
    assert report["cohorts"]["overall"]["control_large_sl"]["tp_rate_pct"] == 0.0
    assert report["production_changes"] is False


def test_report_stays_insufficient_below_minimum(tmp_path):
    rows = []
    for row_id in range(MIN_GROUP_N - 1):
        rows.append(
            {
                "id": row_id,
                "direction": "LONG",
                "entry_price": 100.0,
                "sl_price": 97.0,
                "tp_price": 110.0,
                "status": "tp",
                "exit_price": 110.0,
            }
        )
    for row_id in range(MIN_GROUP_N):
        rows.append(
            {
                "id": 100 + row_id,
                "direction": "LONG",
                "entry_price": 100.0,
                "sl_price": 95.0,
                "tp_price": 110.0,
                "status": "sl",
                "exit_price": 95.0,
            }
        )

    report = build_report(rows)

    assert report["cohorts"]["overall"]["verdict"] == "INSUFFICIENT"
    assert report["cohorts"]["LONG"]["verdict"] == "INSUFFICIENT"


def test_write_report_is_read_only_and_standalone(tmp_path):
    db = _db(tmp_path)
    _insert(db, 1, risk=3.0, status="tp")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM demo_positions").fetchone()[0]

    report = write_report(
        tmp_path / "alerts.db",
        tmp_path / "report",
        generated_ts=EXPLORATORY_CUTOFF_TS + 2000,
    )

    assert report["cohorts"]["overall"]["n_total"] == 1
    assert (tmp_path / "report" / "report.json").exists()
    assert (tmp_path / "report" / "report.md").exists()
    assert (tmp_path / "report" / "rows.csv").exists()
    saved = json.loads((tmp_path / "report" / "report.json").read_text())
    assert saved["cutoff"]["ts"] == EXPLORATORY_CUTOFF_TS
    check = sqlite3.connect(tmp_path / "alerts.db")
    assert check.execute("SELECT COUNT(*) FROM demo_positions").fetchone()[0] == before
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%forward%'"
    ).fetchone() is None
    check.close()