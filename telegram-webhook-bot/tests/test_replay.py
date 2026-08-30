import sqlite3

import pytest

import app


def _replay_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE demo_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_open INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            tp_price REAL NOT NULL,
            size_usd REAL NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'open',
            ts_close INTEGER,
            exit_price REAL,
            pnl_usd REAL,
            is_shadow INTEGER NOT NULL DEFAULT 0,
            shadow_reason TEXT,
            alert_type TEXT,
            is_top INTEGER NOT NULL DEFAULT 0,
            wick_close INTEGER NOT NULL DEFAULT 0,
            repeat_num INTEGER,
            rsi_at_signal REAL,
            signal_price REAL,
            exit_method TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE telegram_replay_delivery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            demo_position_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            ts_sent INTEGER NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 1,
            UNIQUE (demo_position_id, chat_id)
        )
        """
    )
    return conn


def _insert_position(
    conn,
    *,
    symbol,
    status,
    pnl_usd,
    is_shadow=0,
    alert_type="momentum",
    ts_open=1_700_000_000,
    ts_close=1_700_000_600,
    exit_price=105.0,
    exit_method="poll",
):
    conn.execute(
        """
        INSERT INTO demo_positions (
            ts_open, symbol, direction, entry_price, sl_price, tp_price,
            status, ts_close, exit_price, pnl_usd, is_shadow, alert_type,
            signal_price, exit_method
        ) VALUES (?, ?, 'LONG', 100.0, 95.0, 110.0, ?, ?, ?, ?, ?, ?, 100.0, ?)
        """,
        (
            ts_open,
            symbol,
            status,
            ts_close,
            exit_price,
            pnl_usd,
            is_shadow,
            alert_type,
            exit_method,
        ),
    )
    conn.commit()


def _fetch_position_row(conn, symbol):
    return conn.execute(
        """
        SELECT
            id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
            status, ts_close, exit_price, pnl_usd, is_shadow, alert_type,
            exit_method
        FROM demo_positions
        WHERE symbol = ?
        """,
        (symbol,),
    ).fetchone()


def _run_status(monkeypatch, conn, chat_id):
    sent = []
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(
        app,
        "state",
        {
            "initialized": True,
            "known_pairs": {"BTCUSDT"},
            "last_run": "2026-08-29 20:00",
            "last_run_summary": {},
            "silenced": False,
            "silenced_at": None,
        },
    )
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )
    app.handle_status_command(chat_id)
    assert len(sent) == 1
    return sent[0]


def test_replay_sends_all_positive_resolved_live_and_shadow_rows_once(monkeypatch):
    conn = _replay_db()
    _insert_position(
        conn,
        symbol="LIVEUSDT",
        status="tp",
        pnl_usd=3.5,
        alert_type="momentum",
    )
    _insert_position(
        conn,
        symbol="SHADOWUSDT",
        status="manual",
        pnl_usd=1.25,
        is_shadow=1,
        alert_type="overheated_24h",
        exit_method="manual",
    )
    _insert_position(
        conn,
        symbol="OTHERUSDT",
        status="ttl_expired",
        pnl_usd=2.0,
        is_shadow=1,
        alert_type="range_breakout_long",
        exit_method="ttl",
    )
    _insert_position(
        conn,
        symbol="LOSSUSDT",
        status="sl",
        pnl_usd=-4.0,
        is_shadow=1,
        alert_type="ema_cross_confirmed",
    )
    _insert_position(
        conn,
        symbol="OPENUSDT",
        status="open",
        pnl_usd=9.0,
        alert_type="confluence",
        ts_close=None,
    )
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(app, "REPLAY_SEND_DELAY_SEC", 0)
    monkeypatch.setattr(app, "_demo_open_position", lambda *_a, **_k: pytest.fail(
        "replay must never open a position"
    ))
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    app.handle_replay_command(123)

    replay_messages = [
        text for text in sent if "REPLAY — историческая сделка, не live" in text
    ]
    assert len(replay_messages) == 3
    assert any("LIVEUSDT" in text and "Фактический исход: <b>TP" in text
               for text in replay_messages)
    assert any("SHADOWUSDT" in text and "Фактический исход: <b>MANUAL" in text
               for text in replay_messages)
    assert any("OTHERUSDT" in text and "TTL_EXPIRED · TTL" in text
               for text in replay_messages)
    for text in replay_messages:
        assert "📍 Entry:" in text
        assert "🎯 TP:" in text
        assert "🛑 SL:" in text
        assert "💰 P&L:" in text
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log "
        "WHERE chat_id=123 AND delivered=1"
    ).fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM demo_positions").fetchone() == (5,)

    sent.clear()
    app.handle_replay_command(123)
    assert not any(
        "REPLAY — историческая сделка, не live" in text for text in sent
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log "
        "WHERE chat_id=123 AND delivered=1"
    ).fetchone() == (3,)
    conn.close()


def test_status_reports_empty_replay_backlog_without_writing(monkeypatch):
    conn = _replay_db()
    before = conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log"
    ).fetchone()[0]

    message = _run_status(monkeypatch, conn, 321)

    assert "🕰 Replay:</b> ожидают отправки: <b>0</b>" in message
    assert "Последняя успешная доставка: <code>нет отправок</code>" in message
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log"
    ).fetchone()[0] == before
    conn.close()


def test_status_reports_pending_replay_and_last_success_for_chat(monkeypatch):
    conn = _replay_db()
    _insert_position(conn, symbol="PENDINGUSDT", status="tp", pnl_usd=1.0)
    _insert_position(conn, symbol="SENTUSDT", status="manual", pnl_usd=2.0)
    sent_position_id = conn.execute(
        "SELECT id FROM demo_positions WHERE symbol='SENTUSDT'"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO telegram_replay_delivery_log
            (demo_position_id, chat_id, ts_sent, delivered)
        VALUES (?, ?, ?, 1)
        """,
        (sent_position_id, 456, 1_700_000_600),
    )
    conn.commit()

    message = _run_status(monkeypatch, conn, 456)

    assert "🕰 Replay:</b> ожидают отправки: <b>1</b>" in message
    assert "Последняя успешная доставка: <code>2023-" in message
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log"
    ).fetchone() == (1,)
    conn.close()


