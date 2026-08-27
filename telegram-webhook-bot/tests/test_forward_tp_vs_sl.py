import json
import sqlite3

import pytest

from forward_tp_vs_sl import (
    FORWARD_DIRECTION,
    FORWARD_EXPERIMENT_KEY,
    FORWARD_FREEZE_TS,
    FORWARD_MIN_OUTCOME_N,
    FORWARD_SL_THRESHOLD_PCT,
    FORWARD_STRATEGY,
    build_report,
    classify_risk,
    initialize_schema,
    sync_outcome,
    track_position,
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
    initialize_schema(connection)
    connection.commit()
    return connection


def _track(connection, source_id, *, risk_pct=3.0, ts_open=FORWARD_FREEZE_TS):
    entry = 100.0
    sl = entry * (1.0 - risk_pct / 100.0)
    return track_position(
        connection,
        source_demo_id=source_id,
        ts_open=ts_open,
        symbol=f"COIN{source_id}USDT",
        direction=FORWARD_DIRECTION,
        alert_type=FORWARD_STRATEGY,
        is_shadow=True,
        entry_price=entry,
        sl_price=sl,
        tp_price=110.0,
    )


def test_frozen_boundary_and_inclusive_threshold():
    assert classify_risk(FORWARD_SL_THRESHOLD_PCT) == "tp_candidate"
    assert classify_risk(FORWARD_SL_THRESHOLD_PCT + 0.00001) == "sl_candidate"


def test_tracker_records_only_new_long_shadow_strategy_rows(tmp_path):
    db = _db(tmp_path)

    assert _track(db, 1, risk_pct=FORWARD_SL_THRESHOLD_PCT)
    assert not track_position(
        db,
        source_demo_id=2,
        ts_open=FORWARD_FREEZE_TS,
        symbol="SHORTUSDT",
        direction="SHORT",
        alert_type=FORWARD_STRATEGY,
        is_shadow=True,
        entry_price=100.0,
        sl_price=97.0,
        tp_price=106.0,
    )
    assert not _track(db, 3, ts_open=FORWARD_FREEZE_TS - 1)

    row = db.execute(
        "SELECT source_demo_id, risk_pct, rule_prediction, status FROM "
        "tp_vs_sl_forward_positions WHERE source_demo_id=1"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == pytest.approx(FORWARD_SL_THRESHOLD_PCT)
    assert row[2] == "tp_candidate"
    assert row[3] == "open"


def test_sync_outcome_is_independent_and_computes_r(tmp_path):
    db = _db(tmp_path)
    assert _track(db, 1, risk_pct=3.0)

    assert sync_outcome(
        db,
        source_demo_id=1,
        status="tp",
        ts_close=FORWARD_FREEZE_TS + 100,
        exit_price=110.0,
    )
    db.commit()

    row = db.execute(
        "SELECT status, exit_price, result_r FROM tp_vs_sl_forward_positions "
        "WHERE source_demo_id=1"
    ).fetchone()
    assert row == ("tp", 110.0, pytest.approx(10.0 / 3.0))
    assert sync_outcome(
        db, source_demo_id=999, status="sl", ts_close=1, exit_price=1.0
    ) is False


def test_metadata_drift_fails_loudly(tmp_path):
    db = _db(tmp_path)
    db.execute(
        "UPDATE tp_vs_sl_forward_meta SET sl_threshold_pct=? "
        "WHERE experiment_key=?",
        (1.0, FORWARD_EXPERIMENT_KEY),
    )
    db.commit()

    with pytest.raises(RuntimeError, match="frozen timestamp, threshold"):
        initialize_schema(db)


def test_report_is_insufficient_until_both_forward_outcome_classes_reach_20(tmp_path):
    db = _db(tmp_path)
    for source_id in range(1, 6):
        assert _track(db, source_id, risk_pct=3.0)
        sync_outcome(
            db,
            source_demo_id=source_id,
            status="tp",
            ts_close=FORWARD_FREEZE_TS + source_id,
            exit_price=110.0,
        )
    for source_id in range(100, 120):
        assert _track(db, source_id, risk_pct=5.0)
        sync_outcome(
            db,
            source_demo_id=source_id,
            status="sl",
            ts_close=FORWARD_FREEZE_TS + source_id,
            exit_price=97.0,
        )
    db.commit()

    report = write_report(
        tmp_path / "alerts.db",
        tmp_path / "report",
        generated_ts=FORWARD_FREEZE_TS + 1000,
    )

    assert report["experiment"]["sl_threshold_pct"] == FORWARD_SL_THRESHOLD_PCT
    assert report["baseline_no_rule"]["tp_first"] == 5
    assert report["baseline_no_rule"]["sl_first"] == FORWARD_MIN_OUTCOME_N
    assert report["verdict"] == "insufficient"
    assert report["verdict_is_allowed"] is False
    saved = json.loads((tmp_path / "report" / "report.json").read_text())
    assert saved["verdict"] == "insufficient"
    assert "n≥20 TP-first" in (tmp_path / "report" / "report.md").read_text()


def test_report_marks_unresolved_separately():
    meta = {
        "experiment_key": FORWARD_EXPERIMENT_KEY,
        "strategy": FORWARD_STRATEGY,
        "direction": FORWARD_DIRECTION,
        "freeze_ts": FORWARD_FREEZE_TS,
        "sl_threshold_pct": FORWARD_SL_THRESHOLD_PCT,
        "min_outcome_n": FORWARD_MIN_OUTCOME_N,
    }
    rows = [
        {
            "id": 1,
            "status": "open",
            "rule_prediction": "tp_candidate",
            "result_r": None,
        },
        {
            "id": 2,
            "status": "tp",
            "rule_prediction": "tp_candidate",
            "result_r": 2.0,
        },
    ]

    report = build_report(meta, rows, generated_ts=FORWARD_FREEZE_TS)

    assert report["baseline_no_rule"]["unresolved"] == 1
    assert report["baseline_no_rule"]["resolved_wr_pct"] == 100.0
    assert report["verdict"] == "insufficient"