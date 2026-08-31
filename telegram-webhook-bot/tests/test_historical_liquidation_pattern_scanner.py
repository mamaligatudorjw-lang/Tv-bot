import json

import pytest

from historical_liquidation_pattern_scanner import (
    Candle,
    FetchStatus,
    GateClient,
    PreflightError,
    classify_support_retest,
    detect_pump_episodes,
    flush_low_for_episode,
    load_sign_examples,
    run_scan,
    scan_symbol_events,
    validate_sign_preflight,
)
from historical_liquidation_pattern_review import (
    ReviewError,
    build_review,
    load_completed_scan_report,
)


def _candle(
    ts,
    *,
    open=100.0,
    high=100.0,
    low=100.0,
    close=100.0,
    quote_notional=1000.0,
):
    return Candle(ts, open, high, low, close, quote_notional)


def _sign_example(*, side="long", ts=100, rationale=None, sources=None):
    return {
        "symbol": "BTCUSDT",
        "ts": ts,
        "expected_side": side,
        "rationale": rationale
        or "Independent reports identify a documented long-squeeze event.",
        "sources": sources or ["https://example.com/source"],
    }


def test_adjacent_pump_candidates_are_one_episode():
    candles = [_candle(index * 900) for index in range(32)]
    candles.extend(
        [
            _candle(32 * 900, high=120.0, close=115.0),
            _candle(33 * 900, high=125.0, close=116.0),
            _candle(34 * 900, high=130.0, close=117.0),
            _candle(35 * 900, high=117.0, low=100.0, close=101.0),
        ]
    )

    episodes = detect_pump_episodes("FOOUSDT", candles)

    assert len(episodes) == 1
    assert episodes[0].first_ts == 32 * 900
    assert episodes[0].last_candidate_ts == 34 * 900
    assert episodes[0].support is None
    assert episodes[0].pump_high == 130.0


def test_sign_preflight_discovers_manual_long_mapping():
    records = [
        {
            "contract": "BTC_USDT",
            "time": 100,
            "size": "-10",
            "fill_price": "100",
        },
        {
            "contract": "BTC_USDT",
            "time": 101,
            "size": "1",
            "fill_price": "100",
        },
    ]

    def fetch(_symbol, _start, _end):
        return records, FetchStatus("complete", count=2)

    result = validate_sign_preflight(
        [
            _sign_example()
        ],
        fetch,
        created_at_ts=50,
        now_fn=lambda: 200,
    )

    assert result.ok is True
    assert result.sign_to_side == {"-1": "long"}
    assert result.examples[0]["observed_size_sign"] == -1
    assert result.reason == "inferred from 1 externally-verified events"


def test_sign_preflight_fails_on_ambiguous_or_wrong_evidence():
    def ambiguous(_symbol, _start, _end):
        return [
            {"size": "-1", "fill_price": "100"},
            {"size": "1", "fill_price": "100"},
        ], FetchStatus("complete", count=2)

    with pytest.raises(PreflightError, match="dominant sign"):
        validate_sign_preflight(
            [
                _sign_example()
            ],
            ambiguous,
            created_at_ts=50,
            now_fn=lambda: 200,
        )

    def other_observed_sign(_symbol, _start, _end):
        return [{"size": "5", "fill_price": "100"}], FetchStatus("complete", count=1)

    result = validate_sign_preflight(
        [_sign_example()],
        other_observed_sign,
        created_at_ts=50,
        now_fn=lambda: 200,
    )
    assert result.sign_to_side == {"1": "long"}


def test_sign_preflight_rejects_expected_sign_and_bad_preregistration():
    def fetch(_symbol, _start, _end):
        return [{"size": "-1", "fill_price": "100"}], FetchStatus("complete", count=1)

    with pytest.raises(PreflightError, match="expected_size_sign"):
        validate_sign_preflight(
            [{**_sign_example(), "expected_size_sign": -1}],
            fetch,
            created_at_ts=50,
            now_fn=lambda: 200,
        )

    with pytest.raises(PreflightError, match="precede"):
        validate_sign_preflight(
            [_sign_example()],
            fetch,
            created_at_ts=200,
            now_fn=lambda: 200,
        )

    with pytest.raises(PreflightError, match="substantive rationale"):
        validate_sign_preflight(
            [_sign_example(rationale="too short")],
            fetch,
            created_at_ts=50,
            now_fn=lambda: 200,
        )


