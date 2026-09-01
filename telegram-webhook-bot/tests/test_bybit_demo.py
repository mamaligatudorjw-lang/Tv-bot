import hashlib
import hmac
import json
import sqlite3
import threading
import time

import pytest

import app
import bybit_demo
from app import _bybit_demo_signal_allowed
from bybit_relay import create_app as create_bybit_relay_app
from bybit_demo import (
    BybitDemoClient,
    BybitDemoError,
    BybitDemoSizingError,
    BYBIT_DEMO_EQUITY_RESERVE_ENV,
    BYBIT_DEMO_MAX_EXPOSURE_ENV,
    BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV,
    BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV,
    BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_ENV,
    BYBIT_DEMO_SHADOW_GATE_FIX_TS,
    _reset_reserve_health_for_tests,
    allowed_signal_variants,
    backfill_gate_metadata,
    calculate_linear_quantity,
    classify_gate_metadata,
    initialize_schema,
    is_allowed_signal,
    poll_positions,
    record_reserve_health,
    reserve_preflight,
    reserve_config,
    reserve_health_config,
    reserve_health_status,
    status_snapshot,
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
    route = "test"
    require_relay = True
    relay_configured = True
    configuration_error = None

    def __init__(self):
        self.create_calls = []
        self.order = []
        self.executions = []
        self.positions = []
        self.closed = []
        self.wallet_balance = [{"totalWalletBalance": "1000"}]

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

    def get_open_positions(self):
        return list(self.positions)

    def get_wallet_balance(self):
        return list(self.wallet_balance)

    def get_closed_pnl(self, symbol):
        return list(self.closed)

    def get_executions(self, symbol, order_id=None):
        return list(self.executions)


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
    assert len(allowed_signal_variants(overheated_early_is_promoted=False)) == 3
    assert allowed_signal_variants(overheated_early_is_promoted=False)[
        "ema_cross_confirmed"
    ] == "1/3"
    assert allowed_signal_variants(overheated_early_is_promoted=True) == {
        "overheated_24h": None,
        "overheated_confirmed": "1/3",
        "overheated_early": None,
    }
    assert is_allowed_signal(
        "overheated_early", None, overheated_early_is_promoted=True
    )
    assert not is_allowed_signal(
        "ema_cross_confirmed", "1/3", overheated_early_is_promoted=True
    )


def test_app_gate_excludes_every_shadow_variant():
    assert _bybit_demo_signal_allowed(False, "overheated_24h", None)
    assert not _bybit_demo_signal_allowed(True, "overheated_24h", None)
    assert _bybit_demo_signal_allowed(False, "overheated_confirmed", "1/3")
    assert _bybit_demo_signal_allowed(False, "ema_cross_confirmed", "1/3")
    assert not _bybit_demo_signal_allowed(True, "overheated_confirmed", "1/3")
    assert not _bybit_demo_signal_allowed(True, "ema_cross_confirmed", "1/3")
    assert not _bybit_demo_signal_allowed(
        True, "ema_cross_confirmed", "2/3"
    )


def test_app_gate_uses_same_conditional_third_slot(monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_OVERHEATED_EARLY_PROMOTED", "1")
    assert _bybit_demo_signal_allowed(False, "overheated_early", None)
    assert not _bybit_demo_signal_allowed(
        False, "ema_cross_confirmed", "1/3"
    )
    assert not _bybit_demo_signal_allowed(True, "overheated_early", None)


@pytest.mark.parametrize(
    ("strategy", "confirmation"),
    [
        ("overheated_24h", None),
        ("overheated_confirmed", "1/3"),
        ("ema_cross_confirmed", "1/3"),
    ],
)
def test_decision_186_whitelist_overrides_global_shadow_mode(
    monkeypatch, strategy, confirmation
):
    monkeypatch.setattr(app, "SHADOW_ONLY_MODE", True)

    # The application-level override makes the source row real, while the
    # shared Bybit gate still receives is_shadow=False and applies its own
    # strategy/confirmation whitelist.
    assert app.ALERT_TYPE_SHADOW_ONLY[strategy] is False
    assert _bybit_demo_signal_allowed(False, strategy, confirmation)
    assert not _bybit_demo_signal_allowed(True, strategy, confirmation)


@pytest.mark.parametrize("strategy", ["overheated_confirmed", "ema_cross_confirmed"])
@pytest.mark.parametrize("confirmation", ["2/3", "3/3"])
def test_decision_186_keeps_repeat_confirmations_out_of_bybit(
    strategy, confirmation
):
    assert app.ALERT_TYPE_SHADOW_ONLY[strategy] is False
    assert not _bybit_demo_signal_allowed(False, strategy, confirmation)


def test_missing_timestamp_is_uncertain_not_a_leak():
    metadata = classify_gate_metadata(1, None)
    assert metadata["pre_gate_exception"] == 0
    assert metadata["post_fix_leak"] == 0
    assert metadata["gate_classification_uncertain"] == 1


@pytest.mark.parametrize(
    ("shadow_origin", "placement_ts", "expected"),
    [
        (1, BYBIT_DEMO_SHADOW_GATE_FIX_TS - 1, (1, 0, 0, 0, 0)),
        (1, BYBIT_DEMO_SHADOW_GATE_FIX_TS, (0, 1, 0, 0, 0)),
        (0, BYBIT_DEMO_SHADOW_GATE_FIX_TS - 1, (0, 0, 0, 0, 0)),
        (0, BYBIT_DEMO_SHADOW_GATE_FIX_TS, (0, 0, 0, 0, 0)),
        (None, BYBIT_DEMO_SHADOW_GATE_FIX_TS - 1, (0, 0, 1, 1, 0)),
        (None, BYBIT_DEMO_SHADOW_GATE_FIX_TS, (0, 0, 1, 0, 1)),
        (None, None, (0, 0, 1, 0, 0)),
    ],
)
def test_gate_classification_fact_first_and_exact_cutoff(
    shadow_origin, placement_ts, expected
):
    metadata = classify_gate_metadata(shadow_origin, placement_ts)
    assert (
        metadata["pre_gate_exception"],
        metadata["post_fix_leak"],
        metadata["gate_classification_uncertain"],
        metadata["fallback_pre_gate_exception"],
        metadata["fallback_post_fix_leak"],
    ) == expected


def test_shadow_source_is_blocked_before_ledger_and_external_order():
    conn = _db()
    client = FakeTradingClient()
    result = submit_signal(
        conn,
        threading.Lock(),
        client,
        strategy="ema_cross_confirmed",
        confirmation_level="1/3",
        source_demo_position_id=99,
        signal_ts=1_700_000_000,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
        source_is_shadow=True,
    )
    assert result == {"status": "filtered", "reason": "shadow_source"}
    assert client.create_calls == []
    assert conn.execute("SELECT COUNT(*) FROM bybit_demo_positions").fetchone()[0] == 0


def test_backfill_prefers_source_fact_and_logs_confirmed_post_fix_leak(caplog):
    conn = _db()
    conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, confirmation_level, symbol, direction,
            source_demo_position_id, signal_price, entry_price, sl_price, tp_price,
            order_link_id, ts_created, ts_submitted
            ) VALUES ('legacy-source', 10, 'ema_cross_confirmed', '1/3', 'ZKCUSDT',
                      'LONG', 6526, 100, 100, 95, 110, 'link-source', ?, ?)
        """,
        (BYBIT_DEMO_SHADOW_GATE_FIX_TS - 100, BYBIT_DEMO_SHADOW_GATE_FIX_TS - 50),
    )
    conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, confirmation_level, symbol, direction,
            signal_price, entry_price, sl_price, tp_price, order_link_id, ts_created
        ) VALUES ('legacy-fallback', 10, 'ema_cross_confirmed', '1/3', 'BTCUSDT',
                  'LONG', 100, 100, 95, 110, 'link-fallback', ?)
        """,
        (BYBIT_DEMO_SHADOW_GATE_FIX_TS + 50,),
    )
    conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, confirmation_level, symbol, direction,
            source_demo_position_id, signal_price, entry_price, sl_price, tp_price,
            order_link_id, ts_created
        ) VALUES ('legacy-post', 10, 'overheated_confirmed', '1/3', 'ZKCUSDT',
                  'LONG', 6527, 100, 100, 95, 110, 'link-post', ?)
        """,
        (BYBIT_DEMO_SHADOW_GATE_FIX_TS + 1,),
    )
    backfill_gate_metadata(conn, {6526: 1, 6527: 1})
    source_row = conn.execute(
        """
        SELECT shadow_origin, pre_gate_exception, post_fix_leak,
               gate_classification_uncertain, fallback_pre_gate_exception,
               fallback_post_fix_leak
        FROM bybit_demo_positions WHERE signal_key='legacy-source'
        """
    ).fetchone()
    fallback_row = conn.execute(
        """
        SELECT shadow_origin, pre_gate_exception, post_fix_leak,
               gate_classification_uncertain, fallback_pre_gate_exception,
               fallback_post_fix_leak
        FROM bybit_demo_positions WHERE signal_key='legacy-fallback'
        """
    ).fetchone()
    assert source_row == (1, 1, 0, 0, 0, 0)
    assert fallback_row == (None, 0, 0, 1, 0, 1)
    post_row = conn.execute(
        """
        SELECT shadow_origin, pre_gate_exception, post_fix_leak,
               gate_classification_uncertain
        FROM bybit_demo_positions WHERE signal_key='legacy-post'
        """
    ).fetchone()
    assert post_row == (1, 0, 1, 0)
    assert "bybit_demo_post_fix_leak" in caplog.text
    assert "symbol=ZKCUSDT" in caplog.text


def test_status_exposes_post_fix_leak_identity_without_secrets():
    conn = _db()
    conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, confirmation_level, symbol, direction,
            signal_price, entry_price, sl_price, tp_price, order_link_id, ts_created,
            shadow_origin, post_fix_leak
        ) VALUES ('post-fix', 10, 'ema_cross_confirmed', '1/3', 'LEAKUSDT',
                  'LONG', 100, 100, 95, 110, 'link-post-fix', ?, 1, 1)
        """,
        (BYBIT_DEMO_SHADOW_GATE_FIX_TS + 1,),
    )
    snapshot = status_snapshot(conn, threading.Lock(), FakeTradingClient())
    assert snapshot["post_fix_leak_count"] == 1
    assert snapshot["post_fix_leak_alert"] is True
    assert snapshot["post_fix_leak_latest"]["symbol"] == "LEAKUSDT"
    assert snapshot["post_fix_leak_latest"]["timestamp"] == BYBIT_DEMO_SHADOW_GATE_FIX_TS + 1
    assert "api_secret" not in json.dumps(snapshot).lower()


