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
    BYBIT_DEMO_MULTI_TP_ENABLED_ENV,
    BYBIT_DEMO_BREAKEVEN_ENABLED_ENV,
    BYBIT_DEMO_TP_TRAIL_LAST_LEG_ENV,
    BYBIT_DEMO_TRADING_ENABLED_ENV,
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
    ensure_pending_tp_orders,
    ensure_breakeven_sl,
    manual_breakeven_snapshot,
    manual_recover_breakeven,
    manual_recover_tp_orders,
    manual_tp_recovery_snapshot,
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
        self.trading_stop_calls = []
        self.trading_stop_plan = []
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

    def set_trading_stop(self, **kwargs):
        self.trading_stop_calls.append(dict(kwargs))
        action = self.trading_stop_plan.pop(0) if self.trading_stop_plan else "apply"
        if action == "duplicate":
            for position in self.positions:
                position["stopLoss"] = str(kwargs["stop_loss"])
            raise BybitDemoError(
                "/v5/position/trading-stop",
                "bybit_ret_code_10014",
                ret_code=10014,
                ret_msg="Invalid duplicate request",
            )
        if isinstance(action, BaseException):
            raise action
        if action == "reject":
            raise BybitDemoError(
                "/v5/position/trading-stop",
                "bybit_ret_code_10001",
                ret_code=10001,
                ret_msg="Request parameter error",
            )
        for position in self.positions:
            position["stopLoss"] = str(kwargs["stop_loss"])
        return {}

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


class MultiTpTradingClient(FakeTradingClient):
    """Filled-entry exchange double with deterministic TP create outcomes."""

    def __init__(self, tp_plan=None, entry_status="Filled", entry_lookup_error=None):
        super().__init__()
        self.tp_plan = list(tp_plan or [])
        self.tp_calls = []
        self.tp_orders = {}
        self.entry_order = None
        self.entry_status = entry_status
        self.entry_lookup_error = entry_lookup_error
        self.entry_created = False

    def create_market_order(self, **kwargs):
        self.create_calls.append(kwargs)
        qty = float(kwargs["qty"])
        self.positions = (
            [{
                "symbol": kwargs["symbol"],
                "side": "Buy" if kwargs["direction"] == "LONG" else "Sell",
                "size": str(qty),
                "positionValue": str(qty * 100),
                "unrealisedPnl": "0",
                "avgPrice": "100",
                "stopLoss": "95",
            }]
            if self.entry_status == "Filled"
            else []
        )
        self.entry_order = {
            "orderId": "entry-multi-1",
            "orderLinkId": kwargs["order_link_id"],
            "orderStatus": self.entry_status,
            "cumExecQty": str(qty) if self.entry_status == "Filled" else "0",
            "avgPrice": "100" if self.entry_status == "Filled" else "",
        }
        self.order = [dict(self.entry_order)]
        self.entry_created = True
        return dict(self.entry_order)

    def get_order_realtime(self, *, symbol, order_id=None, order_link_id=None):
        if self.entry_created and order_id == "entry-multi-1" and self.entry_lookup_error:
            raise self.entry_lookup_error
        if order_link_id and order_link_id in self.tp_orders:
            return [dict(self.tp_orders[order_link_id])]
        if order_id:
            for order in self.tp_orders.values():
                if order.get("orderId") == order_id:
                    return [dict(order)]
        if order_id == "entry-multi-1" or (
            order_link_id
            and self.entry_order
            and order_link_id == self.entry_order["orderLinkId"]
        ):
            return [dict(self.entry_order)]
        return []

    def create_limit_tp_order(self, **kwargs):
        self.tp_calls.append(dict(kwargs))
        action = self.tp_plan.pop(0) if self.tp_plan else "created"
        if isinstance(action, BaseException):
            raise action
        if action == "ambiguous":
            raise BybitDemoError(
                "/v5/order/create",
                "server_timeout",
                retryable=True,
                transport=True,
            )
        if action == "rejected":
            raise BybitDemoError(
                "/v5/order/create",
                "bybit_ret_code_170136",
                ret_code=170136,
                ret_msg="Order quantity lower than the minimum",
            )
        order = {
            "orderId": f"tp-{len(self.tp_calls)}",
            "orderLinkId": kwargs["order_link_id"],
            "orderStatus": "New",
            "cumExecQty": "0",
            "avgPrice": "",
        }
        self.tp_orders[kwargs["order_link_id"]] = order
        return dict(order)


class PartialTpMockClient(FakeTradingClient):
    """Deterministic TP-order stream for poll reconciliation tests."""

    def __init__(self, order_states, execution_states=None):
        super().__init__()
        self.order_states = list(order_states)
        self.execution_states = list(execution_states or [[]])
        self.order_calls = []
        self.execution_calls = []

    def get_order_realtime(self, *, symbol, order_id=None, order_link_id=None):
        self.order_calls.append((symbol, order_id, order_link_id))
        index = min(len(self.order_calls) - 1, len(self.order_states) - 1)
        state = self.order_states[index]
        return [] if state is None else [dict(state)]

    def get_executions(self, symbol, order_id=None):
        self.execution_calls.append((symbol, order_id))
        index = min(len(self.execution_calls) - 1, len(self.execution_states) - 1)
        return [dict(item) for item in self.execution_states[index]]


class ReversalTradingClient(FakeTradingClient):
    """Deterministic one-way exchange double for reversal lifecycle tests."""

    def __init__(self, close_plan=None):
        super().__init__()
        self.close_plan = list(close_plan or [])
        self.close_calls = []
        self.cancel_calls = []
        self.cancel_plan = []
        self.call_log = []
        self.entry_calls = []
        self.orders = {}
        self.entry_order = None
        self.fail_position = False
        self.position_plan = []
        self.fail_close_submission = False
        self.block_entry_with_reserve = False
        self._order_number = 0

    def get_position(self, symbol):
        if self.fail_position:
            raise BybitDemoError(
                "/v5/position/list",
                "position_read_failed",
                payload={"retCode": 10001, "retMsg": "position read failed"},
            )
        if self.position_plan:
            next_positions = self.position_plan.pop(0)
            if isinstance(next_positions, BaseException):
                raise next_positions
            return list(next_positions)
        return list(self.positions)

    def get_order_realtime(self, *, symbol, order_id=None, order_link_id=None):
        if order_id and order_id in self.orders:
            return [self.orders[order_id]]
        if order_link_id and order_link_id in self.orders:
            return [self.orders[order_link_id]]
        if order_link_id and self.entry_order:
            if self.entry_order.get("orderLinkId") == order_link_id:
                return [self.entry_order]
        return []

    def cancel_order(self, *, symbol, order_id=None, order_link_id=None):
        self.cancel_calls.append(
            {"symbol": symbol, "order_id": order_id, "order_link_id": order_link_id}
        )
        self.call_log.append(("cancel", order_id or order_link_id))
        action = self.cancel_plan.pop(0) if self.cancel_plan else "cancel"
        if isinstance(action, BaseException):
            raise action
        key = order_id or order_link_id
        order = self.orders.get(key) if key else None
        if action == "ambiguous":
            raise BybitDemoError(
                "/v5/order/cancel",
                "transport_error",
                retryable=True,
                transport=True,
            )
        if action == "already_terminal":
            raise BybitDemoError(
                "/v5/order/cancel",
                "bybit_ret_code_110008",
                ret_code=110008,
                ret_msg="The order has been completed or cancelled",
            )
        if order is None:
            raise BybitDemoError(
                "/v5/order/cancel",
                "bybit_ret_code_110001",
                ret_code=110001,
                ret_msg="Order does not exist",
            )
        if action == "filled":
            order["orderStatus"] = "Filled"
            order["cumExecQty"] = order.get("cumExecQty") or "0.1"
            try:
                fill_qty = float(order["cumExecQty"])
            except (TypeError, ValueError):
                fill_qty = 0.0
            if fill_qty > 0 and self.positions:
                remaining = max(
                    0.0,
                    float(self.positions[0].get("size") or 0) - fill_qty,
                )
                self.positions = (
                    [{**self.positions[0], "size": str(remaining)}]
                    if remaining > 0
                    else []
                )
        else:
            order["orderStatus"] = "Cancelled"
        return dict(order)

    def create_market_order(self, **kwargs):
        self._order_number += 1
        reduce_only = bool(kwargs.get("reduce_only"))
        if reduce_only:
            self.close_calls.append(dict(kwargs))
            self.call_log.append(("close", kwargs["order_link_id"]))
            if self.fail_close_submission:
                raise BybitDemoError(
                    "/v5/order/create",
                    "transport_error",
                    retryable=True,
                    transport=True,
                )
            status, executed = self.close_plan.pop(0)
            requested = float(kwargs["qty"])
            executed = requested if executed is None else float(executed)
            remaining = max(0.0, requested - executed)
            if remaining:
                self.positions = [{
                    "symbol": kwargs["symbol"],
                    "side": "Buy" if kwargs["direction"] == "LONG" else "Sell",
                    "size": str(remaining),
                    "positionValue": str(remaining * 100),
                    "unrealisedPnl": "0",
                }]
            else:
                self.positions = []
            order_id = f"close-{self._order_number}"
            order = {
                "orderId": order_id,
                "orderLinkId": kwargs["order_link_id"],
                "orderStatus": status,
                "cumExecQty": str(executed),
                "avgPrice": "100",
            }
            self.orders[order_id] = order
            self.orders[kwargs["order_link_id"]] = order
            return dict(order)

        self.entry_calls.append(dict(kwargs))
        if self.block_entry_with_reserve:
            self.positions = []
        else:
            self.positions = [{
                "symbol": kwargs["symbol"],
                "side": "Buy" if kwargs["direction"] == "LONG" else "Sell",
                "size": str(kwargs["qty"]),
                "positionValue": str(float(kwargs["qty"]) * 100),
                "unrealisedPnl": "0",
            }]
        order_id = f"entry-{self._order_number}"
        self.entry_order = {
            "orderId": order_id,
            "orderLinkId": kwargs["order_link_id"],
            "orderStatus": "New",
            "cumExecQty": "0",
            "avgPrice": "",
        }
        self.orders[order_id] = self.entry_order
        self.orders[kwargs["order_link_id"]] = self.entry_order
        return dict(self.entry_order)

    def get_wallet_balance(self):
        return list(self.wallet_balance)

    def get_open_positions(self):
        if self.block_entry_with_reserve:
            return [{
                "symbol": "BTCUSDT",
                "size": "1",
                "positionValue": "4000",
                "unrealisedPnl": "0",
            }]
        return list(self.positions)


def _db():
    conn = sqlite3.connect(":memory:")
    initialize_schema(conn)
    return conn


