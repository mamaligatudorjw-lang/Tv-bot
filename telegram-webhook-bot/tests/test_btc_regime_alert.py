import app
import sqlite3


def _snapshot(**overrides):
    result = {
        "available": True,
        "regime": "bull",
        "candle_ts": 1_760_000_000,
        "close": 100.0,
        "ema50": 95.0,
        "fetched_ts": 1_760_000_600,
        "stale": False,
        "error": "",
    }
    result.update(overrides)
    return result


def _report(**overrides):
    result = {
        "status": "ready",
        "analysis_ts": 1_760_000_000,
        "summary_index": {
            ("bb_squeeze", "SHORT", "bull"): {
                "n": 268,
                "resolved_wr_pct": 15.3,
                "avg_r": -1.0091,
                "sample_status": "ready",
            }
        },
    }
    result.update(overrides)
    return result


def test_current_btc_label_includes_completed_candle_and_snapshot_time():
    label = app._format_btc_4h_regime_label(_snapshot())

    assert "BTC 4h: <b>BULL</b>" in label
    assert "close 100 vs EMA50 95" in label
    assert "свеча закрыта" in label
    assert "срез" in label
    assert "ДАННЫЕ УСТАРЕЛИ" not in label


def test_stale_current_btc_label_is_explicit():
    label = app._format_btc_4h_regime_label(_snapshot(stale=True))

    assert "ДАННЫЕ УСТАРЕЛИ" in label


def test_unavailable_current_btc_label_is_explicit():
    label = app._format_btc_4h_regime_label(
        _snapshot(available=False, regime="unknown", close=None, ema50=None)
    )

    assert "BTC 4h: <b>Н/Д</b>" in label
    assert "завершённая свеча недоступны" in label


def test_stats_label_shows_n_wr_avg_r_and_report_snapshot():
    label = app._format_regime_stats_label(
        "bb_squeeze", "SHORT", _snapshot(), _report()
    )

    assert "n=268" in label
    assert "WR=15.30%" in label
    assert "avg R=-1.0091" in label
    assert "срез" in label
    assert "INSUFFICIENT" not in label


def test_small_stats_label_is_marked_insufficient():
    report = _report(
        summary_index={
            ("confluence", "SHORT", "bull"): {
                "n": 10,
                "resolved_wr_pct": 10.0,
                "avg_r": -0.8,
                "sample_status": "insufficient_sample",
            }
        }
    )
    label = app._format_regime_stats_label(
        "confluence", "SHORT", _snapshot(), report
    )

    assert "n=10" in label
    assert "WR=10.00%" in label
    assert "avg R=-0.8000" in label
    assert "недостаточно данных" in label
    assert "INSUFFICIENT (<20; n=10)" in label


def test_missing_stats_and_stale_report_are_not_fabricated():
    missing = app._format_regime_stats_label(
        "new_strategy", "LONG", _snapshot(), _report()
    )
    stale = app._format_regime_stats_label(
        "bb_squeeze", "SHORT", _snapshot(), _report(status="stale")
    )

    assert "WR: <b>Н/Д</b>" in missing
    assert "нет resolved-статистики" in missing
    assert "WR: <b>Н/Д</b>" in stale
    assert "отчёт устарел" in stale


def test_enrichment_helper_combines_live_and_historical_context(monkeypatch):
    monkeypatch.setattr(app, "_get_btc_4h_regime_snapshot", lambda: _snapshot())
    monkeypatch.setattr(app, "_load_regime_stats_report", lambda: _report())
    monkeypatch.setattr(
        app,
        "_get_strategy_wr_trend_label",
        lambda *_args: "📈 Тренд WR: недостаточно данных для тренда",
    )

    label = app._get_btc_regime_enrichment_label("bb_squeeze", "SHORT")

    assert label.count("\n") == 2
    assert "BTC 4h: <b>BULL</b>" in label
    assert "n=268" in label
    assert "недостаточно данных для тренда" in label


def _trend_db(statuses):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY,
            alert_type TEXT,
            direction TEXT,
            status TEXT,
            ts_close INTEGER
        )
        """
    )
    for index, status in enumerate(statuses, start=1):
        conn.execute(
            "INSERT INTO demo_positions "
            "(id, alert_type, direction, status, ts_close) VALUES (?, ?, ?, ?, ?)",
            (index, "ema_cross", "LONG", status, index),
        )
    conn.commit()
    return conn


def test_wr_trend_uses_previous_and_latest_resolved_windows(monkeypatch):
    # Oldest 20: 8 TP (40%); newest 20: 12 TP (60%).
    conn = _trend_db(["tp"] * 8 + ["sl"] * 12 + ["tp"] * 12 + ["sl"] * 8)
    monkeypatch.setattr(app, "_get_db", lambda: conn)

    label = app._get_strategy_wr_trend_label("ema_cross", "LONG")

    assert "WR: 40.0% → 60.0%" in label
    assert "+20.0 п.п. за последние 20 сделок" in label
    assert "resolved demo_positions" in label
    conn.close()


def test_wr_trend_is_explicitly_insufficient_below_40_resolved(monkeypatch):
    conn = _trend_db(["tp"] * 19 + ["sl"] * 20)
    monkeypatch.setattr(app, "_get_db", lambda: conn)

    label = app._get_strategy_wr_trend_label("ema_cross", "LONG")

    assert "недостаточно данных для тренда" in label
    assert "resolved n=39" in label
    assert "п.п." not in label
    conn.close()


def test_send_alert_with_log_appends_context_to_common_alert_path(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, symbol TEXT, alert_type TEXT, recommendation TEXT,
            price_at_alert REAL, score INTEGER,
            factor_funding_pts INTEGER, factor_lsr_pts INTEGER,
            snapshot_ts REAL, snapshot_price REAL,
            delivery_ts REAL, snapshot_age_sec REAL, snapshot_gap_pct REAL
        )
        """
    )
    conn.execute(
        "CREATE TABLE demo_positions (symbol TEXT, status TEXT, is_shadow INTEGER)"
    )
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(
        app, "_cycle_side_effect_allowed", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(app, "is_hidden", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app, "SHADOW_ONLY_MODE", False)
    monkeypatch.setattr(app, "ALERT_TYPE_SHADOW_ONLY", {})
    monkeypatch.setattr(app, "TELEGRAM_NOTIFICATION_STRATEGIES", {"bb_squeeze"})
    monkeypatch.setattr(app, "MIN_SCORE_LONG_BY_TYPE", {"bb_squeeze": 0})
    monkeypatch.setattr(app, "MAX_SCORE_LONG_BY_TYPE", {})
    monkeypatch.setattr(
        app,
        "_get_btc_regime_enrichment_label",
        lambda *_args: "📚 BTC 4h: BULL\n📊 History: n=268",
    )
    monkeypatch.setattr(app, "get_regime_label", lambda *_args: (True, ""))
    monkeypatch.setattr(app, "_coin_trend_label", lambda *_args: "")
    monkeypatch.setattr(app, "_get_signal_edge_label", lambda *_args: None)
    monkeypatch.setattr(app, "_build_alert_buttons", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app, "_get_current_price", lambda *_args: 100.0)
    monkeypatch.setattr(app, "_auto_start_monitor", lambda *_args: None)
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    delivered, alert_id = app.send_alert_with_log(
        "AAAUSDT",
        "bb_squeeze",
        "LONG",
        100.0,
        "signal body",
        score=60,
    )

    assert delivered is True
    assert alert_id == 1
    assert len(sent) == 1
    assert "signal body" in sent[0]
    assert "📚 BTC 4h: BULL" in sent[0]
    assert "n=268" in sent[0]
    conn.close()