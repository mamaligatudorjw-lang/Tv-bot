import pytest

import app


def test_inverted_shadow_uses_same_entry_and_mirrored_barriers(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app,
        "_demo_open_position",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    app._open_inverted_bb_squeeze_shadow(
        "TESTUSDT", 100.0, 112.0, 76.0
    )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:5] == ("TESTUSDT", "LONG", 100.0, 76.0, 112.0)
    assert kwargs["is_shadow"] is True
    assert kwargs["alert_type"] == "bb_squeeze_inverted_test"
    assert kwargs["shadow_reason"] == "bb_squeeze_short_direction_inversion"
    assert kwargs.get("notify_body") is None


def test_inverted_shadow_is_explicitly_blocked_from_telegram():
    assert app._shadow_strategy_telegram_allowed(
        "bb_squeeze_inverted_test"
    ) is False


def test_short_breakout_creates_independent_inverted_row_without_cooldown_change(
    monkeypatch,
):
    candles = [[i, 100, 101, 99, 100, 10] for i in range(129)]
    candles.append([129, 100, 101, 89, 90, 20])
    calls = []
    monkeypatch.setattr(app, "_fetch_1h_ohlcv", lambda *args, **kwargs: candles)
    monkeypatch.setattr(
        app,
        "_calc_bollinger",
        lambda *args, **kwargs: (
            [100] * 130,
            [100] * 130,
            [1] * 130,
        ),
    )
    monkeypatch.setattr(
        app, "_calc_vwap_atr1h", lambda *_args, **_kwargs: (None, 10.0)
    )
    monkeypatch.setattr(
        app,
        "_demo_open_position",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    symbol = "TESTUSDT"
    before = app.state["last_bb_squeeze_alerted"].get(symbol)
    sent = app.check_bollinger_squeeze(
        {
            symbol: {
                "quoteVolume": app.MIN_VOLUME_USDT + 1,
                "priceChangePercent": 0,
                "lastPrice": 90,
            }
        },
        {symbol: 50},
    )

    assert sent == 1
    assert len(calls) == 2
    assert calls[0][0][:5] == (symbol, "SHORT", 90.0, 100.0, 70.0)
    assert calls[0][1]["alert_type"] == "bb_squeeze"
    assert calls[1][0][:5] == (symbol, "LONG", 90.0, 70.0, 100.0)
    assert calls[1][1]["alert_type"] == "bb_squeeze_inverted_test"
    assert calls[1][1]["is_shadow"] is True
    assert app.state["last_bb_squeeze_alerted"].get(symbol) != before