def test_quantity_rounds_down_to_step_and_never_exceeds_requested_notional():
    qty = calculate_linear_quantity(50, 123, "0.01", "0.001")
    assert qty == pytest.approx(0.406)
    assert qty * 123 <= 50

    with pytest.raises(BybitDemoSizingError):
        calculate_linear_quantity(50, 10_000, "1", "1")


@pytest.mark.parametrize(
    ("open_exposure", "expected_decision", "expected_reason"),
    [
        (449.0, "allow", "allowed"),
        (450.0, "allow", "allowed"),
        (451.0, "blocked", "exposure_cap"),
    ],
)
def test_reserve_preflight_exposure_gate_boundaries(
    open_exposure, expected_decision, expected_reason
):
    client = FakeTradingClient()
    client.positions = [{
        "symbol": "BTCUSDT",
        "size": "1",
        "positionValue": str(open_exposure),
        "unrealisedPnl": "0",
    }]

    result = reserve_preflight(client, 50.0)

    assert result["decision"] == expected_decision
    assert result["reason"] == expected_reason
    assert result["open_exposure_usd"] == pytest.approx(open_exposure)
    assert result["exposure_gate_passed"] is (expected_decision == "allow")


@pytest.mark.parametrize(
    ("balance", "unrealized_pnl", "expected_decision", "expected_reason"),
    [
        ("101", "-1", "allow", "allowed"),
        ("100", "0", "allow", "allowed"),
        ("99", "0", "blocked", "equity_floor"),
        ("1000", "-1", "allow", "allowed"),
    ],
)
def test_reserve_preflight_equity_floor_and_unrealized_pnl(
    balance, unrealized_pnl, expected_decision, expected_reason
):
    client = FakeTradingClient()
    client.wallet_balance = [{"totalWalletBalance": balance}]
    client.positions = [{
        "symbol": "BTCUSDT",
        "size": "1",
        "positionValue": "50",
        "unrealisedPnl": unrealized_pnl,
    }]

    result = reserve_preflight(client, 50.0)

    assert result["decision"] == expected_decision
    assert result["reason"] == expected_reason
    assert result["equity_usd"] == pytest.approx(
        float(balance) + float(unrealized_pnl)
    )
    assert result["equity_gate_passed"] is (expected_decision == "allow")


