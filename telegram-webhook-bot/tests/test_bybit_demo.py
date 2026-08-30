import hashlib
import hmac
import json
import sqlite3
import threading
import time

import pytest

from app import _bybit_demo_signal_allowed
from bybit_relay import create_app as create_bybit_relay_app
from bybit_demo import (
    BybitDemoClient,
    BybitDemoSizingError,
    calculate_linear_quantity,
    initialize_schema,
    is_allowed_signal,
    poll_positions,
    submit_signal,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, payload=None):
        self.payload = payload or {"retCode": 0, "result": {"list": []}}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.payload)


class FakeTradingClient:
    enabled = True

    def __init__(self):
        self.create_calls = []
        self.order = []
        self.positions = []
        self.closed = []

    def get_instrument_info(self, symbol):
        return {"min_order_qty": 0.001, "qty_step": 0.001, "tick_size": 0.1}

    def get_order_realtime(self, *, symbol, order_id=None, order_link_id=None):
        return list(self.order)

    def create_market_order(self, **kwargs):
        self.create_calls.append(kwargs)
        self.order = [{
            "orderId": "order-1",
            "orderStatus": "New",
            "cumExecQty": "0",
            "avgPrice": "",
        }]
        return {"orderId": "order-1", "orderStatus": "New"}

    def get_position(self, symbol):
        return list(self.positions)

    def get_closed_pnl(self, symbol):
        return list(self.closed)

    def get_executions(self, symbol, order_id=None):
        return []


def _db():
    conn = sqlite3.connect(":memory:")
    initialize_schema(conn)
    return conn


def test_bybit_signature_uses_v5_payload_and_never_changes_for_get():
    session = RecordingSession()
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        session=session,
        clock=lambda: 1_700_000_000,
    )

    client.get_order_realtime(symbol="BTCUSDT", order_link_id="bdabc")

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    expected_query = "category=linear&orderLinkId=bdabc&symbol=BTCUSDT"
    assert url == (
        "https://api-demo.bybit.com/v5/order/realtime?"
        + expected_query
    )
    assert "params" not in kwargs
    payload = str(1_700_000_000_000) + "api-key" + "5000" + expected_query
    expected = hmac.new(
        b"api-secret", payload.encode(), hashlib.sha256
    ).hexdigest()
    assert kwargs["headers"]["X-BAPI-SIGN"] == expected
    assert kwargs["headers"]["X-BAPI-API-KEY"] == "api-key"


def test_whitelist_requires_first_confirmation_only():
    assert is_allowed_signal("overheated_24h", None)
    assert is_allowed_signal("overheated_confirmed", "1/3")
    assert is_allowed_signal("ema_cross_confirmed", "1/3")
    assert not is_allowed_signal("overheated_confirmed", "2/3")
    assert not is_allowed_signal("ema_cross_confirmed", "3/3")
    assert not is_allowed_signal("overheated_early", None)


def test_app_gate_excludes_overheated_shadow_but_allows_continuation_one_of_three():
    assert _bybit_demo_signal_allowed(False, "overheated_24h", None)
    assert not _bybit_demo_signal_allowed(True, "overheated_24h", None)
    assert _bybit_demo_signal_allowed(
        True, "overheated_confirmed", "1/3"
    )
    assert _bybit_demo_signal_allowed(
        True, "ema_cross_confirmed", "1/3"
    )
    assert not _bybit_demo_signal_allowed(
        True, "ema_cross_confirmed", "2/3"
    )


def test_quantity_rounds_down_to_step_and_never_exceeds_requested_notional():
    qty = calculate_linear_quantity(50, 123, "0.01", "0.001")
    assert qty == pytest.approx(0.406)
    assert qty * 123 <= 50

    with pytest.raises(BybitDemoSizingError):
        calculate_linear_quantity(50, 10_000, "1", "1")


def test_submit_signal_is_idempotent_and_keeps_paper_table_separate():
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()
    args = dict(
        strategy="ema_cross_confirmed",
        confirmation_level="1/3",
        source_demo_position_id=42,
        signal_ts=1_700_000_000,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )

    first = submit_signal(conn, db_lock, client, **args)
    second = submit_signal(conn, db_lock, client, **args)

    assert first["status"] == "submitted"
    assert second["status"] == "duplicate"
    assert len(client.create_calls) == 1
    row = conn.execute(
        "SELECT strategy, confirmation_level, status, order_id, qty "
        "FROM bybit_demo_positions"
    ).fetchone()
    assert row[:4] == ("ema_cross_confirmed", "1/3", "submitted", "order-1")
    assert row[4] == pytest.approx(0.5)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='demo_positions'"
    ).fetchone() is None


def test_polling_closes_filled_position_from_closed_pnl():
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()
    args = dict(
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=7,
        signal_ts=int(time.time()),
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )
    result = submit_signal(conn, db_lock, client, **args)
    assert result["status"] == "submitted"

    client.order = [{
        "orderId": "order-1",
        "orderStatus": "Filled",
        "cumExecQty": "0.5",
        "avgPrice": "100",
    }]
    client.closed = [{
        "symbol": "BTCUSDT",
        "avgExitPrice": "110",
        "closedPnl": "4.75",
        "openFee": "0.1",
        "closeFee": "0.1",
        "updatedTime": int(time.time() * 1000),
    }]

    polled = poll_positions(conn, db_lock, client)
    row = conn.execute(
        "SELECT status, exit_price, realized_pnl_usd, fee_usd, exit_reason "
        "FROM bybit_demo_positions"
    ).fetchone()
    assert polled["closed"] == 1
    assert row == ("closed", 110.0, 4.75, 0.2, "tp")


