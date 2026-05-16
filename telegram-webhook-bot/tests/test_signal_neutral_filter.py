"""
/signal command should hide NEUTRAL recommendations.

Covers three scenarios:
  1. Mixed top-10 (3 directional + 7 neutral) → 3 numbered lines + "Скрыто
     нейтральных: 7" footer, no neutral lines anywhere in the body.
  2. All-neutral top-10 → friendly empty-state message, no numbered lines.
  3. Empty ranking → "Данные для сигналов ещё не готовы" message.
"""
import app


def _seed_ranking(symbols):
    """Populate state['volume_ranking'] with deterministic fake rows."""
    with app.state_lock:
        app.state["volume_ranking"] = [
            (sym, 1_000_000.0, 5_000_000.0, 400.0) for sym in symbols
        ]
        app.state["weekly_highs"] = {}
        app.state["monthly_highs"] = {}
        app.state["ema200_4h"] = {}


def _patch_market(monkeypatch, symbols):
    """Stub the Binance / RSI / 5m calls so the test is offline & deterministic."""
    fake_tickers = {
        sym: {"lastPrice": "1.0", "priceChangePercent": "5.0",
              "highPrice": "1.1", "lowPrice": "0.9"}
        for sym in symbols
    }
    monkeypatch.setattr(app, "get_24h_tickers", lambda _syms: fake_tickers)
    monkeypatch.setattr(app, "get_klines_rsi", lambda _s: 50.0)
    monkeypatch.setattr(app, "get_5m_signals", lambda _s: (1.0, 1.0))


def _capture_send(monkeypatch):
    sent = []

    def fake_send(chat_id, text, **_kwargs):
        sent.append({"chat_id": chat_id, "text": text})
        return True

    monkeypatch.setattr(app, "_telegram_send", fake_send)
    return sent


def test_mixed_signals_hide_neutrals(monkeypatch):
    symbols = [f"S{i}USDT" for i in range(10)]
    _seed_ranking(symbols)
    _patch_market(monkeypatch, symbols)

    # 3 directional, 7 neutral, in mixed order. handle_signal_command iterates
    # top10 in order and calls make_recommendation once per symbol → side_effect
    # list is consumed positionally.
    recs = [
        ("📊 Рекомендация: ЛОНГ", "RSI перепродан"),                 # 1: shown
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
        ("📊 Рекомендация: ШОРТ", "RSI перекуплен"),                 # 2: shown
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
        ("📊 Рекомендация: ЛОНГ", "Пробой максимума"),               # 3: shown
        ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),       # skip
    ]
    it = iter(recs)
    monkeypatch.setattr(app, "make_recommendation", lambda **_kw: next(it))

    sent = _capture_send(monkeypatch)
    app.handle_signal_command(chat_id=42)

    assert len(sent) == 1
    msg = sent[0]["text"]

    # No neutral line in body.
    assert "НЕЙТРАЛЬНО" not in msg
    # Shown rows are renumbered 1./2./3., not 1./3./9. of original positions.
    assert "1. <code>S0USDT</code>" in msg
    assert "2. <code>S2USDT</code>" in msg
    assert "3. <code>S8USDT</code>" in msg
    # Footer counts hidden ones.
    assert "Скрыто нейтральных: 7" in msg


def test_all_neutral_shows_empty_state(monkeypatch):
    symbols = [f"N{i}USDT" for i in range(10)]
    _seed_ranking(symbols)
    _patch_market(monkeypatch, symbols)

    monkeypatch.setattr(
        app, "make_recommendation",
        lambda **_kw: ("📊 Рекомендация: НЕЙТРАЛЬНО", "Нет ясных сигналов"),
    )
    sent = _capture_send(monkeypatch)
    app.handle_signal_command(chat_id=42)

    assert len(sent) == 1
    msg = sent[0]["text"]
    assert "нет направленных сигналов" in msg
    # No numbered rows when nothing is shown.
    assert "1. <code>" not in msg
    # Footer with hidden count is suppressed when shown==0.
    assert "Скрыто нейтральных" not in msg


def test_empty_ranking_shows_warmup_message(monkeypatch):
    with app.state_lock:
        app.state["volume_ranking"] = []
    sent = _capture_send(monkeypatch)
    app.handle_signal_command(chat_id=42)

    assert len(sent) == 1
    assert "Данные для сигналов ещё не готовы" in sent[0]["text"]