def test_reserve_preflight_reads_validated_environment_overrides(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MAX_EXPOSURE_ENV, "600")
    monkeypatch.setenv(BYBIT_DEMO_EQUITY_RESERVE_ENV, "250")
    config = reserve_config()

    assert config == {
        "valid": True,
        "configuration_error": None,
        "max_exposure_usd": 600.0,
        "equity_reserve_usd": 250.0,
    }


def test_reserve_preflight_invalid_environment_fails_closed(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MAX_EXPOSURE_ENV, "not-a-number")
    client = FakeTradingClient()

    result = reserve_preflight(client, 50.0)

    assert result["decision"] == "error"
    assert result["reason"] == "configuration_invalid"
    assert client.create_calls == []


def test_reserve_health_uses_real_time_window_and_resets_on_success(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV, "600")
    monkeypatch.setenv(BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV, "3")
    _reset_reserve_health_for_tests()

    first = record_reserve_health(success=False, error="relay_error", now=100)
    second = record_reserve_health(success=False, error="relay_error", now=200)
    third = record_reserve_health(success=False, error="relay_error", now=300)

    assert first["failure_count"] == 1
    assert first["alert_triggered"] is False
    assert second["failure_count"] == 2
    assert second["alert_triggered"] is False
    assert third["failure_count"] == 3
    assert third["alert_triggered"] is True
    assert third["alert_active"] is True

    repeated = record_reserve_health(
        success=False,
        error="relay_error",
        now=301,
    )
    assert repeated["failure_count"] == 4
    assert repeated["alert_triggered"] is False

    healthy = record_reserve_health(
        success=True,
        snapshot={
            "open_exposure_usd": 100.0,
            "balance_usd": 1000.0,
            "unrealized_pnl_usd": -1.0,
            "equity_usd": 999.0,
        },
        now=302,
    )
    assert healthy["failure_count"] == 0
    assert healthy["alert_active"] is False
    assert healthy["last_error"] is None
    assert healthy["latest"]["equity_usd"] == 999.0
    assert reserve_health_status()["failure_count"] == 0
    _reset_reserve_health_for_tests()