def test_load_sign_examples_requires_object_metadata(tmp_path):
    path = tmp_path / "examples.json"
    path.write_text(
        json.dumps({"created_at": "1970-01-01T00:00:50Z", "examples": [_sign_example()]}),
        encoding="utf-8",
    )

    loaded = load_sign_examples(path)

    assert loaded.created_at_ts == 50
    assert loaded.examples[0]["expected_side"] == "long"


def test_liquidation_full_limit_is_split_and_not_marked_incomplete():
    calls = []

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def request(_url, *, params, **_kwargs):
        calls.append(params)
        response = Response()
        span = params["to"] - params["from"] + 1
        if span > 1800:
            response.payload = [
                {
                    "contract": "BTC_USDT",
                    "time": 100,
                    "size": "1",
                    "fill_price": "100",
                }
            ] * 1000
        else:
            response.payload = [
                {
                    "contract": "BTC_USDT",
                    "time": params["from"],
                    "size": "1",
                    "fill_price": "100",
                }
            ]
        return response

    client = GateClient(request_fn=request, sleep_fn=lambda _seconds: None)
    records, status = client.fetch_liquidations("BTC_USDT", 0, 3600)

    assert status.status == "complete"
    assert status.overflow_splits == 1
    assert len(records) == 2
    assert len(calls) == 3


def test_support_retest_requires_touch_and_close_at_support():
    success, success_ts, reason = classify_support_retest(
        [_candle(100, high=102, low=99, close=100.5)],
        support=100,
        flow_high=110,
        start_ts=100,
        end_ts=200,
    )
    assert (success, success_ts) == ("success_retest_hold", 100)
    assert "closed_above" in reason

    failure, failure_ts, _ = classify_support_retest(
        [_candle(100, high=102, low=99, close=98)],
        support=100,
        flow_high=110,
        start_ts=100,
        end_ts=200,
    )
    assert (failure, failure_ts) == ("failure_breakdown", 100)


def test_support_retest_returns_no_outcome_for_sideways_window():
    outcome, outcome_ts, reason = classify_support_retest(
        [_candle(100, high=105, low=101, close=103)],
        support=100,
        flow_high=110,
        start_ts=100,
        end_ts=200,
    )

    assert (outcome, outcome_ts) == ("no_outcome_in_window", None)
    assert "24h" in reason


def test_support_retest_accepts_continuation_before_breakdown():
    outcome, outcome_ts, _ = classify_support_retest(
        [_candle(100, high=111, low=90, close=95)],
        support=100,
        flow_high=110,
        start_ts=100,
        end_ts=200,
    )

    assert (outcome, outcome_ts) == ("success_continuation", 100)


def test_failed_preflight_never_creates_scan_outputs(tmp_path):
    from historical_liquidation_pattern_scanner import write_failed_preflight

    write_failed_preflight(tmp_path, RuntimeError("ambiguous sign"))

    assert json.loads((tmp_path / "preflight.json").read_text())["ok"] is False
    assert (tmp_path / "preflight.csv").exists()
    assert not (tmp_path / "events.csv").exists()
    assert "ABORTED" in (tmp_path / "report.md").read_text()


@pytest.mark.parametrize(
    ("long_size_sign", "liquidation_size"),
    [(-1, "-10000"), (1, "10000")],
)
def test_event_sequence_reaches_flow_and_support_success(
    long_size_sign,
    liquidation_size,
):
    candles_15m = [_candle(index * 900) for index in range(32)]
    candles_15m.extend(
        [
            _candle(32 * 900, high=120, close=115),
            _candle(33 * 900, high=125, close=116),
            _candle(34 * 900, high=130, close=117),
            _candle(35 * 900, high=117, low=110, close=112),
            _candle(36 * 900),
            _candle(37 * 900),
            _candle(38 * 900),
            _candle(39 * 900),
            _candle(40 * 900, high=101, low=99, close=100.5),
        ]
    )
    flow_start = 39 * 900
    candles_5m = [
        _candle(ts, open=100, close=100, quote_notional=1000)
        for ts in range(flow_start - 86400, flow_start, 300)
    ]
    candles_5m.append(
        _candle(flow_start, open=100, high=101, close=101, quote_notional=4000)
    )

    class Client:
        def fetch_liquidations(self, _contract, _start, _end):
            return [
                {"size": liquidation_size, "fill_price": "100", "time": _start}
            ], FetchStatus("complete", count=1)

    events, coverage = scan_symbol_events(
        "FOOUSDT",
        candles_15m,
        candles_5m,
        Client(),
        cohort="primary",
        size_field="size",
        long_size_sign=long_size_sign,
    )

    assert len(events) == 1
    assert events[0]["correction_ts"] == 35 * 900
    assert events[0]["flow_ts"] == flow_start
    assert events[0]["outcome"] == "success_retest_hold"
    assert events[0]["support_retest_ts"] == 40 * 900
    assert events[0]["support"] == 100
    assert coverage["liquidation_status"] == "complete"
    assert coverage["flow_5m_status"] == "complete"


