from historical_liquidation_pattern_sensitivity import (
    BASELINE_THRESHOLD_ID,
    THRESHOLD_GRID,
    build_sensitivity,
)


def _event(
    symbol,
    *,
    cohort="primary",
    pump_ts=1,
    liq=200_000.0,
    hourly=1_000_000.0,
    outcome="not_reached",
    reason="long_liquidation_threshold_not_met",
):
    return {
        "symbol": symbol,
        "cohort": cohort,
        "pump_ts": pump_ts,
        "pump_utc": "2026-01-01T00:00:00+00:00",
        "pump_episode_end_ts": pump_ts,
        "support": 90.0,
        "pump_high": 120.0,
        "correction_ts": pump_ts + 1,
        "correction_utc": "2026-01-01T00:15:00+00:00",
        "liq_window_start_ts": pump_ts + 2,
        "liq_window_end_ts": pump_ts + 3602,
        "long_liq_notional_usd": liq,
        "hour_futures_notional_usd": hourly,
        "liq_threshold_usd": 100_000.0,
        "flow_ts": None,
        "flow_utc": None,
        "flow_notional_usd": None,
        "flow_baseline_median_usd": None,
        "flow_threshold_usd": None,
        "outcome_ts": None,
        "outcome_utc": None,
        "support_retest_ts": None,
        "support_retest_utc": None,
        "outcome": outcome,
        "reason": reason,
    }


def _report(events):
    return {
        "production_changes": False,
        "generated_utc": "2026-08-31T00:00:00+00:00",
        "config": {
            "control_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        },
        "coverage": [],
        "events": events,
    }


def test_fixed_grid_is_nested_and_baseline_is_explicit():
    assert [
        (spec["min_usd"], spec["hourly_fraction"]) for spec in THRESHOLD_GRID
    ] == [
        (25_000.0, 0.005),
        (50_000.0, 0.01),
        (100_000.0, 0.02),
        (150_000.0, 0.03),
    ]
    assert THRESHOLD_GRID[2]["id"] == BASELINE_THRESHOLD_ID


def test_replay_preserves_baseline_and_uses_adjacent_incremental_bands():
    events = [
        _event("AAAUSDT", pump_ts=1, outcome="success_continuation", reason="large_5m_flow_found"),
        _event("AAAUSDT", pump_ts=2, liq=40_000.0),
        _event("AAAUSDT", pump_ts=3, liq=70_000.0),
        _event("AAAUSDT", pump_ts=4, liq=110_000.0, outcome="failure_breakdown", reason="closed_below_flush_low"),
        _event("AAAUSDT", pump_ts=5, liq=200_000.0, outcome="no_outcome_in_window", reason="no_terminal_outcome_in_24h"),
        _event(
            "BTCUSDT",
            cohort="control",
            pump_ts=6,
            outcome="success_continuation",
            reason="large_5m_flow_found",
        ),
    ]
    replays = {
        ("AAAUSDT", 2, 3, 2): {
            "flow_ts": 10,
            "flow_utc": "2026-01-01T00:00:10+00:00",
            "flow_notional_usd": 10_000,
            "flow_baseline_median_usd": 1_000,
            "flow_threshold_usd": 3_000,
            "outcome_ts": 20,
            "outcome_utc": "2026-01-01T00:00:20+00:00",
            "support_retest_ts": None,
            "support_retest_utc": None,
            "outcome": "success_continuation",
            "reason": "continued_above_flow_high",
        },
        ("AAAUSDT", 3, 4, 3): {
            "flow_ts": 10,
            "flow_utc": "2026-01-01T00:00:10+00:00",
            "flow_notional_usd": 10_000,
            "flow_baseline_median_usd": 1_000,
            "flow_threshold_usd": 3_000,
            "outcome_ts": 20,
            "outcome_utc": "2026-01-01T00:00:20+00:00",
            "support_retest_ts": None,
            "support_retest_utc": None,
            "outcome": "success_continuation",
            "reason": "continued_above_flow_high",
        },
    }

    result = build_sensitivity(_report(events), replays=replays)
    cumulative = {
        (row["threshold_id"], row["cohort"]): row
        for row in result["cumulative"]
    }

    baseline = cumulative[(BASELINE_THRESHOLD_ID, "primary")]
    assert baseline["resolved_n"] == 2
    assert baseline["success_continuation_n"] == 1
    assert baseline["failure_n"] == 1
    assert baseline["no_outcome_n"] == 1
    assert baseline["success_rate"] == 1 / 2

    soft = cumulative[("soft_25k_0_5pct", "primary")]
    assert soft["resolved_n"] == 4
    assert soft["success_continuation_n"] == 3
    assert soft["failure_n"] == 1
    assert soft["no_outcome_n"] == 1
    assert soft["success_rate"] == 3 / 4

    bands = {
        (row["threshold_id"], row["stricter_threshold_id"], row["cohort"]): row
        for row in result["incremental"]
    }
    first_band = bands[("soft_25k_0_5pct", "soft_50k_1pct", "primary")]
    assert first_band["event_rows"] == 1
    assert first_band["resolved_n"] == 1
    assert first_band["success_n"] == 1
    second_band = bands[("soft_50k_1pct", BASELINE_THRESHOLD_ID, "primary")]
    assert second_band["event_rows"] == 1
    assert second_band["resolved_n"] == 1

    assert result["global_decision"] == "descriptive_only_no_threshold_optimization"


def test_incomplete_replay_is_fail_closed_and_not_resolved():
    event = _event("AAAUSDT", pump_ts=10, liq=40_000.0)
    replay_key = ("AAAUSDT", 10, 11, 10)
    result = build_sensitivity(
        _report([event]),
        replays={
            replay_key: {
                "flow_ts": None,
                "flow_utc": None,
                "flow_notional_usd": None,
                "flow_baseline_median_usd": None,
                "flow_threshold_usd": None,
                "outcome_ts": None,
                "outcome_utc": None,
                "support_retest_ts": None,
                "support_retest_utc": None,
                "outcome": "not_reached",
                "reason": "missing_5m_coverage:candle_fetch_error",
            }
        },
    )
    soft = next(
        row
        for row in result["cumulative"]
        if row["threshold_id"] == "soft_25k_0_5pct"
        and row["cohort"] == "primary"
    )
    assert soft["event_rows"] == 1
    assert soft["resolved_n"] == 0
    assert soft["unresolved_precondition_n"] == 1
    assert soft["success_rate"] is None