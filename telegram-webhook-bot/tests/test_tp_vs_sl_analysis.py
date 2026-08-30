import json
import random

from tp_vs_sl_analysis import (
    MIN_GROUP_N,
    build_report,
    compare_feature,
    enrich_rows,
    historical_features,
    metrics,
    parse_runtime_log,
)


def _row(outcome, row_id=1, direction="LONG", strategy="overheated_early"):
    entry = 100.0
    stop = 95.0
    target = 110.0
    exit_price = target if outcome == "tp" else stop
    return {
        "id": row_id,
        "ts_open": 1_780_000_000 + row_id,
        "symbol": f"COIN{row_id}USDT",
        "direction": direction,
        "entry_price": entry,
        "sl_price": stop,
        "tp_price": target,
        "status": outcome,
        "ts_close": 1_780_000_100 + row_id,
        "exit_price": exit_price,
        "alert_type": strategy,
        "is_shadow": 1,
        "rsi_at_signal": 55.0,
        "signal_price": entry,
    }


def test_runtime_log_parser_keeps_explicit_rounded_provenance_fields(tmp_path):
    log = tmp_path / "bot_debug.log"
    log.write_text(
        "\n".join(
            [
                "2026-08-27 10:00:00,000 [INFO] "
                "EMA cross shadow BTCUSDT LONG: price=100 gap=0.423% sl=95 tp=110",
                "2026-08-27 10:00:01,000 [INFO] "
                "overheated_early PRECHECK ETHUSDT: pct24=18.5% threshold=12.0% "
                "rsi=66.0 range=[55,70) btc_pct24=1.0%",
                "2026-08-27 10:00:02,000 [INFO] "
                "cont_confirmed: ema_cross_confirmed SOLUSDT SHORT confirmed#2 "
                "@100 signal=101 vol=2.5x tp_mult=1.5x age=42min",
            ]
        ),
        encoding="utf-8",
    )

    events = parse_runtime_log(log)

    assert events[("ema_cross", "BTCUSDT", "LONG")][0]["ema_gap_pct_log"] == 0.423
    assert events[("overheated_early", "ETHUSDT", "LONG")][0]["overheated_rsi_log"] == 66.0
    confirmed = events[("ema_cross_confirmed", "SOLUSDT", "SHORT")][0]
    assert confirmed["confirmation_volume_ratio_log"] == 2.5
    assert confirmed["confirmation_number_log"] == 2
    assert confirmed["confirmation_age_min_log"] == 42


def test_enrich_rows_computes_directional_r_and_barrier_geometry():
    rows = enrich_rows([_row("tp"), _row("sl", row_id=2)], {})

    assert rows[0]["result_r"] == 2.0
    assert rows[1]["result_r"] == -1.0
    assert rows[0]["risk_pct"] == 5.0
    assert rows[0]["reward_risk"] == 2.0
    assert rows[0]["entry_vs_signal_pct"] == 0.0
    assert rows[0]["rsi_at_signal"] == 55.0
    assert metrics(rows)["resolved_wr_pct"] == 50.0
    assert metrics(rows)["avg_r"] == 0.5


def test_insufficient_outcome_group_is_preserved_without_candidate():
    rows = [_row("tp", row_id=i) for i in range(1, MIN_GROUP_N)]
    rows += [_row("sl", row_id=i) for i in range(100, 100 + MIN_GROUP_N)]

    # Reduce TP below the guardrail while retaining a valid resolved cohort.
    rows = rows[: MIN_GROUP_N - 1] + [
        _row("sl", row_id=200 + i) for i in range(MIN_GROUP_N)
    ]
    report = build_report(rows, {})
    cohort = report["strategies"]["overheated_early"]["overall"]

    assert cohort["tp_first"] == MIN_GROUP_N - 1
    assert cohort["sl_first"] == MIN_GROUP_N
    assert cohort["comparison_allowed"] is False
    assert cohort["candidate"] is None


def test_direction_summary_keeps_missing_short_cohorts_explicit():
    rows = [
        _row("tp", row_id=i, strategy="overheated_confirmed")
        for i in range(1, MIN_GROUP_N + 1)
    ]
    rows += [
        _row("sl", row_id=100 + i, strategy="overheated_confirmed")
        for i in range(MIN_GROUP_N)
    ]

    report = build_report(rows, {})
    strategy = report["strategies"]["overheated_confirmed"]

    assert set(strategy) >= {"overall", "LONG", "SHORT"}
    assert strategy["LONG"]["comparison_allowed"] is True
    assert strategy["SHORT"]["metrics"]["total_n"] == 0
    assert strategy["SHORT"]["comparison_status"] == "INSUFFICIENT_TP_OR_SL"
    assert strategy["SHORT"]["comparison_reason"] == (
        "Requires TP>= 20 and SL>= 20; observed TP=0, SL=0."
    )


