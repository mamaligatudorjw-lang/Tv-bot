import sqlite3

import app
import bybit_demo
from bybit_demo import (
    BYBIT_DEMO_POLL_STALE_AFTER_SEC,
    active_whitelist,
    initialize_schema,
    polling_health_status,
    record_successful_poll,
)


def _db():
    conn = sqlite3.connect(":memory:")
    initialize_schema(conn)
    conn.execute(
        "CREATE TABLE alerts (ts INTEGER, symbol TEXT, alert_type TEXT, "
        "recommendation TEXT, price_at_alert REAL)"
    )
    return conn


def test_polling_stale_transitions_at_documented_threshold():
    bybit_demo._reset_poll_health_for_tests()
    try:
        record_successful_poll(now=100)
        assert polling_health_status(now=100 + BYBIT_DEMO_POLL_STALE_AFTER_SEC - 1)[
            "polling_stale"
        ] is False
        assert polling_health_status(now=100 + BYBIT_DEMO_POLL_STALE_AFTER_SEC)[
            "polling_stale"
        ] is True
    finally:
        bybit_demo._reset_poll_health_for_tests()


def test_status_snapshot_has_whitelist_and_safe_open_position_format(monkeypatch):
    conn = _db()
    conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, confirmation_level, symbol, direction,
            signal_price, entry_price, sl_price, tp_price, order_link_id,
            ts_created, status, position_size, ts_filled
        ) VALUES ('open-1', 100, 'overheated_confirmed', '1/3', 'BTCUSDT',
                  'LONG', 100, 100, 95, 110, 'link-open-1', 90, 'open', 0.5, 101)
        """
    )
    conn.commit()
    monkeypatch.delenv("BYBIT_DEMO_OVERHEATED_EARLY_PROMOTED", raising=False)

    snapshot = bybit_demo.status_snapshot(
        conn, __import__("threading").Lock(), app.BybitDemoClient.from_env()
    )

    assert len(snapshot["active_whitelist"]) == 3
    assert all(
        {"strategy", "confirmation_level", "status"} <= set(slot)
        for slot in snapshot["active_whitelist"]
    )
    assert snapshot["active_whitelist"][2]["overheated_early_decision"] == "not_promoted"
    assert snapshot["overheated_early_decision"]["active"] is False
    assert snapshot["open_positions"] == [
        {"strategy": "overheated_confirmed", "symbol": "BTCUSDT", "opened_at": 101}
    ]
    assert "api_secret" not in str(snapshot).lower()


def test_http_status_requires_dedicated_token_and_returns_operational_fields(monkeypatch):
    conn = _db()
    conn.execute(
        "INSERT INTO alerts (ts, symbol, alert_type, recommendation, price_at_alert) "
        "VALUES (123, 'BTCUSDT', 'test', 'LONG', 100)"
    )
    conn.commit()
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setenv("STATUS_API_TOKEN", "status-test-token")

    client = app.app.test_client()
    assert client.get("/bot-api/status").status_code == 401

    response = client.get(
        "/bot-api/status",
        headers={"X-Status-Token": "status-test-token"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["last_signal_at"] == 123
    assert isinstance(payload["last_successful_poll_at"], (int, type(None)))
    assert isinstance(payload["polling_stale"], bool)
    assert isinstance(payload["active_whitelist"], list)
    assert isinstance(payload["open_positions"], list)


def test_http_status_missing_token_configuration_is_auth_failure(monkeypatch):
    monkeypatch.delenv("STATUS_API_TOKEN", raising=False)

    client = app.app.test_client()
    response = client.get("/bot-api/status")

    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}


def test_forensic_endpoint_is_authenticated_fixed_scope_and_read_only(monkeypatch):
    conn = _db()
    initialize_schema(conn)
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
            status TEXT NOT NULL,
            ts_close INTEGER,
            is_shadow INTEGER NOT NULL DEFAULT 0,
            shadow_reason TEXT,
            alert_type TEXT,
            is_top INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, symbol, direction,
            signal_price, entry_price, sl_price, tp_price, order_link_id,
            ts_created, status, raw_order_json
        ) VALUES ('forensic-egld', 1788372210, 'overheated_24h', 'EGLDUSDT',
                  'LONG', 4.6, 4.6, 4.4, 5.0, 'link-forensic-egld',
                  1788372210, 'intent', '{"secret":"must-not-leak"}')
        """
    )
    conn.execute(
        """
        INSERT INTO demo_positions (
            ts_open, symbol, direction, entry_price, sl_price, tp_price,
            status, is_shadow, shadow_reason, alert_type, is_top, ts_close
        ) VALUES (1788373406, 'ARBUSDT', 'SHORT', 0.12, 0.13, 0.10,
                  'open', 0, 'must-not-leak', 'overheated_24h', 0, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO bybit_demo_reversals (
            symbol, source_signal_key, source_direction, target_direction,
            state, close_deadline_ts, claimed_ts, created_ts, updated_ts
        ) VALUES ('ARBUSDT', 'source-arb', 'LONG', 'SHORT', 'CLOSING',
                  1788373500, 1788373406, 1788373406, 1788373406)
        """
    )
    conn.commit()
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setenv("STATUS_API_TOKEN", "status-test-token")

    client = app.app.test_client()
    assert client.get("/bot-api/bybit-demo-forensic").status_code == 401

    response = client.get(
        "/bot-api/bybit-demo-forensic",
        headers={"X-Status-Token": "status-test-token"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["read_only"] is True
    assert [item["symbol"] for item in payload["cases"]] == [
        "EGLDUSDT",
        "ARBUSDT",
        "KITEUSDT",
    ]

    egl = payload["cases"][0]
    assert egl["ledger_rows"] == [
        {
            "ledger_id": 1,
            "strategy": "overheated_24h",
            "symbol": "EGLDUSDT",
            "direction": "LONG",
            "status": "intent",
            "signal_ts": 1788372210,
            "created_ts": 1788372210,
        }
    ]
    assert egl["reversal_rows"] == []
    assert egl["demo_rows"] == []
    assert egl["demo_id_gap_rows"] == []

    arb = payload["cases"][1]
    assert arb["ledger_rows"] == []
    assert arb["reversal_rows"] == [
        {
            "reversal_id": 1,
            "symbol": "ARBUSDT",
            "state": "CLOSING",
            "source_direction": "LONG",
            "target_direction": "SHORT",
            "created_ts": 1788373406,
        }
    ]
    assert arb["demo_rows"] == [
        {
            "demo_id": 1,
            "symbol": "ARBUSDT",
            "direction": "SHORT",
            "alert_type": "overheated_24h",
            "status": "open",
            "is_shadow": False,
            "is_top": False,
            "ts_open": 1788373406,
            "ts_close": None,
        }
    ]
    assert arb["demo_id_gap_rows"] == [{"id": 1, "ts_open": 1788373406}]
    assert set(arb["demo_id_gap_rows"][0]) == {"id", "ts_open"}
    assert payload["cases"][2]["ledger_rows"] == []
    assert payload["cases"][2]["reversal_rows"] == []
    assert payload["cases"][2]["demo_rows"] == []
    assert payload["cases"][2]["demo_id_gap_rows"] == [
        {"id": 1, "ts_open": 1788373406}
    ]
    assert "raw_order_json" not in response.text
    assert "must-not-leak" not in response.text
    assert "entry_price" not in response.text