def test_reserve_health_invalid_env_uses_safe_defaults(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_ENV, "0")
    monkeypatch.setenv(BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV, "bad")
    monkeypatch.setenv(BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV, "-1")

    config = reserve_health_config()

    assert config["interval_sec"] == 60
    assert config["window_sec"] == 600
    assert config["threshold"] == 3
    assert set(config["configuration_fallback"]) == {
        BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_ENV,
        BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV,
        BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV,
    }


def test_reserve_probe_is_noop_when_bybit_client_is_disabled(monkeypatch):
    _reset_reserve_health_for_tests()
    client = FakeTradingClient()
    client.enabled = False
    monkeypatch.setattr(
        app.BybitDemoClient,
        "from_env",
        classmethod(lambda _cls: client),
    )

    def unexpected_read(_client):
        raise AssertionError("disabled probe must not read Bybit")

    monkeypatch.setattr(app, "read_bybit_reserve_snapshot", unexpected_read)
    app._run_bybit_demo_reserve_probe()

    assert reserve_health_status()["failure_count"] == 0
    _reset_reserve_health_for_tests()


def test_reserve_probe_records_enabled_read_without_order_decision(monkeypatch):
    _reset_reserve_health_for_tests()
    client = FakeTradingClient()
    observed = {
        "open_exposure_usd": 150.0,
        "balance_usd": 1000.0,
        "unrealized_pnl_usd": -2.0,
        "equity_usd": 998.0,
    }
    monkeypatch.setattr(
        app.BybitDemoClient,
        "from_env",
        classmethod(lambda _cls: client),
    )
    monkeypatch.setattr(
        app,
        "read_bybit_reserve_snapshot",
        lambda _client: observed,
    )

    app._run_bybit_demo_reserve_probe()

    health = reserve_health_status()
    assert health["failure_count"] == 0
    assert health["alert_active"] is False
    assert health["latest"] == observed
    assert client.create_calls == []
    _reset_reserve_health_for_tests()


