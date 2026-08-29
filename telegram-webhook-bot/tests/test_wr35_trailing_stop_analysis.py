import csv
import json
import sqlite3
from pathlib import Path

import pytest

from wr35_trailing_stop_analysis import (
    MIN_COHORT_N,
    WR_THRESHOLD,
    build_filter_decisions,
    run_analysis,
)


def _position(
    row_id: int,
    ts_open: int,
    *,
    status: str = "tp",
    direction: str = "LONG",
    strategy: str = "overheated_24h",
    regime: str = "bull",
    ts_close: int | None = None,
) -> tuple[dict, dict]:
    if ts_close is None:
        ts_close = ts_open + 10
    position = {
        "id": row_id,
        "ts_open": ts_open,
        "ts_close": ts_close,
        "status": status,
        "direction": direction,
        "alert_type": strategy,
    }
    regime_row = {"trend_regime": regime, "regime_reason": "close_vs_ema50"}
    return position, regime_row


def test_dynamic_wr_uses_only_results_known_at_signal_time():
    positions = []
    regimes = {}
    for index in range(19):
        position, regime = _position(index + 1, 100 + index, status="tp")
        positions.append(position)
        regimes[index + 1] = regime

    # This result closes exactly at the next signal timestamp and is allowed.
    boundary, boundary_regime = _position(
        20, 119, status="sl", ts_close=200
    )
    positions.append(boundary)
    regimes[20] = boundary_regime

    # The current signal must not use its own future result, nor a later row.
    current, current_regime = _position(21, 200, status="tp")
    positions.append(current)
    regimes[21] = current_regime
    future, future_regime = _position(22, 300, status="tp")
    positions.append(future)
    regimes[22] = future_regime

    decisions = build_filter_decisions(positions, regimes)
    current_decision = next(row for row in decisions if row["id"] == 21)

    assert current_decision["prior_cohort_n"] == MIN_COHORT_N
    assert current_decision["prior_cohort_wins"] == 19
    assert current_decision["historical_wr_pct"] == pytest.approx(95.0)
    assert current_decision["filter_pass"] == "yes"


def test_exact_wr_threshold_passes_and_below_threshold_does_not():
    positions = []
    regimes = {}
    for index in range(MIN_COHORT_N):
        position, regime = _position(
            index + 1,
            index + 1,
            status="tp" if index < 7 else "sl",
        )
        positions.append(position)
        regimes[index + 1] = regime

    at_boundary, regime = _position(100, 1000)
    positions.append(at_boundary)
    regimes[100] = regime

    below_boundary, regime = _position(101, 1001)
    positions.append(below_boundary)
    regimes[101] = regime

    decisions = build_filter_decisions(positions, regimes)
    boundary = next(row for row in decisions if row["id"] == 100)
    below = next(row for row in decisions if row["id"] == 101)

    assert boundary["historical_wr_pct"] == pytest.approx(WR_THRESHOLD * 100)
    assert boundary["filter_pass"] == "yes"
    assert below["prior_cohort_n"] == MIN_COHORT_N + 1
    assert below["filter_pass"] == "yes"

    # Add one loss after the boundary and verify a genuinely sub-threshold
    # cohort is rejected on the next signal.
    loss, regime = _position(102, 1002, status="sl")
    positions.append(loss)
    regimes[102] = regime
    next_signal, regime = _position(103, 1003)
    positions.append(next_signal)
    regimes[103] = regime
    final = build_filter_decisions(positions, regimes)[-1]
    assert final["historical_wr_pct"] < WR_THRESHOLD * 100
    assert final["filter_pass"] == "no"
    assert final["filter_reason"] == "below_threshold"


def test_cohort_and_direction_are_isolated():
    positions = []
    regimes = {}
    for index in range(MIN_COHORT_N):
        position, regime = _position(index + 1, index + 1, status="tp")
        positions.append(position)
        regimes[index + 1] = regime

    different_regime, regime_row = _position(
        100,
        1000,
        regime="bear",
        status="tp",
    )
    positions.append(different_regime)
    regimes[100] = regime_row
    different_direction, regime_row = _position(
        101,
        1001,
        direction="SHORT",
        status="tp",
    )
    positions.append(different_direction)
    regimes[101] = regime_row

    decisions = build_filter_decisions(positions, regimes)
    bear = next(row for row in decisions if row["id"] == 100)
    short = next(row for row in decisions if row["id"] == 101)

    assert bear["prior_cohort_n"] == 0
    assert bear["filter_reason"] == "no_history"
    assert short["prior_cohort_n"] == 0
    assert short["filter_reason"] == "no_history"


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    input_dir = tmp_path / "outcome_trailing_stop"
    regime_dir = tmp_path / "trend_regime_analysis"
    input_dir.mkdir()
    regime_dir.mkdir()
    (input_dir / "coverage.json").write_text(
        json.dumps({"generated_utc": "2026-08-26T06:09:56+00:00", "steps_pct": [2.0]}),
        encoding="utf-8",
    )
    with (input_dir / "trailing_rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "strategy",
                "symbol",
                "direction",
                "is_shadow",
                "step_pct",
                "range_pct",
                "baseline_r",
                "baseline_outcome",
                "alt_r",
                "alt_outcome",
                "trail_ts",
                "trail_price",
                "coverage_error",
                "range_bucket",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "id": 1,
                "strategy": "overheated_24h",
                "symbol": "TESTUSDT",
                "direction": "LONG",
                "is_shadow": 0,
                "step_pct": 2.0,
                "range_pct": 10,
                "baseline_r": 1.6,
                "baseline_outcome": "tp",
                "alt_r": 1.6,
                "alt_outcome": "tp",
                "trail_ts": "",
                "trail_price": "",
                "coverage_error": "",
                "range_bucket": "low",
            }
        )
    with (regime_dir / "signal_regimes.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "trend_regime", "regime_reason"],
        )
        writer.writeheader()
        writer.writerow(
            {"id": 1, "trend_regime": "bull", "regime_reason": "close_vs_ema50"}
        )

    db_path = tmp_path / "alerts.db"
    connection = sqlite3.connect(db_path)
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
            alert_type TEXT NOT NULL,
            is_shadow INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        INSERT INTO demo_positions
        (id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
         status, ts_close, exit_price, alert_type, is_shadow)
        VALUES (1, 100, 'TESTUSDT', 'LONG', 100, 90, 120, 'tp', 110, 120,
                'overheated_24h', 0)
        """
    )
    # This later row must not enter the frozen analysis.
    connection.execute(
        """
        INSERT INTO demo_positions
        (id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
         status, ts_close, exit_price, alert_type, is_shadow)
        VALUES (2, 200, 'NEWUSDT', 'LONG', 100, 90, 120, 'tp', 210, 120,
                'overheated_24h', 0)
        """
    )
    connection.commit()
    connection.close()
    return db_path, input_dir, regime_dir


def test_run_is_frozen_and_read_only(tmp_path):
    db_path, input_dir, regime_dir = _write_fixture(tmp_path)
    marker_before = db_path.read_bytes()
    output_dir = tmp_path / "wr35"

    result = run_analysis(db_path, input_dir, output_dir)

    assert result["coverage"]["frozen_target_ids"] == 1
    assert result["coverage"]["positions_loaded"] == 1
    assert result["coverage"]["signals_passed"] == 0
    assert result["coverage"]["signals_excluded"] == 1
    assert "2026-08-26" in (output_dir / "analysis.md").read_text()
    with (output_dir / "signal_filter_decisions.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["id"] for row in rows] == ["1"]
    assert db_path.read_bytes() == marker_before