import csv
import sqlite3

import pytest

from trailing_shadow import (
    BOOTSTRAP_ITERATIONS,
    MIN_FORWARD_PAIRS,
    TRAILING_SHADOW_FREEZE_TS,
    advance_state,
    advance_open_trackers,
    create_tracker,
    generate_report,
    initialize_schema,
    load_open_trackers,
    read_report_status,
    tracked_strategy,
)


def _state(**overrides):
    state = {
        "direction": "LONG",
        "entry_price": 100.0,
        "initial_sl_price": 90.0,
        "tp_price": 120.0,
        "step_pct": 8.0,
        "activation_r": 0.5,
        "activation_label": "+0.5R",
        "current_stop": 90.0,
        "favorable_extreme": 100.0,
        "activated": 0,
    }
    state.update(overrides)
    return state


def _db_with_source(tmp_path):
    db = sqlite3.connect(tmp_path / "alerts.db")
    db.execute(
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
            is_shadow INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    initialize_schema(db)
    return db


def _add_closed_pair(db, source_id, strategy, ts_open, shadow_exit=115.0):
    db.execute(
        "INSERT INTO demo_positions "
        "(id,ts_open,symbol,direction,entry_price,sl_price,tp_price,status,"
        "ts_close,exit_price,is_shadow) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            ts_open,
            f"COIN{source_id}_USDT",
            "LONG",
            100.0,
            90.0,
            120.0,
            "tp",
            ts_open + 100,
            110.0,
            0,
        ),
    )
    assert create_tracker(
        db,
        source_demo_id=source_id,
        ts_open=ts_open,
        symbol=f"COIN{source_id}_USDT",
        direction="LONG",
        alert_type=strategy,
        source_is_shadow=False,
        entry_price=100.0,
        sl_price=90.0,
        tp_price=120.0,
    )
    db.execute(
        "UPDATE trailing_shadow_positions SET status='closed', ts_close=?, "
        "exit_price=?, outcome='trail_stop', exit_method='live_price', result_r=? "
        "WHERE source_demo_id=?",
        (ts_open + 120, shadow_exit, (shadow_exit - 100.0) / 10.0, source_id),
    )


def test_frozen_configs_and_boundary():
    assert tracked_strategy("overheated_24h", TRAILING_SHADOW_FREEZE_TS)
    assert tracked_strategy("ema_cross_confirmed", TRAILING_SHADOW_FREEZE_TS)
    assert not tracked_strategy("overheated_24h", TRAILING_SHADOW_FREEZE_TS - 1)
    assert not tracked_strategy("other", TRAILING_SHADOW_FREEZE_TS)


def test_activation_updates_stop_but_new_stop_applies_next_snapshot():
    state = _state()

    first = advance_state(state, 106.0, 100)

    assert first["event"] == "activated"
    assert state["activated"] == 1
    assert state["current_stop"] == pytest.approx(97.52)
    assert state["status"] if "status" in state else "open"

    second = advance_state(state, 97.4, 130)

    assert second["event"] == "trail_stop"
    assert state["outcome"] == "trail_stop"
    assert state["exit_price"] == pytest.approx(97.4)


def test_any_profit_activation_and_tp_stop_paths():
    state = _state(activation_r=None, activation_label="any_profit")
    at_entry = advance_state(state, 100.0, 100)
    assert at_entry["event"] is None
    assert not state["activated"]

    just_profitable = advance_state(state, 100.01, 130)
    assert just_profitable["event"] == "activated"

    tp_state = _state()
    advance_state(tp_state, 120.0, 100)
    assert tp_state["outcome"] == "tp"
    assert tp_state["result_r"] == pytest.approx(2.0)

    short_state = _state(
        direction="SHORT",
        tp_price=80.0,
        step_pct=6.0,
        current_stop=110.0,
        activation_r=None,
        activation_label="any_profit",
    )
    advance_state(short_state, 110.0, 100)
    assert short_state["outcome"] == "sl"