def test_recovery_never_posts_when_order_is_ambiguous_or_missing():
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()
    args = dict(
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=8,
        signal_ts=1_700_000_001,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )
    assert submit_signal(conn, db_lock, client, **args)["status"] == "submitted"
    client.order = []

    # The row has an order id from the first acknowledgement, so a missing
    # realtime row is reconciled conservatively and never creates a second POST.
    outcome = poll_positions(conn, db_lock, client)
    assert outcome["polled"] == 1
    assert len(client.create_calls) == 1
    status = conn.execute(
        "SELECT status FROM bybit_demo_positions"
    ).fetchone()[0]
    assert status in {"submitted", "unknown"}


def test_client_uses_https_relay_and_keeps_relay_token_out_of_url():
    session = RecordingSession()
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        relay_url="https://relay.example.test",
        relay_token="relay-secret",
        session=session,
        clock=lambda: 1_700_000_000,
    )

    client.get_order_realtime(symbol="BTCUSDT", order_link_id="bdabc")

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == (
        "https://relay.example.test/v5/order/realtime"
        "?category=linear&orderLinkId=bdabc&symbol=BTCUSDT"
    )
    assert "params" not in kwargs
    assert kwargs["headers"]["X-Bybit-Relay-Token"] == "relay-secret"
    assert "relay-secret" not in url


def test_client_rejects_incomplete_or_insecure_relay_configuration():
    missing_token = BybitDemoClient("key", "secret", relay_url="https://relay.example")
    assert not missing_token.enabled
    assert missing_token.disabled_reason == "relay_token_missing"

    insecure = BybitDemoClient(
        "key",
        "secret",
        relay_url="http://relay.example",
        relay_token="token",
    )
    assert not insecure.enabled
    assert insecure.disabled_reason == "relay_url_must_be_https_origin"

    pathful = BybitDemoClient(
        "key",
        "secret",
        relay_url="https://relay.example/prefix",
        relay_token="token",
    )
    assert not pathful.enabled
    assert pathful.disabled_reason == "relay_url_must_be_https_origin"

    token_without_url = BybitDemoClient("key", "secret", relay_token="token")
    assert not token_without_url.enabled
    assert token_without_url.disabled_reason == "relay_url_missing"


def test_production_client_from_env_fails_closed_without_relay(monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "secret")
    monkeypatch.delenv("BYBIT_RELAY_URL", raising=False)
    monkeypatch.delenv("BYBIT_RELAY_TOKEN", raising=False)

    client = BybitDemoClient.from_env()

    assert not client.enabled
    assert client.route == "unconfigured"
    assert client.disabled_reason == "relay_url_missing"


class FakeRelayUpstream:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(
            {"retCode": 0, "result": {"timeSecond": "1700000000"}},
            status_code=200,
        )


def test_relay_requires_https_token_and_fixed_bybit_path():
    upstream = FakeRelayUpstream()
    relay = create_bybit_relay_app(shared_token="relay-secret", session=upstream)
    client = relay.test_client()

    unauthorized = client.get(
        "/v5/market/time",
        base_url="https://relay.example.test",
    )
    assert unauthorized.status_code == 401

    insecure = client.get(
        "/v5/market/time",
        headers={"X-Bybit-Relay-Token": "relay-secret"},
    )
    assert insecure.status_code == 400

    ok = client.get(
        "/v5/market/time?category=linear",
        headers={"X-Bybit-Relay-Token": "relay-secret"},
        base_url="https://relay.example.test",
    )
    assert ok.status_code == 200
    assert (
        upstream.calls[0][1]
        == "https://api-demo.bybit.com/v5/market/time?category=linear"
    )
    assert "params" not in upstream.calls[0][2]

    not_proxy = client.get(
        "/anything?url=https://example.com",
        headers={"X-Bybit-Relay-Token": "relay-secret"},
        base_url="https://relay.example.test",
    )
    assert not_proxy.status_code == 404


def test_relay_strips_relay_credential_before_upstream():
    upstream = FakeRelayUpstream()
    relay = create_bybit_relay_app(shared_token="relay-secret", session=upstream)
    response = relay.test_client().post(
        "/v5/order/create",
        headers={
            "X-Bybit-Relay-Token": "relay-secret",
            "X-BAPI-API-KEY": "api-key",
            "X-BAPI-SIGN": "signature",
            "Content-Type": "application/json",
        },
        data='{"category":"linear"}',
        base_url="https://relay.example.test",
    )

    assert response.status_code == 200
    forwarded_headers = upstream.calls[0][2]["headers"]
    forwarded_lower = {key.lower(): value for key, value in forwarded_headers.items()}
    assert "x-bybit-relay-token" not in forwarded_lower
    assert forwarded_lower["x-bapi-api-key"] == "api-key"
    assert forwarded_lower["x-bapi-sign"] == "signature"