def _seed_tp_parent(
    conn,
    *,
    signal_key="tp-parent",
    symbol="BTCUSDT",
    order_link_id="bd-tp-parent",
):
    cursor = conn.execute(
        """
        INSERT INTO bybit_demo_positions (
            signal_key, signal_ts, strategy, symbol, direction,
            signal_price, entry_price, sl_price, tp_price,
            order_link_id, ts_created
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_key,
            1_700_000_000,
            "overheated_24h",
            symbol,
            "LONG",
            100,
            100,
            95,
            110,
            order_link_id,
            1_700_000_000,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _seed_tp_leg_order(
    conn,
    db_lock,
    ledger_id,
    client,
    *,
    leg_index,
    order_id,
    order_link_id,
    status="New",
    executed_qty="0",
):
    leg_id = bybit_demo._insert_tp_leg(
        conn,
        db_lock,
        ledger_id,
        leg_index=leg_index,
        target_multiplier=float(leg_index),
        target_price=100.0 + leg_index,
        planned_share=0.1,
        planned_qty=0.1,
        now=1_700_000_001,
    )
    conn.execute(
        """
        UPDATE bybit_demo_tp_legs
        SET order_id=?, order_link_id=?, status=?, executed_qty=?
        WHERE id=?
        """,
        (order_id, order_link_id, status, float(executed_qty), leg_id),
    )
    conn.commit()
    client.orders[order_id] = {
        "orderId": order_id,
        "orderLinkId": order_link_id,
        "orderStatus": status,
        "cumExecQty": str(executed_qty),
        "avgPrice": "101",
    }
    client.orders[order_link_id] = client.orders[order_id]
    return leg_id


@pytest.fixture(autouse=True)
def _enable_bybit_trading_for_existing_unit_tests(monkeypatch):
    """Keep legacy direct submit_signal tests explicit while testing the gate."""
    monkeypatch.setenv(BYBIT_DEMO_TRADING_ENABLED_ENV, "true")


def test_trading_gate_defaults_off(monkeypatch):
    monkeypatch.delenv(BYBIT_DEMO_TRADING_ENABLED_ENV, raising=False)
    assert bybit_demo.bybit_demo_trading_enabled() is False


def test_initialize_schema_adds_backward_compatible_tp_model():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE bybit_demo_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL UNIQUE,
            signal_ts INTEGER NOT NULL,
            source_demo_position_id INTEGER,
            strategy TEXT NOT NULL,
            confirmation_level TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            signal_price REAL NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            tp_price REAL NOT NULL,
            notional_usd REAL NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'intent',
            order_link_id TEXT NOT NULL UNIQUE,
            ts_created INTEGER NOT NULL
        )
        """
    )

    initialize_schema(conn)
    initialize_schema(conn)

    columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(bybit_demo_positions)")
    }
    assert columns["tp_plan_version"][2] == "TEXT"
    assert columns["atr_value"][2] == "REAL"
    assert columns["requested_tp_count"][2] == "INTEGER"
    assert columns["be_state"][4] == "'not_armed'"
    assert columns["protection_state"][4] == "'legacy_full'"
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "bybit_demo_tp_legs" in tables
    assert "bybit_demo_tp_events" in tables


def test_tp_leg_and_event_model_is_idempotent():
    conn = _db()
    lock = threading.Lock()
    ledger_id = _seed_tp_parent(conn)

    leg_id = bybit_demo._insert_tp_leg(
        conn,
        lock,
        ledger_id,
        leg_index=1,
        target_multiplier=1.0,
        target_price=105.0,
        planned_share=0.2,
        planned_qty=0.01,
        now=1_700_000_001,
    )
    first_event_id = bybit_demo._tp_event(
        conn,
        lock,
        ledger_id,
        "tp_plan_created",
        "plan:v1",
        leg_id=leg_id,
        requested_qty=0.01,
        event_ts=1_700_000_002,
        raw_order={"source": "test"},
    )
    duplicate_event_id = bybit_demo._tp_event(
        conn,
        lock,
        ledger_id,
        "tp_plan_created",
        "plan:v1",
        leg_id=leg_id,
        requested_qty=999,
        event_ts=1_700_000_003,
    )

    assert first_event_id is not None
    assert duplicate_event_id is None
    leg = conn.execute(
        """
        SELECT leg_index, target_multiplier, target_price,
               planned_share, planned_qty, executed_qty, status
        FROM bybit_demo_tp_legs WHERE id=?
        """,
        (leg_id,),
    ).fetchone()
    assert leg == (1, 1.0, 105.0, 0.2, 0.01, 0.0, "planned")
    event = conn.execute(
        """
        SELECT event_type, idempotency_key, requested_qty,
               event_ts, raw_order_json
        FROM bybit_demo_tp_events WHERE ledger_id=?
        """,
        (ledger_id,),
    ).fetchone()
    assert event == (
        "tp_plan_created",
        "plan:v1",
        0.01,
        1_700_000_002.0,
        '{"source":"test"}',
    )


def test_reconcile_tp_leg_records_partial_fill_then_filled_once():
    conn = _db()
    lock = threading.Lock()
    ledger_id = _seed_tp_parent(conn)
    leg_id = bybit_demo._insert_tp_leg(
        conn,
        lock,
        ledger_id,
        leg_index=1,
        target_multiplier=1.0,
        target_price=102.0,
        planned_share=0.2,
        planned_qty=0.2,
        now=1_700_000_001,
    )
    conn.execute(
        """
        UPDATE bybit_demo_tp_legs
        SET order_id=?, order_link_id=?
        WHERE id=?
        """,
        ("tp-order-1", "tp-link-1", leg_id),
    )
    conn.commit()
    client = PartialTpMockClient(
        [
            {
                "orderId": "tp-order-1",
                "orderLinkId": "tp-link-1",
                "orderStatus": "PartiallyFilled",
                "cumExecQty": "0.1",
                "avgPrice": "102.1",
            },
            {
                "orderId": "tp-order-1",
                "orderLinkId": "tp-link-1",
                "orderStatus": "PartiallyFilled",
                "cumExecQty": "0.1",
                "avgPrice": "102.1",
            },
            {
                "orderId": "tp-order-1",
                "orderLinkId": "tp-link-1",
                "orderStatus": "Filled",
                "cumExecQty": "0.2",
                "avgPrice": "102.2",
            },
        ],
        [
            [{"execQty": "0.1", "execFee": "0.01"}],
            [{"execQty": "0.1", "execFee": "0.01"}],
            [
                {"execQty": "0.1", "execFee": "0.01"},
                {"execQty": "0.1", "execFee": "0.01"},
            ],
        ],
    )

    first = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_010
    )
    duplicate = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_011
    )
    final = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_012
    )
    terminal = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_013
    )

    assert first["polled"] == 1
    assert first["events_created"] == 1
    assert duplicate["polled"] == 1
    assert duplicate["events_created"] == 0
    assert final["events_created"] == 1
    assert terminal["polled"] == 0
    row = conn.execute(
        """
        SELECT executed_qty, status, order_id, order_link_id,
               avg_exit_price, fee_usd, filled_ts, last_error
        FROM bybit_demo_tp_legs WHERE id=?
        """,
        (leg_id,),
    ).fetchone()
    assert row == (0.2, "filled", "tp-order-1", "tp-link-1", 102.2, 0.02, 1_700_000_012, None)
    events = conn.execute(
        """
        SELECT event_type, executed_qty, status
        FROM bybit_demo_tp_events
        WHERE ledger_id=? ORDER BY id
        """,
        (ledger_id,),
    ).fetchall()
    assert events == [
        ("tp_leg_partial_fill", 0.1, "partially_filled"),
        ("tp_leg_filled", 0.2, "filled"),
    ]
    assert client.order_calls == [
        ("BTCUSDT", "tp-order-1", None),
        ("BTCUSDT", "tp-order-1", None),
        ("BTCUSDT", "tp-order-1", None),
    ]
    assert client.create_calls == []


def test_reconcile_tp_leg_records_partial_fill_then_cancelled_terminal_event():
    conn = _db()
    lock = threading.Lock()
    ledger_id = _seed_tp_parent(conn)
    leg_id = bybit_demo._insert_tp_leg(
        conn,
        lock,
        ledger_id,
        leg_index=1,
        target_multiplier=1.0,
        target_price=102.0,
        planned_share=0.2,
        planned_qty=0.2,
        now=1_700_000_001,
    )
    conn.execute(
        """
        UPDATE bybit_demo_tp_legs
        SET order_id=?, order_link_id=?
        WHERE id=?
        """,
        ("tp-order-cancelled", "tp-link-cancelled", leg_id),
    )
    conn.commit()
    client = PartialTpMockClient(
        [
            {
                "orderId": "tp-order-cancelled",
                "orderLinkId": "tp-link-cancelled",
                "orderStatus": "PartiallyFilled",
                "cumExecQty": "0.1",
                "avgPrice": "102.1",
            },
            {
                "orderId": "tp-order-cancelled",
                "orderLinkId": "tp-link-cancelled",
                "orderStatus": "PartiallyFilledCanceled",
                "cumExecQty": "0.1",
                "avgPrice": "102.1",
            },
        ],
        [
            [{"execQty": "0.1", "execFee": "0.01"}],
            [{"execQty": "0.1", "execFee": "0.01"}],
        ],
    )

    partial = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_010
    )
    cancelled = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_011
    )
    terminal = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_012
    )

    assert partial["events_created"] == 1
    assert cancelled["events_created"] == 1
    assert terminal["polled"] == 0
    assert conn.execute(
        """
        SELECT executed_qty, status, fee_usd, cancelled_ts
        FROM bybit_demo_tp_legs WHERE id=?
        """,
        (leg_id,),
    ).fetchone() == (0.1, "cancelled", 0.01, 1_700_000_011)
    assert conn.execute(
        """
        SELECT event_type, executed_qty, status
        FROM bybit_demo_tp_events
        WHERE ledger_id=? ORDER BY id
        """,
        (ledger_id,),
    ).fetchall() == [
        ("tp_leg_partial_fill", 0.1, "partially_filled"),
        ("tp_leg_cancelled", 0.1, "cancelled"),
    ]


def test_reconcile_tp_leg_keeps_planned_state_when_order_is_missing():
    conn = _db()
    lock = threading.Lock()
    ledger_id = _seed_tp_parent(conn)
    leg_id = bybit_demo._insert_tp_leg(
        conn,
        lock,
        ledger_id,
        leg_index=1,
        target_multiplier=1.0,
        target_price=102.0,
        planned_share=0.2,
        planned_qty=0.2,
        now=1_700_000_001,
    )
    conn.execute(
        "UPDATE bybit_demo_tp_legs SET order_link_id=? WHERE id=?",
        ("tp-link-missing", leg_id),
    )
    conn.commit()
    client = PartialTpMockClient([None])

    result = bybit_demo.reconcile_tp_legs(
        conn, lock, client, ledger_id=ledger_id, now=1_700_000_010
    )

    assert result == {
        "status": "ok",
        "polled": 1,
        "updated": 0,
        "events_created": 0,
        "errors": 1,
        "successful_requests": 1,
    }
    assert conn.execute(
        "SELECT status, executed_qty, last_error FROM bybit_demo_tp_legs WHERE id=?",
        (leg_id,),
    ).fetchone() == ("planned", 0.0, "tp_order_not_found")
    assert conn.execute(
        "SELECT COUNT(*) FROM bybit_demo_tp_events WHERE ledger_id=?",
        (ledger_id,),
    ).fetchone()[0] == 0