def test_flush_low_uses_correction_and_liquidation_window():
    correction = _candle(100, low=90)
    candles = [
        correction,
        _candle(100 + 900, low=80),
        _candle(100 + 1800, low=85),
        _candle(100 + 2700, low=82),
        _candle(100 + 3600, low=20),
    ]

    assert flush_low_for_episode(correction, candles) == 80


def test_review_requires_physical_scan_report_and_excludes_no_outcome(tmp_path):
    with pytest.raises(ReviewError, match="missing"):
        load_completed_scan_report(tmp_path)

    report = {
        "production_changes": False,
        "generated_utc": "2026-08-30T00:00:00+00:00",
        "preflight": {"ok": True},
        "events": [
            {"cohort": "primary", "outcome": "success_continuation"},
            {"cohort": "primary", "outcome": "failure_breakdown"},
            {"cohort": "primary", "outcome": "no_outcome_in_window"},
            {"cohort": "primary", "outcome": "not_reached"},
        ],
        "coverage": [],
    }

    review = build_review(report)

    primary = review["cohorts"]["primary"]
    assert primary["resolved_n"] == 2
    assert primary["no_outcome_n"] == 1
    assert primary["success_rate"] is None
    assert "no_outcome_in_window" in review["resolved_denominator_definition"]


def test_review_reports_control_rate_even_with_small_resolved_sample():
    review = build_review(
        {
            "production_changes": False,
            "generated_utc": "2026-08-30T00:00:00+00:00",
            "preflight": {"ok": True},
            "events": [
                {"cohort": "control", "outcome": "success_continuation"},
                {"cohort": "control", "outcome": "failure_breakdown"},
            ],
            "coverage": [],
        }
    )

    control = review["cohorts"]["control"]
    assert control["resolved_n"] == 2
    assert control["success_rate"] == 0.5
    assert control["sufficiency"] == "controls_any_n"


def test_review_keeps_each_control_and_success_bucket_separate():
    review = build_review(
        {
            "production_changes": False,
            "generated_utc": "2026-08-30T00:00:00+00:00",
            "config": {"control_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]},
            "preflight": {"ok": True},
            "events": [
                {
                    "symbol": "BTCUSDT",
                    "cohort": "control",
                    "outcome": "success_continuation",
                },
                {
                    "symbol": "ETHUSDT",
                    "cohort": "control",
                    "outcome": "success_retest_hold",
                },
                {
                    "symbol": "ETHUSDT",
                    "cohort": "control",
                    "outcome": "failure_breakdown",
                },
            ],
            "coverage": [
                {"symbol": "BTCUSDT", "cohort": "control"},
                {"symbol": "ETHUSDT", "cohort": "control"},
                {"symbol": "SOLUSDT", "cohort": "control"},
            ],
        }
    )

    controls = review["control_cohorts"]
    assert set(controls) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert controls["BTCUSDT"]["success_continuation_n"] == 1
    assert controls["BTCUSDT"]["success_retest_hold_n"] == 0
    assert controls["ETHUSDT"]["success_continuation_n"] == 0
    assert controls["ETHUSDT"]["success_retest_hold_n"] == 1
    assert controls["ETHUSDT"]["failure_n"] == 1
    assert controls["SOLUSDT"]["event_rows"] == 0
    assert review["coverage"]["incomplete_n"] == 0


def test_review_breaks_unresolved_rows_by_stage():
    review = build_review(
        {
            "production_changes": False,
            "generated_utc": "2026-08-30T00:00:00+00:00",
            "preflight": {"ok": True},
            "events": [
                {
                    "cohort": "primary",
                    "outcome": "not_reached",
                    "reason": "correction_not_found_in_12h",
                },
                {
                    "cohort": "primary",
                    "outcome": "not_reached",
                    "reason": "long_liquidation_threshold_not_met",
                },
                {
                    "cohort": "primary",
                    "outcome": "not_reached",
                    "reason": "missing_5m_candle:123",
                },
            ],
            "coverage": [],
        }
    )

    assert review["stage_breakdown"]["primary"] == {
        "correction_not_found_in_12h": 1,
        "liquidation_burst_stage": 1,
        "large_5m_flow_stage": 1,
    }