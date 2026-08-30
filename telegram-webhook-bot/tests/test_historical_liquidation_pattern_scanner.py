import json

import pytest

from historical_liquidation_pattern_scanner import (
    Candle,
    FetchStatus,
    GateClient,
    PreflightError,
    classify_support_retest,
    detect_pump_episodes,
    run_scan,
    scan_symbol_events,
    validate_sign_preflight,
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
    assert episodes[0].support == 100.0
    assert episodes[0].pump_high == 130.0


def test_sign_preflight_accepts_manual_long_mapping():
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
            {
                "symbol": "BTCUSDT",
                "ts": 100,
                "expected_side": "long",
                "expected_size_sign": -1,
            }
        ],
        fetch,
    )

    assert result.ok is True
    assert result.sign_to_side == {"-1": "long"}
    assert result.examples[0]["observed_size_sign"] == -1


def test_sign_preflight_fails_on_ambiguous_or_wrong_evidence():
    def ambiguous(_symbol, _start, _end):
        return [
            {"size": "-1", "fill_price": "100"},
            {"size": "1", "fill_price": "100"},
        ], FetchStatus("complete", count=2)

    with pytest.raises(PreflightError, match="dominant sign"):
        validate_sign_preflight(
            [
                {
                    "symbol": "BTCUSDT",
                    "ts": 100,
                    "expected_side": "long",
                    "expected_size_sign": -1,
                }
            ],
            ambiguous,
        )

    def wrong_sign(_symbol, _start, _end):
        return [{"size": "5", "fill_price": "100"}], FetchStatus("complete", count=1)

    with pytest.raises(PreflightError, match="observed sign"):
        validate_sign_preflight(
            [
                {
                    "symbol": "BTCUSDT",
                    "ts": 100,
                    "expected_side": "long",
                    "expected_size_sign": -1,
                }
            ],
            wrong_sign,
        )


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
        start_ts=100,
        end_ts=200,
    )
    assert (success, success_ts) == ("success", 100)
    assert "closed_above" in reason

    failure, failure_ts, _ = classify_support_retest(
        [_candle(100, high=102, low=99, close=98)],
        support=100,
        start_ts=100,
        end_ts=200,
    )
    assert (failure, failure_ts) == ("failure", None)


def test_failed_preflight_never_creates_scan_outputs(tmp_path):
    from historical_liquidation_pattern_scanner import write_failed_preflight

    write_failed_preflight(tmp_path, RuntimeError("ambiguous sign"))

    assert json.loads((tmp_path / "preflight.json").read_text())["ok"] is False
    assert (tmp_path / "preflight.csv").exists()
    assert not (tmp_path / "events.csv").exists()
    assert "ABORTED" in (tmp_path / "report.md").read_text()


def test_event_sequence_reaches_flow_and_support_success():
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
        _candle(flow_start, open=100, close=101, quote_notional=4000)
    )

    class Client:
        def fetch_liquidations(self, _contract, _start, _end):
            return [
                {"size": "-10000", "fill_price": "100", "time": _start}
            ], FetchStatus("complete", count=1)

    events, coverage = scan_symbol_events(
        "FOOUSDT",
        candles_15m,
        candles_5m,
        Client(),
        cohort="primary",
        size_field="size",
        long_size_sign=-1,
    )

    assert len(events) == 1
    assert events[0]["correction_ts"] == 35 * 900
    assert events[0]["flow_ts"] == flow_start
    assert events[0]["outcome"] == "success"
    assert events[0]["support_retest_ts"] == 40 * 900
    assert coverage["liquidation_status"] == "complete"
    assert coverage["flow_5m_status"] == "complete"