def test_reversal_tp_cancel_is_best_effort_and_blocks_close_on_ambiguity():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "1",
        "positionValue": "100",
        "unrealisedPnl": "0",
    }]
    ledger_id = _seed_tp_parent(conn)
    leg_ids = [
        _seed_tp_leg_order(
            conn,
            lock,
            ledger_id,
            client,
            leg_index=index,
            order_id=f"tp-order-{index}",
            order_link_id=f"tp-link-{index}",
        )
        for index in range(1, 4)
    ]
    client.cancel_plan = ["ambiguous", "cancel", "cancel"]
    reversal_id = bybit_demo._claim_reversal(
        conn,
        lock,
        symbol="BTCUSDT",
        source_signal_key="reversal-best-effort",
        source_direction="LONG",
        target_direction="SHORT",
        source_ledger_ids=[ledger_id],
        now=int(time.time()),
    )
    assert reversal_id is not None

    result = bybit_demo._run_reversal_close(
        conn,
        lock,
        client,
        reversal_id=reversal_id,
        symbol="BTCUSDT",
        source_direction="LONG",
        source_ledger_ids=[ledger_id],
    )

    assert result == "recovery_required"
    assert len(client.cancel_calls) == 3
    assert client.close_calls == []
    assert [item[0] for item in client.call_log] == ["cancel", "cancel", "cancel"]
    assert conn.execute(
        """
        SELECT status FROM bybit_demo_tp_legs
        WHERE id IN (?, ?, ?) ORDER BY id
        """,
        tuple(leg_ids),
    ).fetchall() == [
        ("open",),
        ("cancelled",),
        ("cancelled",),
    ]
    assert conn.execute(
        """
        SELECT tp_legs_cancelled, tp_cancel_errors, state, recovery_reason
        FROM bybit_demo_reversals WHERE id=?
        """,
        (reversal_id,),
    ).fetchone() == (2, 2, "RECOVERY_REQUIRED", "tp_cancel_recovery_required")


def test_reversal_cancel_already_terminal_is_expected_and_reconciled():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    ledger_id = _seed_tp_parent(conn)
    leg_id = _seed_tp_leg_order(
        conn,
        lock,
        ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-already-cancelled",
        order_link_id="tp-link-already-cancelled",
        status="Cancelled",
        executed_qty="0",
    )
    conn.execute(
        "UPDATE bybit_demo_tp_legs SET status='open' WHERE id=?",
        (leg_id,),
    )
    conn.commit()
    client.cancel_plan = ["already_terminal"]

    result = bybit_demo._cancel_tp_legs_for_reversal(
        conn,
        lock,
        client,
        reversal_id=1,
        source_ledger_ids=[ledger_id],
        now=1_700_000_010,
    )

    assert result["status"] == "ok"
    assert result["cancel_attempts"] == 1
    assert result["tp_cancel_errors"] == 0
    assert result["tp_legs_cancelled"] == 1
    assert conn.execute(
        "SELECT status FROM bybit_demo_tp_legs WHERE id=?",
        (leg_id,),
    ).fetchone() == ("cancelled",)
    second = bybit_demo._cancel_tp_legs_for_reversal(
        conn,
        lock,
        client,
        reversal_id=1,
        source_ledger_ids=[ledger_id],
        now=1_700_000_011,
    )
    assert second["cancel_attempts"] == 0
    assert len(client.cancel_calls) == 1


def test_reversal_cancel_race_to_filled_is_terminal_and_close_continues():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "1",
        "positionValue": "100",
        "unrealisedPnl": "0",
    }]
    ledger_id = _seed_tp_parent(conn)
    _seed_tp_leg_order(
        conn,
        lock,
        ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-race",
        order_link_id="tp-link-race",
        status="PartiallyFilled",
        executed_qty="0.1",
    )
    client.cancel_plan = ["filled"]
    reversal_id = bybit_demo._claim_reversal(
        conn,
        lock,
        symbol="BTCUSDT",
        source_signal_key="reversal-race",
        source_direction="LONG",
        target_direction="SHORT",
        source_ledger_ids=[ledger_id],
        now=int(time.time()),
    )
    assert reversal_id is not None

    result = bybit_demo._run_reversal_close(
        conn,
        lock,
        client,
        reversal_id=reversal_id,
        symbol="BTCUSDT",
        source_direction="LONG",
        source_ledger_ids=[ledger_id],
    )

    assert result == "closed"
    assert client.close_calls
    assert client.close_calls[0]["qty"] == pytest.approx(0.9)
    assert [item[0] for item in client.call_log] == ["cancel", "close"]
    assert conn.execute(
        "SELECT status FROM bybit_demo_tp_legs WHERE ledger_id=?",
        (ledger_id,),
    ).fetchone() == ("filled",)