def test_open_tracker_persists_and_source_is_independent(tmp_path):
    db = _db_with_source(tmp_path)
    db.execute(
        "INSERT INTO demo_positions "
        "(id,ts_open,symbol,direction,entry_price,sl_price,tp_price,status,is_shadow) "
        "VALUES (1,?,?,?,?,?,?,?,?)",
        (TRAILING_SHADOW_FREEZE_TS, "BTC_USDT", "LONG", 100, 90, 120, "open", 0),
    )
    assert create_tracker(
        db,
        source_demo_id=1,
        ts_open=TRAILING_SHADOW_FREEZE_TS,
        symbol="BTC_USDT",
        direction="LONG",
        alert_type="overheated_24h",
        source_is_shadow=False,
        entry_price=100,
        sl_price=90,
        tp_price=120,
    )
    db.commit()

    restarted = sqlite3.connect(tmp_path / "alerts.db")
    trackers = load_open_trackers(restarted)
    assert len(trackers) == 1
    assert trackers[0]["activated"] == 0
    assert advance_open_trackers(
        restarted, trackers, {"BTC_USDT": 106.0}, TRAILING_SHADOW_FREEZE_TS + 30
    ) == 0
    restarted.commit()

    source = restarted.execute(
        "SELECT status, exit_price FROM demo_positions WHERE id=1"
    ).fetchone()
    shadow = restarted.execute(
        "SELECT activated, current_stop FROM trailing_shadow_positions WHERE source_demo_id=1"
    ).fetchone()
    assert source == ("open", None)
    assert shadow[0] == 1
    assert shadow[1] == pytest.approx(97.52)


def test_report_is_insufficient_then_rolls_bootstrap(tmp_path):
    db = _db_with_source(tmp_path)
    for index in range(MIN_FORWARD_PAIRS - 1):
        _add_closed_pair(
            db,
            index * 2 + 1,
            "overheated_24h",
            TRAILING_SHADOW_FREEZE_TS + index,
        )
        _add_closed_pair(
            db,
            index * 2 + 2,
            "ema_cross_confirmed",
            TRAILING_SHADOW_FREEZE_TS + index,
        )
    db.commit()
    output = tmp_path / "report"

    coverage = generate_report(tmp_path / "alerts.db", output)

    assert not coverage["all_strategies_ready"]
    status = read_report_status(output)
    assert status["minimum_pairs"] == MIN_FORWARD_PAIRS
    assert all(
        not strategy["ready_for_bootstrap"]
        and strategy["n_pairs"] == MIN_FORWARD_PAIRS - 1
        for strategy in status["strategies"]
    )
    with (output / "paired_bootstrap.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["status"] for row in rows} == {"insufficient"}
    assert all(int(row["bootstrap_iterations"] or 0) == 0 for row in rows)

    _add_closed_pair(
        db,
        1001,
        "overheated_24h",
        TRAILING_SHADOW_FREEZE_TS + 1000,
        shadow_exit=118.0,
    )
    _add_closed_pair(
        db,
        1002,
        "ema_cross_confirmed",
        TRAILING_SHADOW_FREEZE_TS + 1000,
        shadow_exit=118.0,
    )
    db.commit()
    ready = generate_report(tmp_path / "alerts.db", output)
    assert ready["all_strategies_ready"]
    ready_status = read_report_status(output)
    assert all(
        strategy["ready_for_bootstrap"]
        and strategy["n_pairs"] == MIN_FORWARD_PAIRS
        and strategy["bootstrap"]["status"] == "ready"
        for strategy in ready_status["strategies"]
    )

    with (output / "paired_bootstrap.csv").open(newline="") as handle:
        ready_rows = list(csv.DictReader(handle))
    assert all(
        row["status"] == "ready"
        and int(row["n_pairs"]) == MIN_FORWARD_PAIRS
        and int(row["bootstrap_iterations"]) == BOOTSTRAP_ITERATIONS
        for row in ready_rows
    )
    first_ci = {
        row["strategy"]: row["mean_ci95_high"] for row in ready_rows
    }

    _add_closed_pair(
        db,
        1003,
        "overheated_24h",
        TRAILING_SHADOW_FREEZE_TS + 1001,
        shadow_exit=119.0,
    )
    _add_closed_pair(
        db,
        1004,
        "ema_cross_confirmed",
        TRAILING_SHADOW_FREEZE_TS + 1001,
        shadow_exit=119.0,
    )
    db.commit()
    generate_report(tmp_path / "alerts.db", output)
    with (output / "paired_bootstrap.csv").open(newline="") as handle:
        rolling_rows = list(csv.DictReader(handle))
    assert all(int(row["n_pairs"]) == MIN_FORWARD_PAIRS + 1 for row in rolling_rows)
    assert any(first_ci[row["strategy"]] != row["mean_ci95_high"] for row in rolling_rows)