def test_blocked_reserve_preflight_records_observed_values_and_never_posts():
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()
    client.positions = [{
        "symbol": "BTCUSDT",
        "size": "1",
        "positionValue": "451",
        "unrealisedPnl": "0",
    }]
    original_positions = list(client.positions)

    result = submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=501,
        signal_ts=1_700_000_000,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "exposure_cap"
    assert client.create_calls == []
    assert client.positions == original_positions
    row = conn.execute(
        """
        SELECT status, last_error, preflight_decision, preflight_reason,
               preflight_open_exposure_usd, preflight_equity_usd
        FROM bybit_demo_positions
        """
    ).fetchone()
    assert row[0] == "rejected"
    assert row[1] == "reserve_preflight:exposure_cap"
    assert row[2:] == ("blocked", "exposure_cap", 451.0, 1000.0)
    snapshot = status_snapshot(conn, db_lock, client)
    latest = snapshot["reserve_preflight"]["latest"]
    assert latest["decision"] == "blocked"
    assert latest["reason"] == "exposure_cap"
    assert latest["open_exposure_usd"] == 451.0
    assert latest["equity_usd"] == 1000.0
    assert "relay-secret" not in json.dumps(snapshot)


def test_reserve_preflight_relay_error_is_fail_closed_without_post():
    class RelayErrorClient(FakeTradingClient):
        def get_wallet_balance(self):
            raise BybitDemoError(
                "/v5/account/wallet-balance",
                "transport_error",
                retryable=True,
                transport=True,
            )

    conn = _db()
    db_lock = threading.Lock()
    client = RelayErrorClient()

    result = submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=502,
        signal_ts=1_700_000_001,
        symbol="ETHUSDT",
        direction="SHORT",
        signal_price=100,
        entry_price=100,
        sl_price=105,
        tp_price=90,
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "relay_or_api_error"
    assert client.create_calls == []
    row = conn.execute(
        "SELECT status, preflight_decision, preflight_reason "
        "FROM bybit_demo_positions"
    ).fetchone()
    assert row == ("rejected", "error", "relay_or_api_error")


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
        source_is_shadow=False,
    )

    first = submit_signal(conn, db_lock, client, **args)
    second = submit_signal(conn, db_lock, client, **args)

    assert first["status"] == "submitted"
    assert second["status"] == "duplicate"
    assert len(client.create_calls) == 1
    row = conn.execute(
        "SELECT strategy, confirmation_level, status, order_id, qty, shadow_origin "
        "FROM bybit_demo_positions"
    ).fetchone()
    assert row[:4] == ("ema_cross_confirmed", "1/3", "submitted", "order-1")
    assert row[4] == pytest.approx(0.5)
    assert row[5] == 0
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