def test_reversal_tp_full_race_continues_as_normal_target_entry():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    source_ledger_id = _seed_open_long(conn, lock, client, source_id=904)
    client.entry_calls.clear()
    _seed_tp_leg_order(
        conn,
        lock,
        source_ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-full-race",
        order_link_id="tp-link-full-race",
        status="PartiallyFilled",
        executed_qty="1",
    )
    client.cancel_plan = ["filled"]

    result = _submit_opposite(conn, lock, client, signal_ts=1_700_000_300)

    assert result["status"] == "submitted"
    assert result.get("reversal_id") is None
    assert client.close_calls == []
    assert len(client.entry_calls) == 1
    assert client.entry_calls[0]["direction"] == "SHORT"
    assert conn.execute(
        "SELECT origin, reversal_id FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == ("signal", None)
    assert conn.execute(
        "SELECT state FROM bybit_demo_reversals"
    ).fetchone() == ("CLOSED",)


def test_reversal_recovers_when_direction_changes_after_tp_cancel():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    ledger_id = _seed_tp_parent(conn)
    _seed_tp_leg_order(
        conn,
        lock,
        ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-direction-race",
        order_link_id="tp-link-direction-race",
    )
    client.position_plan = [
        [{
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "1",
            "positionValue": "100",
            "unrealisedPnl": "0",
        }],
        [{
            "symbol": "BTCUSDT",
            "side": "Sell",
            "size": "1",
            "positionValue": "100",
            "unrealisedPnl": "0",
        }],
    ]
    reversal_id = bybit_demo._claim_reversal(
        conn,
        lock,
        symbol="BTCUSDT",
        source_signal_key="reversal-direction-race",
        source_direction="LONG",
        target_direction="SHORT",
        source_ledger_ids=[ledger_id],
        now=int(time.time()),
    )
    assert reversal_id is not None

    result = bybit_demo._run_reversal_close(
        conn,
        lock,
        client,
        reversal_id=reversal_id,
        symbol="BTCUSDT",
        source_direction="LONG",
        source_ledger_ids=[ledger_id],
    )

    assert result == "recovery_required"
    assert client.close_calls == []
    assert conn.execute(
        "SELECT state, recovery_reason FROM bybit_demo_reversals WHERE id=?",
        (reversal_id,),
    ).fetchone() == (
        "RECOVERY_REQUIRED",
        "live_position_direction_changed_after_tp_cancel",
    )


def test_reversal_recovers_from_unexpected_post_tp_position_error():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    ledger_id = _seed_tp_parent(conn)
    _seed_tp_leg_order(
        conn,
        lock,
        ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-unexpected-position",
        order_link_id="tp-link-unexpected-position",
    )
    client.position_plan = [
        [{
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "1",
            "positionValue": "100",
            "unrealisedPnl": "0",
        }],
        RuntimeError("unexpected_position_payload"),
    ]
    reversal_id = bybit_demo._claim_reversal(
        conn,
        lock,
        symbol="BTCUSDT",
        source_signal_key="reversal-unexpected-position",
        source_direction="LONG",
        target_direction="SHORT",
        source_ledger_ids=[ledger_id],
        now=int(time.time()),
    )
    assert reversal_id is not None

    result = bybit_demo._run_reversal_close(
        conn,
        lock,
        client,
        reversal_id=reversal_id,
        symbol="BTCUSDT",
        source_direction="LONG",
        source_ledger_ids=[ledger_id],
    )

    assert result == "recovery_required"
    assert client.close_calls == []
    assert conn.execute(
        "SELECT state, recovery_reason FROM bybit_demo_reversals WHERE id=?",
        (reversal_id,),
    ).fetchone() == (
        "RECOVERY_REQUIRED",
        "close_position_read_after_tp_cancel_failed:unexpected_position_payload",
    )


def test_reversal_tp_cancel_isolated_to_source_ledgers():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    source_ledger_id = _seed_tp_parent(conn)
    unrelated_ledger_id = _seed_tp_parent(
        conn,
        signal_key="tp-parent-unrelated",
        symbol="ETHUSDT",
        order_link_id="bd-tp-parent-unrelated",
    )
    source_leg_id = _seed_tp_leg_order(
        conn,
        lock,
        source_ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-source",
        order_link_id="tp-link-source",
    )
    unrelated_leg_id = _seed_tp_leg_order(
        conn,
        lock,
        unrelated_ledger_id,
        client,
        leg_index=1,
        order_id="tp-order-unrelated",
        order_link_id="tp-link-unrelated",
    )

    result = bybit_demo._cancel_tp_legs_for_reversal(
        conn,
        lock,
        client,
        reversal_id=1,
        source_ledger_ids=[source_ledger_id],
        now=1_700_000_010,
    )

    assert result["status"] == "ok"
    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["order_id"] == "tp-order-source"
    assert conn.execute(
        "SELECT status FROM bybit_demo_tp_legs WHERE id=?",
        (source_leg_id,),
    ).fetchone() == ("cancelled",)
    assert conn.execute(
        "SELECT status FROM bybit_demo_tp_legs WHERE id=?",
        (unrelated_leg_id,),
    ).fetchone() == ("New",)


def test_atr_provenance_freezes_wilder_4h_snapshot():
    snapshot = bybit_demo.atr_provenance(2.5, 1_700_000_000)

    assert snapshot == {
        "atr_value": 2.5,
        "atr_period": 14,
        "atr_timeframe": "4h",
        "atr_method": "wilder",
        "atr_candle_close_ts": 1_700_000_000,
        "atr_source": "gateio_4h",
    }
    assert bybit_demo.atr_provenance(None) == {
        "atr_value": None,
        "atr_period": None,
        "atr_timeframe": None,
        "atr_method": None,
        "atr_candle_close_ts": None,
        "atr_source": "fixed_fallback",
    }


@pytest.mark.parametrize(
    ("atr_value", "candle_ts"),
    [(0, 1_700_000_000), (-1, 1_700_000_000), (1, None), (None, 1_700_000_000)],
)
def test_atr_provenance_rejects_ambiguous_snapshots(atr_value, candle_ts):
    with pytest.raises(BybitDemoSizingError):
        bybit_demo.atr_provenance(atr_value, candle_ts)


def test_multi_tp_default_requests_five_levels():
    assert bybit_demo.BYBIT_DEMO_TP_COUNT == 5


def test_multi_tp_plan_n3_long_uses_atr_levels_and_increasing_split():
    plan = bybit_demo.calculate_multi_tp_plan(
        direction="LONG",
        entry_price=100,
        sl_price=95,
        atr_value=2,
        executed_qty="1.00",
        min_order_qty="0.01",
        qty_step="0.01",
        tick_size="0.1",
        tp_count=3,
    )

    assert plan["atr_value"] == 2.0
    assert plan["effective_tp_count"] == 3
    assert plan["last_fallback_reason"] is None
    assert [leg["target_multiplier"] for leg in plan["legs"]] == [1.0, 2.0, 3.0]
    assert [leg["target_price"] for leg in plan["legs"]] == [102.0, 104.0, 106.0]
    assert [leg["planned_share"] for leg in plan["legs"]] == [0.2, 0.3, 0.5]
    assert [leg["planned_qty"] for leg in plan["legs"]] == [0.2, 0.3, 0.5]


def test_multi_tp_plan_and_provenance_use_the_same_atr_value():
    snapshot = bybit_demo.atr_provenance(2.25, 1_700_000_000)
    plan = bybit_demo.calculate_multi_tp_plan(
        direction="LONG",
        entry_price=100,
        sl_price=95,
        atr_value=snapshot["atr_value"],
        executed_qty="1.00",
        min_order_qty="0.01",
        qty_step="0.01",
        tick_size="0.01",
        tp_count=3,
    )

    assert plan["atr_value"] == snapshot["atr_value"] == 2.25
    assert [leg["target_price"] for leg in plan["legs"]] == [
        102.25,
        104.5,
        106.75,
    ]


def test_multi_tp_plan_n5_short_uses_atr_levels_and_increasing_split():
    plan = bybit_demo.calculate_multi_tp_plan(
        direction="SHORT",
        entry_price=100,
        sl_price=104,
        atr_value=2,
        executed_qty="2.00",
        min_order_qty="0.01",
        qty_step="0.01",
        tick_size="0.1",
        tp_count=5,
    )

    assert plan["atr_value"] == 2.0
    assert [leg["target_multiplier"] for leg in plan["legs"]] == [
        1.0, 1.5, 2.0, 2.5, 3.0,
    ]
    assert [leg["target_price"] for leg in plan["legs"]] == [
        98.0, 97.0, 96.0, 95.0, 94.0,
    ]
    assert [leg["planned_share"] for leg in plan["legs"]] == [
        0.1, 0.15, 0.2, 0.25, 0.3,
    ]
    assert [leg["planned_qty"] for leg in plan["legs"]] == [
        0.2, 0.3, 0.4, 0.5, 0.6,
    ]


def test_multi_tp_plan_falls_back_from_n5_to_n3_for_minimum_quantity():
    plan = bybit_demo.calculate_multi_tp_plan(
        direction="LONG",
        entry_price=100,
        sl_price=95,
        atr_value=2,
        executed_qty="0.10",
        min_order_qty="0.02",
        qty_step="0.01",
        tick_size="0.1",
        tp_count=5,
    )

    assert plan["requested_tp_count"] == 5
    assert plan["effective_tp_count"] == 3
    assert plan["last_fallback_reason"] == "min_order_qty"
    assert plan["requested_split"] == [0.1, 0.15, 0.2, 0.25, 0.3]
    assert plan["effective_split"] == [0.2, 0.3, 0.5]
    assert sum(leg["planned_qty"] for leg in plan["legs"]) == pytest.approx(0.10)


def test_multi_tp_plan_rounding_assigns_residual_to_farthest_leg():
    plan = bybit_demo.calculate_multi_tp_plan(
        direction="LONG",
        entry_price=10,
        sl_price=9,
        atr_value=0.5,
        executed_qty="0.11",
        min_order_qty="0.01",
        qty_step="0.01",
        tick_size="0.01",
        tp_count=3,
    )

    assert [leg["planned_qty"] for leg in plan["legs"]] == [0.02, 0.03, 0.06]
    assert sum(leg["planned_qty"] for leg in plan["legs"]) == pytest.approx(0.11)


@pytest.mark.parametrize(
    ("direction", "sl_price", "expected_prices"),
    [
        ("LONG", "99.94", [100.1, 100.2]),
        ("SHORT", "100.06", [99.9, 99.8]),
    ],
)
def test_multi_tp_plan_coarse_tick_falls_back_without_duplicate_levels(
    direction, sl_price, expected_prices
):
    plan = bybit_demo.calculate_multi_tp_plan(
        direction=direction,
        entry_price="100",
        sl_price=sl_price,
        atr_value="0.06",
        executed_qty="1",
        min_order_qty="0.01",
        qty_step="0.01",
        tick_size="0.1",
        tp_count=5,
    )

    assert plan["effective_tp_count"] == 2
    assert plan["last_fallback_reason"] == "tick_size"
    assert [leg["target_price"] for leg in plan["legs"]] == expected_prices


def _multi_tp_signal_kwargs():
    return {
        "strategy": "overheated_24h",
        "confirmation_level": None,
        "source_demo_position_id": 1_700_001_001,
        "signal_ts": 1_700_001_001,
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "signal_price": 100,
        "entry_price": 100,
        "sl_price": 95,
        "tp_price": 110,
        "source_is_shadow": False,
        "atr_value": 2,
        "atr_candle_close_ts": 1_700_000_000,
    }


def test_multi_tp_entry_uses_native_sl_and_places_five_reduce_only_legs(
    monkeypatch,
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient()

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())

    assert result == {
        "status": "submitted",
        "ledger_id": result["ledger_id"],
        "tp_setup": "armed",
    }
    entry = client.create_calls[0]
    assert entry["take_profit"] is None
    assert entry["stop_loss"] == pytest.approx(95.0)
    assert len(client.tp_calls) == 5
    assert [call["direction"] for call in client.tp_calls] == ["LONG"] * 5
    assert [call["qty"] for call in client.tp_calls] == pytest.approx(
        [0.075, 0.112, 0.15, 0.187, 0.226]
    )
    assert [call["price"] for call in client.tp_calls] == pytest.approx(
        [102.0, 103.0, 104.0, 105.0, 106.0]
    )
    assert len({call["order_link_id"] for call in client.tp_calls}) == 5
    row = conn.execute(
        """
        SELECT protection_state, tp_plan_version, requested_tp_count,
               effective_tp_count, atr_value, atr_candle_close_ts
        FROM bybit_demo_positions WHERE id=?
        """,
        (result["ledger_id"],),
    ).fetchone()
    assert row == ("armed", "atr_v1", 5, 5, 2.0, 1_700_000_000)
    assert conn.execute(
        "SELECT COUNT(*), SUM(planned_qty) FROM bybit_demo_tp_legs WHERE ledger_id=?",
        (result["ledger_id"],),
    ).fetchone() == (5, pytest.approx(0.75))
    assert conn.execute(
        "SELECT event_type, reason FROM bybit_demo_tp_events "
        "WHERE ledger_id=? AND event_type='plan_created'",
        (result["ledger_id"],),
    ).fetchone() == ("plan_created", None)


def test_multi_tp_client_uses_reduce_only_limit_wire_payload():
    session = RecordingSession({"retCode": 0, "result": {"orderId": "tp-wire"}})
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        session=session,
        clock=lambda: 1_700_000_000,
    )

    result = client.create_limit_tp_order(
        symbol="BTCUSDT",
        direction="LONG",
        qty=0.25,
        price=102.0,
        order_link_id="btp-wire",
    )

    assert result == {"orderId": "tp-wire"}
    body = json.loads(session.calls[0][2]["data"])
    assert body == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "orderType": "Limit",
        "qty": "0.25",
        "price": "102",
        "timeInForce": "GTC",
        "positionIdx": 0,
        "reduceOnly": True,
        "orderLinkId": "btp-wire",
    }


def test_multi_tp_entry_wire_keeps_sl_only_native_protection():
    session = RecordingSession({"retCode": 0, "result": {"orderId": "entry-wire"}})
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        session=session,
        clock=lambda: 1_700_000_000,
    )

    client.create_market_order(
        symbol="BTCUSDT",
        direction="LONG",
        qty=0.5,
        take_profit=None,
        stop_loss=95,
        order_link_id="entry-wire",
    )

    body = json.loads(session.calls[0][2]["data"])
    assert body["stopLoss"] == "95"
    assert body["tpslMode"] == "Full"
    assert "takeProfit" not in body
    assert "tpOrderType" not in body


def test_multi_tp_flag_off_keeps_legacy_native_single_tp(monkeypatch):
    monkeypatch.delenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, raising=False)
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient()

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())

    assert result["status"] == "submitted"
    assert "tp_setup" not in result
    assert client.create_calls[0]["take_profit"] == pytest.approx(110.0)
    assert client.tp_calls == []


