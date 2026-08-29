import csv
import json
import sqlite3
from pathlib import Path

import pytest

from partial_tp50_trailing import (
    STEPS,
    frozen_positions,
    simulate_partial,
)


def _row(*, status="tp", direction="LONG"):
    return {
        "id": 1,
        "ts_open": 1,
        "symbol": "TESTUSDT",
        "direction": direction,
        "entry_price": 100.0,
        "sl_price": 95.0,
        "tp_price": 110.0,
        "status": status,
        "ts_close": 500,
        "cutoff_ts": 1800,
    }


def _candle(ts, high, low):
    return {"t": ts, "h": high, "l": low, "o": 100.0, "c": 100.0, "v": 1.0}


def test_tp_floor_closes_remainder_at_tp():
    result = simulate_partial(
        _row(),
        [
            _candle(300, 111, 108),  # TP trigger; this candle is not reused.
            _candle(600, 112, 109),  # Existing floor at 110 is hit.
        ],
        2.0,
    )

    assert result["partial_branch"] == "tp_branch"
    assert result["outcome"] == "partial_tp_floor"
    assert result["trail_price"] == pytest.approx(110.0)
    assert result["second_half_r"] == pytest.approx(2.0)
    assert result["total_r"] == pytest.approx(2.0)


def test_trailing_remainder_cannot_exit_below_tp_floor():
    result = simulate_partial(
        _row(),
        [
            _candle(300, 111, 108),  # TP trigger.
            _candle(600, 130, 111),  # 10% stop becomes 117, above TP.
            _candle(900, 121, 116),  # Stop at 117 is hit.
        ],
        10.0,
    )

    assert result["outcome"] == "partial_trail_stop"
    assert result["trail_price"] == pytest.approx(117.0)
    assert result["trail_price"] >= 110.0
    assert result["second_half_r"] == pytest.approx(3.4)
    assert result["total_r"] == pytest.approx(2.7)


def test_without_tp_partial_branch_is_not_activated():
    result = simulate_partial(
        _row(status="sl"),
        [_candle(300, 130, 90), _candle(600, 130, 90)],
        2.0,
    )

    assert result["partial_branch"] == "not_activated"
    assert result["outcome"] == "baseline_sl"
    assert result["tp_reached"] is False
    assert result["total_r"] == pytest.approx(-1.0)
    assert result["tp_trigger_ts"] == ""


def _write_frozen_fixture(tmp_path: Path):
    source_dir = tmp_path / "outcome_trailing_stop"
    source_dir.mkdir()
    (source_dir / "coverage.json").write_text(
        json.dumps({"generated_utc": "2026-08-26T06:09:56+00:00"}),
        encoding="utf-8",
    )
    with (source_dir / "trailing_rows.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "strategy", "step_pct", "baseline_r", "baseline_outcome"],
        )
        writer.writeheader()
        for step in STEPS:
            writer.writerow(
                {
                    "id": 1,
                    "strategy": "overheated_24h",
                    "step_pct": step,
                    "baseline_r": 2.0,
                    "baseline_outcome": "tp",
                }
            )

    db_path = tmp_path / "alerts.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY,
            ts_open INTEGER,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            sl_price REAL,
            tp_price REAL,
            status TEXT,
            ts_close INTEGER,
            exit_price REAL,
            alert_type TEXT,
            is_shadow INTEGER
        )
        """
    )
    connection.execute(
        """
        INSERT INTO demo_positions
        VALUES (1, 1, 'TESTUSDT', 'LONG', 100, 95, 110, 'tp', 500, 110,
                'overheated_24h', 0)
        """
    )
    connection.commit()
    connection.close()
    return db_path, source_dir


def test_frozen_position_loader_does_not_modify_sqlite(tmp_path):
    db_path, source_dir = _write_frozen_fixture(tmp_path)
    before = db_path.read_bytes()

    positions, coverage, _ = frozen_positions(db_path, source_dir)

    assert len(positions) == 1
    assert coverage["frozen_target_ids"] == 1
    assert db_path.read_bytes() == before
    assert not (tmp_path / "alerts.db-wal").exists()
    assert not (tmp_path / "alerts.db-journal").exists()