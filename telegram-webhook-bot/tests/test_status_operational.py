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