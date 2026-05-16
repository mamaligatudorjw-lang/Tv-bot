"""
check_volume_surge_crsi must not re-send the same symbol within one cycle.

The run_checks loop calls check_volume_surge_crsi twice on hourly refresh
ticks (once against the stale ranking, once against the freshly-refreshed
one). The per-symbol 4h cooldown in state['last_vol_surge_alerted'] is what
prevents duplicates. This test exercises exactly that contract: two back-
to-back invocations with the same candidate must yield exactly one delivered
alert.
"""
import app


def _reset_cooldown():
    with app.state_lock:
        app.state["last_vol_surge_alerted"] = {}
        app.state["atr_4h"] = {}


def test_double_pass_deduplicates(monkeypatch):
    sym = "FOOUSDT"
    _reset_cooldown()

    # One candidate, pct=400% >= VOLUME_SURGE_PCT(300%).
    with app.state_lock:
        app.state["volume_ranking"] = [(sym, 1_000_000.0, 5_000_000.0, 400.0)]
        app.state["volume_ranking_updated"] = 0  # ancient but irrelevant

    tickers = {
        sym: {"lastPrice": "1.0", "priceChangePercent": "5.0"},
        "BTCUSDT": {"lastPrice": "60000", "priceChangePercent": "1.0"},
    }

    # CRSI=80 → SHORT branch. Daily closes just need to be non-empty since
    # we also stub _calculate_crsi.
    monkeypatch.setattr(app, "_fetch_daily_closes_for_crsi",
                        lambda _s: [1.0] * 120)
    monkeypatch.setattr(app, "_calculate_crsi", lambda _closes: 80.0)

    calls = []

    def fake_send(symbol, kind, rec, price, body, score):
        calls.append({"symbol": symbol, "kind": kind, "rec": rec})
        return True, 1  # (delivered, alert_id)

    monkeypatch.setattr(app, "send_alert_with_log", fake_send)

    # First pass: should send.
    n1 = app.check_volume_surge_crsi(tickers)
    # Second pass in the same cycle: cooldown blocks it.
    n2 = app.check_volume_surge_crsi(tickers)

    assert n1 == 1, "first pass should have sent one alert"
    assert n2 == 0, "second pass must be blocked by per-symbol cooldown"
    assert len(calls) == 1
    assert calls[0]["symbol"] == sym
    assert calls[0]["kind"] == "volume_surge_short"
    assert calls[0]["rec"] == "SHORT"
    # The cooldown marker must be populated after a delivered send — this is
    # what blocks the second pass. Asserting it directly proves the dedup
    # mechanism, not just the observable outcome.
    with app.state_lock:
        assert sym in app.state["last_vol_surge_alerted"]
        assert app.state["last_vol_surge_alerted"][sym] > 0


def test_below_threshold_does_not_alert(monkeypatch):
    """Sanity-check: pct < VOLUME_SURGE_PCT short-circuits the loop."""
    _reset_cooldown()
    with app.state_lock:
        # pct=100% well below the 300% threshold.
        app.state["volume_ranking"] = [("BARUSDT", 1_000_000.0, 2_000_000.0, 100.0)]
        app.state["volume_ranking_updated"] = 0

    tickers = {"BARUSDT": {"lastPrice": "1.0", "priceChangePercent": "5.0"}}

    calls = []
    monkeypatch.setattr(app, "send_alert_with_log",
                        lambda *a, **kw: (calls.append(a), (True, 1))[1])
    # These should never be reached, but stub defensively.
    monkeypatch.setattr(app, "_fetch_daily_closes_for_crsi",
                        lambda _s: [1.0] * 120)
    monkeypatch.setattr(app, "_calculate_crsi", lambda _c: 80.0)

    assert app.check_volume_surge_crsi(tickers) == 0
    assert calls == []