def test_multi_tp_deterministic_reject_is_not_blindly_retried(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(
        tp_plan=["rejected", "created", "created", "created", "created"]
    )

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    assert result["tp_setup"] == "manual_recovery_required"
    first_attempt_count = len(client.tp_calls)

    pending = ensure_pending_tp_orders(conn, lock, client)

    assert pending == {"status": "ok", "processed": 0, "armed": 0}
    assert len(client.tp_calls) == first_attempt_count
    assert conn.execute(
        "SELECT protection_state, last_error FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == (
        "recovery_required_manual",
        "tp_leg_deterministic_reject_requires_manual_recovery",
    )


def test_manual_tp_recovery_bypasses_deadline_and_skips_rejected_leg(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(tp_plan=["rejected", "created", "created"])

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    conn.execute(
        "UPDATE bybit_demo_positions SET ts_submitted=? WHERE id=?",
        (1_700_000_000, result["ledger_id"]),
    )
    missing_leg = conn.execute(
        """
        SELECT order_link_id FROM bybit_demo_tp_legs
        WHERE ledger_id=? AND leg_index=2
        """,
        (result["ledger_id"],),
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE bybit_demo_tp_legs
        SET status='planned', order_id=NULL
        WHERE ledger_id=? AND leg_index=2
        """,
        (result["ledger_id"],),
    )
    client.tp_orders.pop(missing_leg)
    client.tp_plan = ["created"]
    conn.commit()

    recovery = manual_recover_tp_orders(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        action="retry_all",
        now=1_700_000_061,
    )

    assert recovery["status"] == "recovery_required_manual"
    assert len(client.tp_calls) == 6
    assert conn.execute(
        "SELECT protection_state FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == ("recovery_required_manual",)
    assert conn.execute(
        "SELECT status, order_id FROM bybit_demo_tp_legs "
        "WHERE ledger_id=? AND leg_index=1",
        (result["ledger_id"],),
    ).fetchone() == ("rejected", None)
    assert conn.execute(
        "SELECT status, order_id FROM bybit_demo_tp_legs "
        "WHERE ledger_id=? AND leg_index=2",
        (result["ledger_id"],),
    ).fetchone() == ("open", "tp-6")


def test_manual_tp_accept_partial_keeps_existing_legs_in_reconciliation(
    monkeypatch,
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(
        tp_plan=["rejected", "created", "created", "created", "created"]
    )

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    accepted = manual_recover_tp_orders(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        action="accept_partial",
    )
    repeated = manual_recover_tp_orders(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        action="accept_partial",
    )

    assert accepted["status"] == "armed_partial_manual"
    assert repeated["status"] == "armed_partial_manual"
    assert conn.execute(
        "SELECT protection_state FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == ("armed_partial_manual",)
    reconciled = bybit_demo.reconcile_tp_legs(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        now=1_700_000_100,
    )
    assert reconciled["polled"] == 5
    assert reconciled["successful_requests"] >= 5


def test_manual_tp_abandon_is_terminal_and_idempotent(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(
        tp_plan=["rejected", "created", "created", "created", "created"]
    )

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    abandoned = manual_recover_tp_orders(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        action="abandon",
        reason="operator chose native SL only",
    )
    repeated = manual_recover_tp_orders(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        action="abandon",
    )

    assert abandoned["status"] == "recovery_abandoned"
    assert repeated["status"] == "recovery_abandoned"
    assert conn.execute(
        "SELECT protection_state, last_error "
        "FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == (
        "recovery_abandoned",
        "operator chose native SL only",
    )


def test_manual_tp_recovery_snapshot_contains_legs_without_mutation(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(
        tp_plan=["rejected", "created", "created", "created", "created"]
    )

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    before = conn.execute(
        "SELECT protection_state FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone()
    snapshot = manual_tp_recovery_snapshot(conn, lock)

    assert snapshot["status"] == "ok"
    assert snapshot["rows"][0]["ledger_id"] == result["ledger_id"]
    assert len(snapshot["rows"][0]["legs"]) == 5
    assert conn.execute(
        "SELECT protection_state FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == before


def test_manual_tp_recovery_http_actions_use_dedicated_header_and_body(
    monkeypatch, caplog
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    token = "recovery-test-secret"
    monkeypatch.setenv(app.BYBIT_DEMO_TP_RECOVERY_TOKEN_ENV, token)
    app._tp_recovery_auth_failures.clear()
    conn = _db()
    lock = threading.Lock()
    trading_client = MultiTpTradingClient(tp_plan=["rejected", "created", "created"])
    result = submit_signal(
        conn,
        lock,
        trading_client,
        **_multi_tp_signal_kwargs(),
    )
    monkeypatch.setattr(app, "_get_db", lambda: conn)
    monkeypatch.setattr(app, "_db_lock", lock)
    monkeypatch.setattr(
        app.BybitDemoClient,
        "from_env",
        staticmethod(lambda: trading_client),
    )

    http = app.app.test_client()
    with caplog.at_level("WARNING"):
        unauthorized = http.get(
            "/bot-api/bybit-demo/tp-recovery",
            headers={"X-Bybit-TP-Recovery-Token": "wrong"},
        )
    assert unauthorized.status_code == 401
    assert token not in caplog.text

    invalid_unicode = http.get(
        "/bot-api/bybit-demo/tp-recovery",
        headers={
            "X-Bybit-TP-Recovery-Token": "невалидный-токен",
        },
    )
    assert invalid_unicode.status_code == 401

    snapshot = http.get(
        "/bot-api/bybit-demo/tp-recovery",
        headers={"X-Bybit-TP-Recovery-Token": token},
    )
    assert snapshot.status_code == 200
    assert snapshot.get_json()["rows"][0]["ledger_id"] == result["ledger_id"]

    response = http.post(
        "/bot-api/bybit-demo/tp-recovery",
        headers={"X-Bybit-TP-Recovery-Token": token},
        json={
            "ledger_id": result["ledger_id"],
            "action": "accept_partial",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "armed_partial_manual"


def test_manual_tp_recovery_auth_rate_limit_does_not_log_token(monkeypatch, caplog):
    token = "another-recovery-secret"
    monkeypatch.setenv(app.BYBIT_DEMO_TP_RECOVERY_TOKEN_ENV, token)
    app._tp_recovery_auth_failures.clear()
    http = app.app.test_client()

    with caplog.at_level("WARNING"):
        responses = [
            http.get(
                "/bot-api/bybit-demo/tp-recovery",
                headers={"X-Bybit-TP-Recovery-Token": "wrong"},
            )
            for _ in range(app.BYBIT_DEMO_TP_RECOVERY_MAX_AUTH_FAILURES + 1)
        ]

    assert [response.status_code for response in responses[:-1]] == [
        401
    ] * app.BYBIT_DEMO_TP_RECOVERY_MAX_AUTH_FAILURES
    assert responses[-1].status_code == 429
    assert token not in caplog.text
    app._tp_recovery_auth_failures.clear()


def test_multi_tp_post_entry_lookup_error_does_not_reject_entry(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(
        entry_status="New",
        entry_lookup_error=BybitDemoError(
            "/v5/order/realtime",
            "server_timeout",
            retryable=True,
            transport=True,
        )
    )

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())

    assert result["status"] == "submitted"
    assert result["tp_setup"] == "recovery_required"
    assert len(client.tp_calls) == 0
    assert conn.execute(
        "SELECT status, protection_state, last_error "
        "FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == (
        "submitted",
        "recovery_required",
        "tp_setup_post_entry_error:BybitDemoError:server_timeout",
    )


@pytest.mark.parametrize(
    ("ret_code", "expected"),
    [
        (170136, "rejected"),
        (110023, "rejected"),
        (110072, "duplicate"),
        (170141, "duplicate"),
        (10000, "ambiguous"),
    ],
)
def test_multi_tp_create_error_taxonomy(ret_code, expected):
    error = BybitDemoError(
        "/v5/order/create",
        f"bybit_ret_code_{ret_code}",
        ret_code=ret_code,
    )

    assert bybit_demo._tp_create_error_kind(error) == expected


def test_multi_tp_plan_failure_keeps_entry_with_sl_and_no_tp(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient()
    args = _multi_tp_signal_kwargs()
    args["atr_value"] = None
    args["atr_candle_close_ts"] = None

    result = submit_signal(conn, lock, client, **args)

    assert result["status"] == "submitted"
    assert result["tp_setup"] == "tp_plan_failed"
    assert len(client.tp_calls) == 0
    assert client.create_calls[0]["take_profit"] is None
    assert conn.execute(
        "SELECT status, protection_state, last_error "
        "FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == (
        "submitted",
        "tp_plan_failed",
        "tp_plan_failed:invalid atr_value",
    )


def test_multi_tp_pending_entry_is_placed_on_next_hook(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(entry_status="New")

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())

    assert result["status"] == "submitted"
    assert result["tp_setup"] == "awaiting_entry_fill"
    assert client.tp_calls == []
    client.entry_order["orderStatus"] = "Filled"
    client.entry_order["cumExecQty"] = "0.5"
    client.entry_order["avgPrice"] = "100"
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "0.5",
        "positionValue": "50",
        "unrealisedPnl": "0",
    }]

    pending = ensure_pending_tp_orders(conn, lock, client)

    assert pending == {"status": "ok", "processed": 1, "armed": 1}
    assert len(client.tp_calls) == 5
    assert conn.execute(
        "SELECT protection_state FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == ("armed",)


def test_multi_tp_ambiguous_leg_retries_to_completion_without_duplicate(
    monkeypatch,
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(
        tp_plan=["created", "ambiguous", "created", "created", "created"]
    )

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())

    assert result["status"] == "submitted"
    assert result["tp_setup"] == "recovery_required"
    assert len(client.tp_calls) == 5
    first_link = client.tp_calls[0]["order_link_id"]
    second_link = client.tp_calls[1]["order_link_id"]
    third_link = client.tp_calls[2]["order_link_id"]
    fourth_link = client.tp_calls[3]["order_link_id"]
    fifth_link = client.tp_calls[4]["order_link_id"]
    assert first_link != second_link != third_link

    client.tp_plan = ["created"]
    pending = ensure_pending_tp_orders(conn, lock, client)

    assert pending == {"status": "ok", "processed": 1, "armed": 1}
    assert [call["order_link_id"] for call in client.tp_calls] == [
        first_link,
        second_link,
        third_link,
        fourth_link,
        fifth_link,
        second_link,
    ]
    assert conn.execute(
        "SELECT protection_state FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == ("armed",)


def test_multi_tp_trail_last_leg_skips_fixed_order_for_final_leg(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_TP_TRAIL_LAST_LEG_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient()

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())

    assert result["tp_setup"] == "armed"
    # Legs 1-4 place fixed reduce-only orders exactly as without the flag;
    # leg 5 (the highest ATR multiplier, largest share) gets none.
    assert len(client.tp_calls) == 4
    assert [call["price"] for call in client.tp_calls] == pytest.approx(
        [102.0, 103.0, 104.0, 105.0]
    )

    ledger_id = result["ledger_id"]
    assert conn.execute(
        "SELECT trail_floor FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == (106.0,)

    legs = conn.execute(
        "SELECT leg_index, status, order_id FROM bybit_demo_tp_legs "
        "WHERE ledger_id=? ORDER BY leg_index",
        (ledger_id,),
    ).fetchall()
    assert [leg[0] for leg in legs] == [1, 2, 3, 4, 5]
    assert legs[-1][1] == "trailing"
    assert legs[-1][2] is None
    # reconcile_tp_legs must not chase a leg that was never placed: it
    # would otherwise query a nonexistent order and mark it errored on every
    # poll, indefinitely.
    bybit_demo.reconcile_tp_legs(conn, lock, client, ledger_id=ledger_id)
    trailing_leg = conn.execute(
        "SELECT status, last_error FROM bybit_demo_tp_legs "
        "WHERE ledger_id=? AND leg_index=5",
        (ledger_id,),
    ).fetchone()
    assert trailing_leg == ("trailing", None)


def test_multi_tp_trail_last_leg_hands_off_to_maintain_trailing_stops(
    monkeypatch,
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_TP_TRAIL_LAST_LEG_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient()

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    assert result["tp_setup"] == "armed"

    # Price has run past the last leg's 106.0 target; maintain_trailing_stops
    # must pick up the floor _ensure_tp_orders_for_ledger set and move the
    # exchange stop there, exactly as it would for a standalone
    # BYBIT_DEMO_TRAIL_PAST_TP row.
    client.positions[0]["markPrice"] = "108"

    outcome = bybit_demo.maintain_trailing_stops(conn, lock, client)

    assert outcome["moved"] == 1
    assert client.trading_stop_calls == [{"symbol": "BTCUSDT", "stop_loss": 106.0}]


def test_multi_tp_deadline_stops_automatic_placement(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient(entry_status="New")

    result = submit_signal(conn, lock, client, **_multi_tp_signal_kwargs())
    conn.execute(
        "UPDATE bybit_demo_positions SET ts_submitted=? WHERE id=?",
        (1_700_000_000, result["ledger_id"]),
    )
    conn.commit()

    pending = bybit_demo._ensure_tp_orders_for_ledger(
        conn,
        lock,
        client,
        ledger_id=result["ledger_id"],
        now=1_700_000_061,
    )

    assert pending["status"] == "recovery_required"
    assert client.tp_calls == []
    assert conn.execute(
        "SELECT protection_state, last_error FROM bybit_demo_positions WHERE id=?",
        (result["ledger_id"],),
    ).fetchone() == ("recovery_required", "tp_setup_deadline_exceeded")


@pytest.mark.parametrize(
    ("strategy", "confirmation"),
    [
        ("overheated_24h", None),
        ("overheated_confirmed", "1/3"),
    ],
)
def test_multi_tp_keeps_whitelist_and_reserve_preflight(
    monkeypatch, strategy, confirmation
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = MultiTpTradingClient()
    args = _multi_tp_signal_kwargs()
    args.update(
        strategy=strategy,
        confirmation_level=confirmation,
        source_demo_position_id=1_700_001_002,
        signal_ts=1_700_001_002,
    )

    result = submit_signal(conn, lock, client, **args)

    assert result["status"] == "submitted"
    row = conn.execute(
        """
        SELECT preflight_decision, preflight_new_notional_usd,
               preflight_max_exposure_usd, preflight_equity_reserve_usd
        FROM bybit_demo_positions WHERE id=?
        """,
        (result["ledger_id"],),
    ).fetchone()
    assert row[0] == "allow"
    assert row[1] == pytest.approx(75.0)
    assert row[2] == pytest.approx(4000.0)
    assert row[3] == pytest.approx(100.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "direction": "LONG",
            "entry_price": 100,
            "sl_price": 100,
            "atr_value": 1,
            "executed_qty": 1,
            "min_order_qty": 0.01,
            "qty_step": 0.01,
            "tick_size": 0.1,
            "tp_count": 3,
        },
        {
            "direction": "LONG",
            "entry_price": 100,
            "sl_price": 95,
            "atr_value": 1,
            "executed_qty": 0.105,
            "min_order_qty": 0.01,
            "qty_step": 0.01,
            "tick_size": 0.1,
            "tp_count": 3,
        },
        {
            "direction": "LONG",
            "entry_price": 100,
            "sl_price": 95,
            "atr_value": 1,
            "executed_qty": 0.01,
            "min_order_qty": 0.02,
            "qty_step": 0.01,
            "tick_size": 0.1,
            "tp_count": 3,
        },
        {
            "direction": "LONG",
            "entry_price": 100,
            "sl_price": 95,
            "atr_value": 0,
            "executed_qty": 1,
            "min_order_qty": 0.01,
            "qty_step": 0.01,
            "tick_size": 0.1,
            "tp_count": 3,
        },
    ],
)
def test_multi_tp_plan_rejects_unsafe_inputs(kwargs):
    with pytest.raises(BybitDemoSizingError):
        bybit_demo.calculate_multi_tp_plan(**kwargs)


def test_trading_gate_explicit_enable_for_published(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_TRADING_ENABLED_ENV, "true")
    assert bybit_demo.bybit_demo_trading_enabled() is True


@pytest.mark.parametrize("value", ["maybe", "2", "truthy"])
def test_trading_gate_invalid_value_is_disabled(monkeypatch, value):
    monkeypatch.setenv(BYBIT_DEMO_TRADING_ENABLED_ENV, value)
    assert bybit_demo.bybit_demo_trading_enabled() is False


def test_disabled_signal_never_calls_bybit_or_creates_ledger_row(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_TRADING_ENABLED_ENV, "false")
    conn = _db()
    db_lock = threading.Lock()
    client = FakeTradingClient()

    result = submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=901,
        signal_ts=1_700_000_000,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )

    assert result == {"status": "disabled", "reason": "trading_disabled"}
    assert client.create_calls == []
    assert conn.execute("SELECT COUNT(*) FROM bybit_demo_positions").fetchone()[0] == 0


def test_disabled_opposite_signal_never_closes_or_reopens_bybit_position(monkeypatch):
    conn = _db()
    db_lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    source_id = 902
    _seed_open_long(conn, db_lock, client, source_id=source_id)
    client.close_calls.clear()
    client.entry_calls.clear()
    monkeypatch.setenv(BYBIT_DEMO_TRADING_ENABLED_ENV, "false")

    result = _submit_opposite(conn, db_lock, client, signal_ts=1_700_000_100)

    assert result == {"status": "disabled", "reason": "trading_disabled"}
    assert client.close_calls == []
    assert client.entry_calls == []
    assert conn.execute("SELECT COUNT(*) FROM bybit_demo_reversals").fetchone()[0] == 0


def test_reversal_close_has_own_gate_before_reduce_only_submission(monkeypatch):
    conn = _db()
    db_lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    source_id = 903
    _seed_open_long(conn, db_lock, client, source_id=source_id)
    client.close_calls.clear()
    client.entry_calls.clear()
    gate_results = iter([True, False])
    monkeypatch.setattr(
        bybit_demo,
        "bybit_demo_trading_enabled",
        lambda: next(gate_results),
    )

    result = _submit_opposite(conn, db_lock, client, signal_ts=1_700_000_200)

    assert result["status"] == "blocked"
    assert result["reason"] == "reversal_recovery_required"
    assert client.close_calls == []
    assert client.entry_calls == []


def _seed_open_long(conn, db_lock, client, source_id=900, signal_ts=1_700_000_000):
    result = submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=source_id,
        signal_ts=signal_ts,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )
    assert result["status"] == "submitted"
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "1",
        "positionValue": "100",
        "unrealisedPnl": "0",
    }]
    conn.execute(
        "UPDATE bybit_demo_positions SET status='open', position_size=1 WHERE id=?",
        (result["ledger_id"],),
    )
    conn.commit()
    return result["ledger_id"]


def _submit_opposite(conn, db_lock, client, *, signal_ts=1_700_000_100):
    return submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=signal_ts,
        signal_ts=signal_ts,
        symbol="BTCUSDT",
        direction="SHORT",
        signal_price=100,
        entry_price=100,
        sl_price=105,
        tp_price=90,
    )


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


def test_nonzero_bybit_response_preserves_and_logs_full_error_payload(caplog):
    payload = {
        "retCode": 110126,
        "retMsg": "demo error\nwith detail",
        "result": {"list": []},
        "time": 1700000000000,
    }
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        session=RecordingSession(payload),
        clock=lambda: 1_700_000_000,
    )

    with caplog.at_level("WARNING", logger="bybit_demo"):
        with pytest.raises(BybitDemoError) as raised:
            client.get_order_realtime(symbol="CXMTUSDT", order_link_id="bdabc")

    error = raised.value
    assert error.endpoint == "/v5/order/realtime"
    assert error.ret_code == 110126
    assert error.ret_msg == "demo error\nwith detail"
    assert error.payload == payload
    record = next(
        record for record in caplog.records if record.message.startswith("bybit_api_error ")
    )
    assert "endpoint=/v5/order/realtime" in record.message
    assert "retCode=110126" in record.message
    assert "retMsg=\"demo error\\nwith detail\"" in record.message
    assert json.dumps(payload, separators=(",", ":"), ensure_ascii=False) in record.message


def test_submit_signal_persists_instrument_error_payload():
    class InstrumentErrorClient(FakeTradingClient):
        def get_instrument_info(self, symbol):
            raise BybitDemoError(
                "/v5/market/instruments-info",
                "bybit_ret_code_10001",
                ret_code=10001,
                ret_msg="Request parameter error",
                payload={
                    "retCode": 10001,
                    "retMsg": "Request parameter error",
                    "result": {"list": []},
                },
            )

    conn = _db()
    result = submit_signal(
        conn,
        threading.Lock(),
        InstrumentErrorClient(),
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=1,
        signal_ts=1_700_000_000,
        symbol="MARSCOINUSDT",
        direction="LONG",
        signal_price=0.05549,
        entry_price=0.07052,
        sl_price=0.0575675,
        tp_price=0.096425,
    )

    assert result["status"] == "rejected"
    raw = conn.execute(
        "SELECT raw_order_json FROM bybit_demo_positions"
    ).fetchone()[0]
    assert json.loads(raw) == {
        "retCode": 10001,
        "retMsg": "Request parameter error",
        "result": {"list": []},
    }


def test_poll_position_error_persists_raw_position_payload():
    class PositionErrorClient(FakeTradingClient):
        def get_position(self, symbol):
            raise BybitDemoError(
                "/v5/position/list",
                "bybit_ret_code_10001",
                ret_code=10001,
                ret_msg="position lookup failed",
                payload={
                    "retCode": 10001,
                    "retMsg": "position lookup failed",
                    "result": {"list": []},
                },
            )

    conn = _db()
    db_lock = threading.Lock()
    client = PositionErrorClient()
    result = submit_signal(
        conn,
        db_lock,
        client,
        strategy="overheated_24h",
        confirmation_level=None,
        source_demo_position_id=2,
        signal_ts=1_700_000_001,
        symbol="BTCUSDT",
        direction="LONG",
        signal_price=100,
        entry_price=100,
        sl_price=95,
        tp_price=110,
    )
    assert result["status"] == "submitted"

    poll_positions(conn, db_lock, client)
    status, raw = conn.execute(
        "SELECT status, raw_position_json FROM bybit_demo_positions"
    ).fetchone()
    assert status == "unknown"
    assert json.loads(raw)["retMsg"] == "position lookup failed"


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
        (3949.0, "allow", "allowed"),
        (3950.0, "allow", "allowed"),
        (3951.0, "blocked", "exposure_cap"),
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
        "positionValue": "3951",
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
    assert row[2:] == ("blocked", "exposure_cap", 3951.0, 1000.0)
    snapshot = status_snapshot(conn, db_lock, client)
    latest = snapshot["reserve_preflight"]["latest"]
    assert latest["decision"] == "blocked"
    assert latest["reason"] == "exposure_cap"
    assert latest["open_exposure_usd"] == 3951.0
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
    assert row[4] == pytest.approx(0.75)
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


def test_reversal_uses_two_orders_close_then_open():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    source_id = _seed_open_long(conn, lock, client)

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "submitted"
    assert len(client.close_calls) == 1
    assert len(client.entry_calls) == 2
    assert client.close_calls[0]["reduce_only"] is True
    assert client.close_calls[0]["direction"] == "LONG"
    assert client.close_calls[0]["order_link_id"].startswith("brc")
    rows = conn.execute(
        "SELECT id, direction, status, exit_reason, origin, reversal_id "
        "FROM bybit_demo_positions ORDER BY id"
    ).fetchall()
    assert rows[0] == (source_id, "LONG", "closed", "reversal_used", "signal", None)
    assert rows[1][1:] == ("SHORT", "submitted", None, "reversal", result["reversal_id"])
    wire_session = RecordingSession({"retCode": 0, "result": {"orderId": "close-wire"}})
    wire_client = BybitDemoClient(
        "api-key",
        "api-secret",
        session=wire_session,
        clock=lambda: 1_700_000_000,
    )
    wire_client.create_market_order(
        symbol="BTCUSDT",
        direction="LONG",
        qty=1,
        order_link_id="brc-wire",
        reduce_only=True,
    )
    wire_body = json.loads(wire_session.calls[0][2]["data"])
    assert wire_body["side"] == "Sell"
    assert wire_body["reduceOnly"] is True
    assert "takeProfit" not in wire_body


def test_reversal_close_uses_live_exchange_size_including_manual_add():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)
    client.positions[0]["size"] = "2.75"

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "submitted"
    assert client.close_calls[0]["qty"] == pytest.approx(2.75)
    reversal = conn.execute(
        "SELECT position_size_before, reversal_used FROM bybit_demo_reversals"
    ).fetchone()
    assert reversal == (pytest.approx(2.75), 1)


def test_reversal_claim_is_atomic_for_concurrent_signals(tmp_path):
    path = tmp_path / "reversal.sqlite"
    first = sqlite3.connect(path, timeout=2, check_same_thread=False)
    second = sqlite3.connect(path, timeout=2, check_same_thread=False)
    initialize_schema(first)
    initialize_schema(second)
    results = []
    barrier = threading.Barrier(2)

    def claim(conn):
        barrier.wait()
        results.append(
            bybit_demo._claim_reversal(
                conn,
                threading.Lock(),
                symbol="BTCUSDT",
                source_signal_key="same-signal",
                source_direction="LONG",
                target_direction="SHORT",
                source_ledger_ids=[],
                now=1_700_000_000,
            )
        )

    threads = [threading.Thread(target=claim, args=(first,)),
               threading.Thread(target=claim, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(value is not None for value in results) == [False, True]
    assert first.execute(
        "SELECT COUNT(*) FROM bybit_demo_reversals"
    ).fetchone()[0] == 1
    first.close()
    second.close()


def test_reversal_claim_rolls_back_unexpected_exception(monkeypatch):
    conn = _db()
    lock = threading.Lock()

    def fail_json(_value):
        raise RuntimeError("unexpected claim failure")

    monkeypatch.setattr(bybit_demo, "_json", fail_json)
    with pytest.raises(RuntimeError, match="unexpected claim failure"):
        bybit_demo._claim_reversal(
            conn,
            lock,
            symbol="BTCUSDT",
            source_signal_key="rollback-signal",
            source_direction="LONG",
            target_direction="SHORT",
            source_ledger_ids=[],
            now=1_700_000_000,
        )

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM bybit_demo_reversals"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("expired_state", ["CLOSING", "OPEN_PENDING"])
def test_reversal_watchdog_recovers_expired_lifecycle(expired_state):
    conn = _db()
    lock = threading.Lock()
    reversal_id = bybit_demo._claim_reversal(
        conn,
        lock,
        symbol="BTCUSDT",
        source_signal_key=f"watchdog-{expired_state}",
        source_direction="LONG",
        target_direction="SHORT",
        source_ledger_ids=[],
        now=1_700_000_000,
    )
    assert reversal_id is not None
    conn.execute(
        """
        UPDATE bybit_demo_reversals
        SET state=?, close_deadline_ts=?, close_attempts=2
        WHERE id=?
        """,
        (expired_state, 1_700_000_010, reversal_id),
    )
    conn.commit()

    result = bybit_demo.recover_expired_reversals(
        conn,
        lock,
        now=1_700_000_011,
    )

    assert result == {
        "scanned": 1,
        "recovered": 1,
        "reversal_ids": [reversal_id],
    }
    assert conn.execute(
        "SELECT state, recovery_reason, last_error "
        "FROM bybit_demo_reversals WHERE id=?",
        (reversal_id,),
    ).fetchone() == (
        "RECOVERY_REQUIRED",
        f"watchdog_expired_{expired_state.lower()}",
        f"watchdog_expired_{expired_state.lower()}",
    )
    assert conn.execute(
        """
        SELECT event_type, attempt_no, status, reason
        FROM bybit_demo_reversal_events
        WHERE reversal_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (reversal_id,),
    ).fetchone() == (
        "watchdog_recovery_required",
        2,
        "recovery_required",
        f"watchdog_expired_{expired_state.lower()}",
    )


def test_reversal_watchdog_does_not_touch_unexpired_lifecycle():
    conn = _db()
    lock = threading.Lock()
    reversal_id = bybit_demo._claim_reversal(
        conn,
        lock,
        symbol="BTCUSDT",
        source_signal_key="watchdog-not-expired",
        source_direction="LONG",
        target_direction="SHORT",
        source_ledger_ids=[],
        now=1_700_000_000,
    )
    assert reversal_id is not None

    result = bybit_demo.recover_expired_reversals(
        conn,
        lock,
        now=1_700_000_010,
    )

    assert result == {"scanned": 0, "recovered": 0, "reversal_ids": []}
    assert conn.execute(
        "SELECT state FROM bybit_demo_reversals WHERE id=?",
        (reversal_id,),
    ).fetchone() == ("CLOSING",)


def test_reversal_persists_claim_and_used_across_restart(tmp_path):
    path = tmp_path / "restart.sqlite"
    conn = sqlite3.connect(path, check_same_thread=False)
    initialize_schema(conn)
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)
    first = _submit_opposite(conn, lock, client)
    assert first["status"] == "submitted"
    persisted = conn.execute(
        "SELECT reversal_claimed, reversal_used, state FROM bybit_demo_reversals"
    ).fetchone()
    conn.close()

    restarted = sqlite3.connect(path, check_same_thread=False)
    initialize_schema(restarted)
    restarted_lock = threading.Lock()
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Sell",
        "size": "0.5",
        "positionValue": "50",
        "unrealisedPnl": "0",
    }]
    second = submit_signal(
        restarted, restarted_lock, client,
        strategy="overheated_24h", confirmation_level=None,
        source_demo_position_id=1_700_000_200, signal_ts=1_700_000_200,
        symbol="BTCUSDT", direction="LONG", signal_price=100,
        entry_price=100, sl_price=95, tp_price=110,
    )

    assert persisted == (1, 1, "ACTIVE_AFTER_REVERSAL")
    assert second["status"] == "blocked"
    assert second["reason"] == "reversal_already_used"
    restarted.close()