def test_replay_picks_up_new_profitable_closures_after_backlog(monkeypatch):
    conn = _replay_db()
    _insert_position(conn, symbol="FIRSTUSDT", status="tp", pnl_usd=1.0)
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(app, "REPLAY_SEND_DELAY_SEC", 0)
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    app.handle_replay_command(456)
    first_replay = [
        text for text in sent if "REPLAY — историческая сделка, не live" in text
    ]
    assert len(first_replay) == 1
    sent.clear()

    _insert_position(conn, symbol="SECONDUSDT", status="manual", pnl_usd=2.0)
    app.handle_replay_command(456)

    second_replay = [
        text for text in sent if "REPLAY — историческая сделка, не live" in text
    ]
    assert len(second_replay) == 1
    assert "SECONDUSDT" in second_replay[0]
    assert "FIRSTUSDT" not in second_replay[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log "
        "WHERE chat_id=456 AND delivered=1"
    ).fetchone() == (2,)
    conn.close()


def test_replay_batches_without_daily_cap(monkeypatch):
    conn = _replay_db()
    for index in range(31):
        _insert_position(
            conn,
            symbol=f"COIN{index}USDT",
            status="tp",
            pnl_usd=1.0,
            ts_open=1_700_000_000 + index,
            ts_close=1_700_000_600 + index,
        )
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(app, "REPLAY_SEND_DELAY_SEC", 0)
    sent = []
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    app.handle_replay_command(789)

    assert len([
        text for text in sent if "REPLAY — историческая сделка, не live" in text
    ]) == 25
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log "
        "WHERE chat_id=789 AND delivered=1"
    ).fetchone() == (25,)
    assert any("Осталось в backlog: <b>6</b>" in text for text in sent)

    sent.clear()
    app.handle_replay_command(789)

    assert len([
        text for text in sent if "REPLAY — историческая сделка, не live" in text
    ]) == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM telegram_replay_delivery_log "
        "WHERE chat_id=789 AND delivered=1"
    ).fetchone() == (31,)
    assert any("Backlog пуст." in text for text in sent)
    conn.close()


