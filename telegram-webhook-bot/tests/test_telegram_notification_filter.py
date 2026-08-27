import sqlite3

import app


def _alerts_db():
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
    return conn


def _stub_common_alert_dependencies(monkeypatch, conn):
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(
        app, "_cycle_side_effect_allowed", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(app, "is_hidden", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app, "SHADOW_ONLY_MODE", False)
    monkeypatch.setattr(app, "ALERT_TYPE_SHADOW_ONLY", {})
    monkeypatch.setattr(app, "MIN_SCORE_LONG_BY_TYPE", {})
    monkeypatch.setattr(app, "MAX_SCORE_LONG_BY_TYPE", {})
    monkeypatch.setattr(app, "MIN_SCORE_BY_TYPE", {})
    monkeypatch.setattr(app, "MIN_ALERT_SCORE", 0)
    monkeypatch.setattr(
        app, "_get_btc_regime_enrichment_label", lambda *_args: ""
    )
    monkeypatch.setattr(app, "get_regime_label", lambda *_args: (True, ""))
    monkeypatch.setattr(app, "_coin_trend_label", lambda *_args: "")
    monkeypatch.setattr(app, "_get_signal_edge_label", lambda *_args: "")
    monkeypatch.setattr(app, "_auto_start_monitor", lambda *_args: None)
    monkeypatch.setattr(app, "_GEMINI_AI_COMMENTARY", False)


def test_allowlisted_strategy_is_delivered_without_format_changes(monkeypatch):
    conn = _alerts_db()
    _stub_common_alert_dependencies(monkeypatch, conn)
    monkeypatch.setattr(
        app, "TELEGRAM_NOTIFICATION_STRATEGIES", {"ema_cross_confirmed"}
    )
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    delivered, alert_id = app.send_alert_with_log(
        "AAAUSDT",
        "ema_cross_confirmed",
        "LONG",
        100.0,
        "signal body",
        score=60,
    )

    assert delivered is True
    assert alert_id == 1
    assert sent == ["signal body"]
    assert conn.execute(
        "SELECT alert_type FROM alerts WHERE id=1"
    ).fetchone() == ("ema_cross_confirmed",)


def test_filtered_strategy_is_logged_but_not_sent(monkeypatch):
    conn = _alerts_db()
    _stub_common_alert_dependencies(monkeypatch, conn)
    monkeypatch.setattr(
        app, "TELEGRAM_NOTIFICATION_STRATEGIES", {"ema_cross_confirmed"}
    )
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    delivered, alert_id = app.send_alert_with_log(
        "BBB usdt",
        "streak_1h",
        "LONG",
        100.0,
        "hidden strategy body",
        score=60,
    )

    assert delivered is True
    assert alert_id == 1
    assert sent == []
    assert conn.execute(
        "SELECT symbol, alert_type FROM alerts WHERE id=1"
    ).fetchone() == ("BBB usdt", "streak_1h")


def test_filtered_shadow_position_and_forward_tracker_still_run(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_open INTEGER, symbol TEXT, direction TEXT,
            entry_price REAL, sl_price REAL, tp_price REAL,
            size_usd REAL, status TEXT, is_shadow INTEGER,
            shadow_reason TEXT, alert_type TEXT, is_top INTEGER,
            repeat_num INTEGER, rsi_at_signal REAL, signal_price REAL
        )
        """
    )
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(
        app, "_cycle_side_effect_allowed", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app, "TELEGRAM_NOTIFICATION_STRATEGIES", {"ema_cross_confirmed"}
    )
    monkeypatch.setattr(app, "create_tracker", lambda *_args, **_kwargs: False)
    tracked = []
    monkeypatch.setattr(
        app,
        "track_forward_tp_vs_sl_position",
        lambda *_args, **kwargs: tracked.append(kwargs) or False,
    )
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    app._demo_open_position(
        "CCCUSDT",
        "LONG",
        100.0,
        97.0,
        106.0,
        is_shadow=True,
        shadow_reason="test_filter",
        alert_type="overheated_24h",
        notify_body="shadow body",
    )

    row = conn.execute(
        "SELECT symbol, alert_type, is_shadow FROM demo_positions"
    ).fetchone()
    assert row == ("CCCUSDT", "overheated_24h", 1)
    assert len(tracked) == 1
    assert tracked[0]["alert_type"] == "overheated_24h"
    assert sent == []