def test_reversal_blocks_second_opposite_signal_after_used():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)
    first = _submit_opposite(conn, lock, client)
    close_count = len(client.close_calls)

    second = submit_signal(
        conn, lock, client,
        strategy="overheated_24h", confirmation_level=None,
        source_demo_position_id=1_700_000_201, signal_ts=1_700_000_201,
        symbol="BTCUSDT", direction="LONG", signal_price=100,
        entry_price=100, sl_price=95, tp_price=110,
    )

    assert first["status"] == "submitted"
    assert second["status"] == "blocked"
    assert second["reason"] == "reversal_already_used"
    assert len(client.close_calls) == close_count


def test_reversal_fails_closed_when_live_position_read_fails():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)
    client.fail_position = True

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "unknown"
    assert result["reason"] == "live_position_read_failed"
    assert client.close_calls == []
    assert client.entry_calls and len(client.entry_calls) == 1
    raw = conn.execute(
        "SELECT raw_position_json FROM bybit_demo_positions ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert json.loads(raw)["retCode"] == 10001


def test_reversal_manual_close_between_signals_becomes_normal_open():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    _seed_open_long(conn, lock, client)
    client.positions = []

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "submitted"
    assert "reversal_id" not in result
    assert client.close_calls == []
    assert len(client.entry_calls) == 2


def test_reversal_partial_close_re_reads_and_retries_with_remaining_size():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(
        close_plan=[("PartiallyFilled", 0.5), ("Filled", None)]
    )
    _seed_open_long(conn, lock, client)

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "submitted"
    assert [call["qty"] for call in client.close_calls] == [1.0, 0.5]
    assert conn.execute(
        "SELECT close_attempts, reversal_used, state FROM bybit_demo_reversals"
    ).fetchone() == (2, 1, "ACTIVE_AFTER_REVERSAL")


def test_reversal_partial_close_is_bounded_and_enters_recovery():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(
        close_plan=[("PartiallyFilled", 0.1)] * bybit_demo.BYBIT_DEMO_REVERSAL_MAX_PASSES
    )
    _seed_open_long(conn, lock, client)

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "blocked"
    assert result["reason"] == "reversal_recovery_required"
    assert len(client.close_calls) == bybit_demo.BYBIT_DEMO_REVERSAL_MAX_PASSES
    attempts, state, reason = conn.execute(
        "SELECT close_attempts, state, recovery_reason FROM bybit_demo_reversals"
    ).fetchone()
    assert attempts == 3
    assert state == "RECOVERY_REQUIRED"
    assert reason == "close_max_passes_exceeded"
    assert client.entry_calls and len(client.entry_calls) == 1


def test_reversal_ambiguous_close_requires_recovery_without_second_post():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient()
    _seed_open_long(conn, lock, client)
    client.fail_close_submission = True

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "blocked"
    assert result["reason"] == "reversal_recovery_required"
    assert len(client.close_calls) == 1
    assert len(client.entry_calls) == 1
    assert conn.execute(
        "SELECT state, recovery_reason FROM bybit_demo_reversals"
    ).fetchone() == ("RECOVERY_REQUIRED", "ambiguous_close_submission:transport_error")


def test_reversal_open_failure_enters_recovery_without_open_retry():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)
    client.block_entry_with_reserve = True

    result = _submit_opposite(conn, lock, client)

    assert result["status"] == "rejected"
    assert result["reversal_id"] is not None
    assert len(client.close_calls) == 1
    assert len(client.entry_calls) == 1
    assert conn.execute(
        "SELECT state, reversal_used, current_ledger_id "
        "FROM bybit_demo_reversals"
    ).fetchone() == ("RECOVERY_REQUIRED", 1, None)