def test_candidate_is_in_sample_and_not_silently_promoted_to_telegram():
    rows = []
    for i in range(MIN_GROUP_N):
        row = _row("tp", row_id=i + 1, strategy="ema_cross")
        row["entry_price"] = 100.0 + i * 0.01
        row["sl_price"] = row["entry_price"] - 1.0
        row["tp_price"] = row["entry_price"] + 2.0
        row["exit_price"] = row["tp_price"]
        rows.append(row)
    for i in range(MIN_GROUP_N):
        row = _row("sl", row_id=100 + i, strategy="ema_cross")
        row["entry_price"] = 100.0 + i * 0.01
        row["sl_price"] = row["entry_price"] - 5.0
        row["tp_price"] = row["entry_price"] + 10.0
        row["exit_price"] = row["sl_price"]
        rows.append(row)

    report = build_report(rows, {})
    cohort = report["strategies"]["ema_cross"]["overall"]

    assert cohort["candidate"] is not None
    assert "in_sample" in json.dumps(cohort["candidate"])
    assert "telegram" not in report


def _candle(ts, close, volume=100.0, high=None, low=None):
    return {
        "t": ts,
        "o": close,
        "h": high if high is not None else close + 1.0,
        "l": low if low is not None else close - 1.0,
        "c": close,
        "v": volume,
    }


def test_historical_features_use_only_completed_contiguous_candles():
    hour = 3600
    candles = [
        _candle(1_700_000_000 + hour * index, 100.0 + index, 100.0 + index)
        for index in range(30)
    ]
    signal_ts = candles[-1]["t"] + hour + 1

    features = historical_features(candles, signal_ts, "LONG")

    assert features["historical_feature_status"] == "ok"
    assert features["price_return_1h_pct"] is not None
    assert features["price_return_2h_pct"] > features["price_return_1h_pct"]
    assert features["realized_vol_2h_pct"] is not None
    assert features["volume_ratio_1h_vs_24h"] is not None
    assert features["distance_to_recent_low_24h_pct"] == pytest.approx(
        (129.0 - 105.0) / 105.0 * 100.0
    )
    assert features["distance_to_recent_high_24h_pct"] == pytest.approx(
        (130.0 - 129.0) / 130.0 * 100.0
    )

    # The last candle is still forming at this timestamp and must not leak.
    before_last = historical_features(candles, candles[-1]["t"] + 1, "LONG")
    assert before_last["historical_feature_status"] == "ok"
    assert before_last["historical_last_candle_ts"] == candles[-2]["t"]


def test_historical_features_mark_gapped_windows_unavailable():
    hour = 3600
    candles = [
        _candle(1_700_000_000, 100.0),
        _candle(1_700_000_000 + hour, 101.0),
        _candle(1_700_000_000 + hour * 3, 103.0),
        _candle(1_700_000_000 + hour * 5, 105.0),
    ]

    features = historical_features(
        candles, 1_700_000_000 + hour * 6 + 1, "SHORT"
    )

    assert features["historical_feature_status"] == "ok"
    assert features["price_return_1h_pct"] is not None
    assert features["price_return_2h_pct"] is None
    assert features["price_return_4h_pct"] is None


def test_feature_comparison_reports_bootstrap_ci_for_mean_difference():
    tp_rows = [_row("tp", row_id=i) for i in range(1, MIN_GROUP_N + 1)]
    sl_rows = [_row("sl", row_id=100 + i) for i in range(MIN_GROUP_N)]
    for index, row in enumerate(tp_rows):
        row["rsi_at_signal"] = 60.0 + index
    for index, row in enumerate(sl_rows):
        row["rsi_at_signal"] = 40.0 + index

    comparison = compare_feature(
        tp_rows, sl_rows, "rsi_at_signal", random.Random(42)
    )

    assert comparison["comparison_allowed"] is True
    assert comparison["mean_diff_tp_minus_sl"] == 20.0
    assert comparison["bootstrap_mean_diff_95ci"][0] is not None
    assert comparison["bootstrap_mean_diff_95ci"][1] is not None
    assert comparison["bootstrap_mean_diff_95ci"][0] <= 20.0
    assert comparison["bootstrap_mean_diff_95ci"][1] >= 20.0