def test_first_observed_fill_is_immutable_and_poll_time_is_separate(
    monkeypatch, caplog
):
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()
    result = submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=70,
        signal_ts=1_700_000_000,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )
    assert result["status"] == "submitted"
    conn.execute(
        "UPDATE bybit_demo_positions SET ts_submitted=?",
        (1_700_000_000,),
    )
    conn.commit()

    client.order = [{
        "orderId": "order-1",
        "orderStatus": "Filled",
        "cumExecQty": "0.5",
        "avgPrice": "100",
        "createdTime": "1700000000123",
    }]
    client.executions = [{"execTime": "1700000000456"}]
    caplog.set_level("INFO", logger="bybit_demo")

    monkeypatch.setattr(bybit_demo.time, "time", lambda: 1_700_000_100)
    first = poll_positions(conn, db_lock, client)
    first_row = conn.execute(
        """
        SELECT status, ts_submitted, ts_filled, exchange_created_time,
               exchange_exec_time, last_polled
        FROM bybit_demo_positions
        """
    ).fetchone()

    monkeypatch.setattr(bybit_demo.time, "time", lambda: 1_700_000_120)
    second = poll_positions(conn, db_lock, client)
    second_row = conn.execute(
        """
        SELECT status, ts_submitted, ts_filled, exchange_created_time,
               exchange_exec_time, last_polled
        FROM bybit_demo_positions
        """
    ).fetchone()

    assert first["polled"] == 1
    assert second["polled"] == 1
    assert first_row == (
        "submitted",
        1_700_000_000,
        1_700_000_100,
        1_700_000_000123,
        1_700_000_000456,
        1_700_000_100,
    )
    assert second_row == (
        "submitted",
        1_700_000_000,
        1_700_000_100,
        1_700_000_000123,
        1_700_000_000456,
        1_700_000_120,
    )
    assert caplog.text.count("bybit_demo_fill_observed") == 1
    assert "order_id=order-1" in caplog.text
    assert "symbol=BTCUSDT" in caplog.text
    assert "latency_sec=100.000" in caplog.text


def test_polling_allocates_aggregate_exchange_close_across_entries():
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()
    common = dict(
        strategy="overheated_24h",
        confirmation_level=None,
        signal_ts=int(time.time()),
        symbol="ZKCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
        source_is_shadow=False,
    )
    first = submit_signal(
        conn,
        db_lock,
        client,
        source_demo_position_id=100,
        **common,
    )
    client.order = []
    second = submit_signal(
        conn,
        db_lock,
        client,
        source_demo_position_id=101,
        **common,
    )
    assert first["status"] == "submitted"
    assert second["status"] == "submitted"

    entry_ms = (int(time.time()) - 100) * 1000
    client.order = [{
        "orderId": "order-1",
        "orderStatus": "Filled",
        "cumExecQty": "1",
        "avgPrice": "100",
        "createdTime": entry_ms,
    }]
    event_ms = entry_ms + 5_000
    client.closed = [{
        "symbol": "ZKCUSDT",
        "avgEntryPrice": "100",
        "avgExitPrice": "110",
        "closedPnl": "6.74939849",
        "openFee": "0.05545813",
        "closeFee": "0.05923338",
        "closedSize": "2",
        "createdTime": event_ms,
        "updatedTime": event_ms,
    }]

    polled = poll_positions(conn, db_lock, client)
    rows = conn.execute(
        """
        SELECT status, exit_price, realized_pnl_usd, fee_usd, ts_closed
        FROM bybit_demo_positions ORDER BY id
        """
    ).fetchall()
    assert polled["closed"] == 2
    assert rows == [
        ("closed", 110.0, pytest.approx(3.374699245), pytest.approx(0.057345755), event_ms // 1000),
        ("closed", 110.0, pytest.approx(3.374699245), pytest.approx(0.057345755), event_ms // 1000),
    ]


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


def test_client_reads_reserve_inputs_through_https_relay():
    session = RecordingSession()
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        relay_url="https://relay.example.test",
        relay_token="relay-secret",
        session=session,
        clock=lambda: 1_700_000_000,
    )

    client.get_wallet_balance()
    client.get_open_positions()

    assert [call[0] for call in session.calls] == ["GET", "GET"]
    assert session.calls[0][1] == (
        "https://relay.example.test/v5/account/wallet-balance"
        "?accountType=UNIFIED&coin=USDT"
    )
    assert session.calls[1][1] == (
        "https://relay.example.test/v5/position/list"
        "?category=linear&settleCoin=USDT"
    )
    assert all(
        call[2]["headers"]["X-Bybit-Relay-Token"] == "relay-secret"
        for call in session.calls
    )


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