def test_reversal_telemetry_has_distinct_close_and_open_events():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)

    result = _submit_opposite(conn, lock, client)

    events = conn.execute(
        "SELECT event_type FROM bybit_demo_reversal_events "
        "WHERE reversal_id=? ORDER BY id",
        (result["reversal_id"],),
    ).fetchall()
    assert [event[0] for event in events] == [
        "claim", "close_reconciled", "open_submitted"
    ]
    close_link, open_link = conn.execute(
        "SELECT close_order_link_id, ("
        "SELECT order_link_id FROM bybit_demo_positions WHERE reversal_id=?"
        ") FROM bybit_demo_reversals WHERE id=?",
        (result["reversal_id"], result["reversal_id"]),
    ).fetchone()
    assert close_link.startswith("brc")
    assert open_link.startswith("bd")
    assert close_link != open_link


def test_reversal_lifecycle_resets_only_after_reversed_position_closes():
    conn = _db()
    lock = threading.Lock()
    client = ReversalTradingClient(close_plan=[("Filled", None)])
    _seed_open_long(conn, lock, client)
    first = _submit_opposite(conn, lock, client)
    current_id = first["ledger_id"]
    client.closed = [{
        "symbol": "BTCUSDT",
        "avgExitPrice": "90",
        "closedPnl": "1",
        "openFee": "0",
        "closeFee": "0",
        "updatedTime": int(time.time() * 1000) + 1_000,
    }]
    client.positions = []

    poll_positions(conn, lock, client)
    state = conn.execute(
        "SELECT state FROM bybit_demo_reversals WHERE id=?",
        (first["reversal_id"],),
    ).fetchone()[0]
    second = submit_signal(
        conn, lock, client,
        strategy="overheated_24h", confirmation_level=None,
        source_demo_position_id=1_700_000_300, signal_ts=1_700_000_300,
        symbol="BTCUSDT", direction="LONG", signal_price=100,
        entry_price=100, sl_price=105, tp_price=90,
    )

    assert state == "CLOSED"
    assert second["status"] == "submitted"
    assert "reversal_id" not in second
    assert current_id != second["ledger_id"]


