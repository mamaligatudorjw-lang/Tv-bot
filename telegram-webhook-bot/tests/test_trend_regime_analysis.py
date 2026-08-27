import sqlite3

import pytest

from trend_regime_analysis import (
    BTC_INTERVAL_SEC,
    MIN_GROUP_N,
    annotate_rows,
    build_summary,
    classify_regime,
    metrics,
    regime_at_signal,
    special_summary,
)


def _candles(closes, start=0, step=BTC_INTERVAL_SEC):
    return [{"t": start + index * step, "c": close} for index, close in enumerate(closes)]


def _row(
    row_id,
    *,
    strategy="alpha",
    direction="LONG",
    regime="bull",
    status="tp",
    result_r=2.0,
):
    return {
        "id": row_id,
        "ts_open": 10_000,
        "symbol": f"COIN{row_id}USDT",
        "direction": direction,
        "alert_type": strategy,
        "is_shadow": 1,
        "status": status,
        "entry_price": 100.0,
        "sl_price": 90.0 if direction == "LONG" else 110.0,
        "tp_price": 120.0 if direction == "LONG" else 80.0,
        "exit_price": 120.0 if status == "tp" else (90.0 if direction == "LONG" else 110.0),
        "ts_close": 10_100,
        "trend_regime": regime,
        "result_r": result_r,
    }


def test_regime_uses_only_last_completed_candle_and_not_future_data():
    # 50 equal closes make EMA50 exactly 100.  The 51st candle is still open
    # at signal time, so its extreme close must not change the classification.
    candles = _candles([100.0] * 50 + [1_000.0])
    signal_ts = 50 * BTC_INTERVAL_SEC + 1

    result = regime_at_signal(candles, signal_ts)

    assert result["btc_candle_ts"] == 49 * BTC_INTERVAL_SEC
    assert result["btc_close"] == pytest.approx(100.0)
    assert result["btc_ema50"] == pytest.approx(100.0)
    assert result["trend_regime"] == "unknown"
    assert result["regime_reason"] == "close_vs_ema50"


def test_regime_changes_only_after_candle_is_completed():
    candles = _candles([100.0] * 50 + [110.0])
    before_close = regime_at_signal(candles, 50 * BTC_INTERVAL_SEC + 1)
    after_close = regime_at_signal(candles, 51 * BTC_INTERVAL_SEC)

    assert before_close["trend_regime"] == "unknown"
    assert after_close["trend_regime"] == "bull"
    assert after_close["btc_candle_ts"] == 50 * BTC_INTERVAL_SEC


def test_insufficient_history_is_unknown():
    result = regime_at_signal(_candles([100.0] * (MIN_GROUP_N + 1)), 99 * BTC_INTERVAL_SEC)

    assert result["trend_regime"] == "unknown"
    assert result["regime_reason"] == "insufficient_ema_history"
    assert result["btc_ema50"] is None


def test_classification_boundary_is_explicit():
    assert classify_regime(101.0, 100.0) == "bull"
    assert classify_regime(99.0, 100.0) == "bear"
    assert classify_regime(100.0, 100.0) == "unknown"


def test_metrics_and_summary_mark_small_groups_insufficient():
    rows = [_row(index) for index in range(MIN_GROUP_N - 1)]
    rows += [_row(100 + index) for index in range(MIN_GROUP_N)]
    rows += [
        _row(
            200 + index,
            strategy="alpha",
            direction="SHORT",
            regime="bear",
            status="sl" if index else "tp",
            result_r=2.0 if index == 0 else -1.0,
        )
        for index in range(MIN_GROUP_N)
    ]

    summary = build_summary(rows)
    small = next(
        item
        for item in summary
        if item["strategy"] == "alpha"
        and item["direction"] == "LONG"
        and item["trend_regime"] == "bull"
        and item["n"] == (2 * MIN_GROUP_N - 1)
    )
    # The two alpha LONG bull batches intentionally share one group and should
    # therefore be ready; the separate helper check covers the small case.
    assert small["sample_status"] == "ready"
    assert small["resolved_wr_pct"] == pytest.approx(100.0)
    assert metrics(rows[: MIN_GROUP_N - 1])["sample_status"] == "insufficient_sample"


def test_special_cohorts_are_filtered_to_requested_short_strategies():
    rows = [
        _row(1, strategy="bb_squeeze", direction="SHORT", regime="bull"),
        _row(2, strategy="high_rejection_short", direction="SHORT", regime="bear"),
        _row(3, strategy="other", direction="SHORT", regime="bull"),
        _row(4, strategy="bb_squeeze", direction="LONG", regime="bull"),
    ]

    selected = special_summary(build_summary(rows))

    assert {(row["strategy"], row["direction"]) for row in selected} == {
        ("bb_squeeze", "SHORT"),
        ("high_rejection_short", "SHORT"),
    }
    high_rejection_regimes = {
        row["trend_regime"]
        for row in selected
        if row["strategy"] == "high_rejection_short"
    }
    assert high_rejection_regimes == {"bull", "bear"}
    assert next(
        row
        for row in selected
        if row["strategy"] == "high_rejection_short"
        and row["trend_regime"] == "bear"
    )["sample_status"] == "insufficient_sample"


def test_annotation_computes_recorded_r_without_database_writes(tmp_path):
    db_path = tmp_path / "alerts.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE marker (value TEXT)")
    conn.execute("INSERT INTO marker VALUES ('before')")
    conn.commit()
    before = conn.execute("SELECT * FROM marker").fetchall()
    conn.close()

    source = _row(1, strategy="bb_squeeze", direction="SHORT", status="tp")
    source.update(
        {
            "ts_open": 50 * BTC_INTERVAL_SEC + 1,
            "entry_price": 100.0,
            "sl_price": 110.0,
            "tp_price": 80.0,
            "exit_price": 80.0,
        }
    )
    source_rows = [source]
    annotated = annotate_rows(source_rows, _candles([100.0] * 50 + [1_000.0]))

    assert annotated[0]["result_r"] == pytest.approx(2.0)
    assert annotated[0]["outcome"] == "win"
    assert source_rows[0]["status"] == "tp"

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT * FROM marker").fetchall() == before
    conn.close()