def test_replay_marks_trade_closed_before_strategy_fix(monkeypatch):
    conn = _replay_db()
    first_fix_ts = app.REPLAY_STRATEGY_FIXES["oversold_24h"][0][0]
    _insert_position(
        conn,
        symbol="OLDOSUSDT",
        status="tp",
        pnl_usd=1.0,
        alert_type="oversold_24h",
        ts_open=first_fix_ts + 100,
        ts_close=first_fix_ts - 1,
    )

    monkeypatch.setattr(app, "_get_db", lambda: conn)
    message = app._format_replay_message(_fetch_position_row(conn, "OLDOSUSDT"))

    assert "Сделка предшествует фиксу" in message
    assert "TP LONG 5%" in message
    assert "R:R/логика могли отличаться от текущей" in message
    conn.close()


def test_replay_does_not_mark_trade_closed_after_all_strategy_fixes(monkeypatch):
    conn = _replay_db()
    latest_fix_ts = max(
        fix_ts for fix_ts, _description in app.REPLAY_STRATEGY_FIXES["oversold_24h"]
    )
    _insert_position(
        conn,
        symbol="NEWOSUSDT",
        status="tp",
        pnl_usd=1.0,
        alert_type="oversold_24h",
        ts_open=latest_fix_ts - 100,
        ts_close=latest_fix_ts + 1,
    )

    monkeypatch.setattr(app, "_get_db", lambda: conn)
    message = app._format_replay_message(_fetch_position_row(conn, "NEWOSUSDT"))

    assert "Сделка предшествует фиксу" not in message
    conn.close()


def test_replay_unknown_strategy_has_no_fix_warning(monkeypatch):
    conn = _replay_db()
    _insert_position(
        conn,
        symbol="UNKNOWNUSDT",
        status="tp",
        pnl_usd=1.0,
        alert_type="strategy_not_in_fix_registry",
        ts_close=1,
    )

    message = app._format_replay_message(
        _fetch_position_row(conn, "UNKNOWNUSDT")
    )

    assert "Сделка предшествует фиксу" not in message
    conn.close()


def test_replay_fix_warning_uses_close_timestamp_not_open_timestamp(monkeypatch):
    conn = _replay_db()
    first_fix_ts = app.REPLAY_STRATEGY_FIXES["ema_cross"][0][0]
    _insert_position(
        conn,
        symbol="CLOSETIMEUSDT",
        status="tp",
        pnl_usd=1.0,
        alert_type="ema_cross",
        ts_open=first_fix_ts + 100,
        ts_close=first_fix_ts - 1,
    )

    message = app._format_replay_message(
        _fetch_position_row(conn, "CLOSETIMEUSDT")
    )

    assert "Сделка предшествует фиксу" in message
    assert "live ticker" in message
    conn.close()


def test_replay_marks_overheated_early_before_price_basis_fix(monkeypatch):
    conn = _replay_db()
    first_fix_ts = app.REPLAY_STRATEGY_FIXES["overheated_early"][0][0]
    _insert_position(
        conn,
        symbol="OLDEARLYUSDT",
        status="tp",
        pnl_usd=1.0,
        alert_type="overheated_early",
        ts_open=first_fix_ts - 100,
        ts_close=first_fix_ts - 1,
    )

    monkeypatch.setattr(app, "_get_db", lambda: conn)
    message = app._format_replay_message(
        _fetch_position_row(conn, "OLDEARLYUSDT")
    )

    assert "Сделка предшествует фиксу" in message
    assert "цены входа" in message
    conn.close()