def _seed_breakeven_ledger(
    conn,
    lock,
    client,
    *,
    direction="LONG",
    be_state="not_armed",
    tp1_executed_qty=0.1,
    stop_loss="95",
    pending_since=None,
    readback_attempts=0,
    signal_key="tp-parent",
    symbol="BTCUSDT",
    order_link_id="bd-tp-parent",
):
    ledger_id = _seed_tp_parent(
        conn,
        signal_key=signal_key,
        symbol=symbol,
        order_link_id=order_link_id,
    )
    side = "Buy" if direction == "LONG" else "Sell"
    conn.execute(
        """
        UPDATE bybit_demo_positions
        SET direction=?, status='open', protection_state='armed',
            tick_size=0.1, be_state=?, be_price=?,
            be_pending_since_ts=?, be_readback_attempts=?
        WHERE id=?
        """,
        (
            direction,
            be_state,
            100.0,
            pending_since,
            readback_attempts,
            ledger_id,
        ),
    )
    leg_id = bybit_demo._insert_tp_leg(
        conn,
        lock,
        ledger_id,
        leg_index=1,
        target_multiplier=1.0,
        target_price=102.0 if direction == "LONG" else 98.0,
        planned_share=0.2,
        planned_qty=0.2,
        now=1_700_000_001,
    )
    conn.execute(
        """
        UPDATE bybit_demo_tp_legs
        SET status='open', order_id='tp-be-1', order_link_id='tp-be-link-1',
            executed_qty=?
        WHERE id=?
        """,
        (tp1_executed_qty, leg_id),
    )
    conn.commit()
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": side,
        "size": "0.8",
        "positionValue": "80",
        "unrealisedPnl": "1",
        "avgPrice": "100",
        "stopLoss": str(stop_loss),
    }]
    return ledger_id


def test_breakeven_skips_multiple_ledger_rows_for_same_symbol(monkeypatch, caplog):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()

    first_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        signal_key="tp-parent-a",
        order_link_id="bd-tp-parent-a",
    )
    second_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        signal_key="tp-parent-b",
        order_link_id="bd-tp-parent-b",
    )

    with caplog.at_level("WARNING", logger="bybit_demo"):
        first = ensure_breakeven_sl(
            conn,
            lock,
            client,
            ledger_id=first_id,
            position=client.positions[0],
            now=1_700_000_010,
        )
        second = ensure_breakeven_sl(
            conn,
            lock,
            client,
            ledger_id=second_id,
            position=client.positions[0],
            now=1_700_000_011,
        )

    expected_candidates = sorted([first_id, second_id])
    assert first["status"] == "skipped_multi_row"
    assert second["status"] == "skipped_multi_row"
    assert first["candidate_ledger_ids"] == expected_candidates
    assert second["candidate_ledger_ids"] == expected_candidates
    assert client.trading_stop_calls == []
    assert sum(
        "bybit_demo_breakeven_skipped_multi_row" in record.message
        for record in caplog.records
    ) == 2

    # A distinct symbol is a single-candidate case and must proceed normally.
    lone_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        signal_key="tp-parent-c",
        order_link_id="bd-tp-parent-c",
        symbol="ETHUSDT",
    )
    client.positions = [{
        "symbol": "ETHUSDT",
        "side": "Buy",
        "size": "0.8",
        "positionValue": "80",
        "unrealisedPnl": "1",
        "avgPrice": "100",
        "stopLoss": "95",
    }]
    lone = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=lone_id,
        position=client.positions[0],
        now=1_700_000_012,
    )
    assert lone["status"] == "armed"
    assert len(client.trading_stop_calls) == 1


def _seed_trailing_ledger(
    conn,
    *,
    direction="LONG",
    floor=102.0,
    signal_key="trail-parent",
    symbol="BTCUSDT",
    order_link_id="bd-trail-parent",
):
    ledger_id = _seed_tp_parent(
        conn,
        signal_key=signal_key,
        symbol=symbol,
        order_link_id=order_link_id,
    )
    conn.execute(
        """
        UPDATE bybit_demo_positions
        SET direction=?, status='open', tick_size=0.1, trail_floor=?
        WHERE id=?
        """,
        (direction, floor, ledger_id),
    )
    conn.commit()
    return ledger_id


def test_maintain_trailing_stops_activates_at_floor():
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    _seed_trailing_ledger(conn, direction="LONG", floor=102.0)
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "1",
        "markPrice": "103",
    }]

    result = bybit_demo.maintain_trailing_stops(conn, lock, client)

    assert result["moved"] == 1
    assert len(client.trading_stop_calls) == 1
    assert client.trading_stop_calls[0]["stop_loss"] == 102.0


def test_maintain_trailing_stops_skips_when_reversal_in_progress():
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_trailing_ledger(conn, direction="LONG", floor=102.0)
    conn.execute(
        """
        INSERT INTO bybit_demo_reversals (
            symbol, source_signal_key, source_direction, target_direction,
            state, close_deadline_ts, claimed_ts, created_ts, updated_ts
        ) VALUES ('BTCUSDT', 'trail-reversal-src', 'LONG', 'SHORT',
                  'CLOSING', 9999999999, 1700000000, 1700000000, 1700000000)
        """
    )
    conn.commit()
    client.positions = [{
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "1",
        "markPrice": "103",
    }]

    result = bybit_demo.maintain_trailing_stops(conn, lock, client)

    assert result["skipped_reversal_in_progress"] == 1
    assert client.trading_stop_calls == []
    assert conn.execute(
        "SELECT trail_active FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone()[0] == 0


def test_breakeven_wire_payload_is_full_position_stop_loss():
    session = RecordingSession({"retCode": 0, "result": {}})
    client = BybitDemoClient(
        "api-key",
        "api-secret",
        session=session,
        clock=lambda: 1_700_000_000,
    )

    assert client.set_trading_stop(
        symbol="BTCUSDT",
        stop_loss=100.0,
    ) == {}
    body = json.loads(session.calls[0][2]["data"])
    assert body == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "tpslMode": "Full",
        "stopLoss": "100",
        "slTriggerBy": "MarkPrice",
        "positionIdx": 0,
    }


def test_breakeven_arms_after_tp1_and_is_idempotent(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(conn, lock, client)

    first = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )
    second = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_011,
    )

    assert first["status"] == "armed"
    assert second["status"] == "armed"
    assert len(client.trading_stop_calls) == 1
    assert conn.execute(
        "SELECT be_state, be_price, be_set_ts, be_pending_since_ts, "
        "be_readback_attempts FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == ("armed", 100.0, 1_700_000_010, None, 0)


def test_breakeven_uses_short_entry_basis_and_never_downgrades_protection(
    monkeypatch,
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        direction="SHORT",
        stop_loss="99",
    )

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )

    assert result["status"] == "armed"
    assert client.trading_stop_calls == []
    assert conn.execute(
        "SELECT be_state, be_price FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == ("armed", 100.0)


def test_breakeven_requires_confirmed_tp1_execution(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        tp1_executed_qty=0,
    )

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )

    assert result["status"] == "not_eligible"
    assert client.trading_stop_calls == []


def test_breakeven_pending_is_bounded_by_readbacks_and_timeout(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        be_state="pending",
        pending_since=1_700_000_000,
        readback_attempts=2,
    )

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )

    assert result["status"] == "recovery_required"
    assert client.trading_stop_calls == []
    assert conn.execute(
        "SELECT be_state, last_error FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == (
        "recovery_required",
        "be_readback_exhausted:pending_readback",
    )


def test_breakeven_pending_times_out_before_readback_limit(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        be_state="pending",
        pending_since=1_700_000_000,
        readback_attempts=1,
    )

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_180,
    )

    assert result["status"] == "recovery_required"
    assert client.trading_stop_calls == []
    assert conn.execute(
        "SELECT be_state, be_readback_attempts, last_error "
        "FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == (
        "recovery_required",
        1,
        "be_readback_timeout:pending_readback",
    )


def test_pending_breakeven_fails_closed_when_live_direction_changes(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        be_state="pending",
        pending_since=1_700_000_000,
    )
    client.positions[0]["side"] = "Sell"

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        now=1_700_000_010,
    )

    assert result["status"] == "recovery_required"
    assert conn.execute(
        "SELECT be_state, last_error FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == (
        "recovery_required",
        "be_live_direction_mismatch",
    )


def test_breakeven_deterministic_reject_enters_recovery(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    client.trading_stop_plan = ["reject"]
    ledger_id = _seed_breakeven_ledger(conn, lock, client)

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )

    assert result["status"] == "recovery_required"
    assert conn.execute(
        "SELECT be_state, be_price FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == ("recovery_required", 100.0)
    assert client.positions[0]["stopLoss"] == "95"


def test_breakeven_duplicate_confirms_only_protective_readback(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    client.trading_stop_plan = ["duplicate"]
    ledger_id = _seed_breakeven_ledger(conn, lock, client)

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )

    assert result["status"] == "armed"
    assert conn.execute(
        "SELECT be_state, be_price FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == ("armed", 100.0)


def test_breakeven_detects_reversal_that_starts_before_mutation(monkeypatch):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(conn, lock, client)
    reversal = {
        "state": "CLOSING",
        "current_ledger_id": ledger_id,
    }
    checks = iter([None, reversal])
    monkeypatch.setattr(
        bybit_demo,
        "_reversal_row_for_symbol",
        lambda *args, **kwargs: next(checks),
    )

    result = ensure_breakeven_sl(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        position=client.positions[0],
        now=1_700_000_010,
    )

    assert result["status"] == "recovery_required"
    assert client.trading_stop_calls == []
    assert conn.execute(
        "SELECT be_state, last_error FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == (
        "recovery_required",
        "reversal_started_before_be_mutation",
    )


def test_operator_can_retry_breakeven_recovery_and_snapshot_is_read_only(
    monkeypatch,
):
    monkeypatch.setenv(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, "true")
    monkeypatch.setenv(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, "true")
    conn = _db()
    lock = threading.Lock()
    client = FakeTradingClient()
    ledger_id = _seed_breakeven_ledger(
        conn,
        lock,
        client,
        be_state="recovery_required",
    )
    before = conn.execute(
        "SELECT COUNT(*) FROM bybit_demo_tp_events WHERE ledger_id=?",
        (ledger_id,),
    ).fetchone()[0]

    snapshot = manual_breakeven_snapshot(conn, lock)
    assert snapshot["status"] == "ok"
    assert snapshot["rows"][0]["ledger_id"] == ledger_id
    assert conn.execute(
        "SELECT be_state FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone()[0] == "recovery_required"

    result = manual_recover_breakeven(
        conn,
        lock,
        client,
        ledger_id=ledger_id,
        action="retry_breakeven",
        reason="operator_verified_position",
        now=1_700_000_020,
    )

    assert result["status"] == "armed"
    assert len(client.trading_stop_calls) == 1
    assert conn.execute(
        "SELECT be_state, be_set_ts, last_error FROM bybit_demo_positions WHERE id=?",
        (ledger_id,),
    ).fetchone() == ("armed", 1_700_000_020, None)
    assert conn.execute(
        "SELECT COUNT(*) FROM bybit_demo_tp_events WHERE ledger_id=?",
        (ledger_id,),
    ).fetchone()[0] > before