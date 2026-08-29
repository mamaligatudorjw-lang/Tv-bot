"""Bybit Demo Trading REST integration.

This module deliberately owns a separate SQLite ledger.  It never imports the
bot module and never touches the existing paper-trading tables.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

BYBIT_DEMO_BASE_URL = "https://api-demo.bybit.com"
BYBIT_DEMO_CATEGORY = "linear"
BYBIT_DEMO_NOTIONAL_USD = 50.0
BYBIT_DEMO_RECV_WINDOW = 5_000
BYBIT_DEMO_REQUEST_TIMEOUT = 8.0
BYBIT_DEMO_MAX_POLL_ROWS = 25

_TERMINAL_STATUSES = {"closed", "rejected"}
_POLL_STATUSES = {
    "intent",
    "submitting",
    "submitted",
    "partially_filled",
    "open",
    "unknown",
}

_ALLOWED_STRATEGIES = {
    "overheated_24h": None,
    "overheated_confirmed": "1/3",
    "ema_cross_confirmed": "1/3",
}


class BybitDemoError(RuntimeError):
    """Safe, structured error from the Bybit Demo API."""

    def __init__(
        self,
        endpoint: str,
        message: str,
        *,
        retryable: bool = False,
        transport: bool = False,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.retryable = retryable
        self.transport = transport


class BybitDemoSizingError(ValueError):
    """The $50 notional cannot be represented by the instrument filters."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BybitDemoSizingError(f"invalid {field}") from exc
    if not result.is_finite() or result <= 0:
        raise BybitDemoSizingError(f"invalid {field}")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def calculate_linear_quantity(
    notional_usd: float,
    entry_price: float,
    min_order_qty: float | str,
    qty_step: float | str,
) -> float:
    """Return a step-aligned linear-contract quantity without exceeding $50."""
    notional = _decimal(notional_usd, "notional")
    price = _decimal(entry_price, "entry_price")
    minimum = _decimal(min_order_qty, "min_order_qty")
    step = _decimal(qty_step, "qty_step")
    raw_qty = notional / price
    quantity = (raw_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity < minimum or quantity <= 0:
        raise BybitDemoSizingError(
            f"$50 notional is below instrument minimum ({_decimal_text(minimum)})"
        )
    return float(quantity)


def normalize_price(price: float, tick_size: float | str, *, direction: str, is_tp: bool) -> float:
    """Align a barrier to tick size while keeping it on the favorable side."""
    value = _decimal(price, "price")
    tick = _decimal(tick_size, "tick_size")
    rounding = ROUND_UP if (direction == "LONG") == is_tp else ROUND_DOWN
    normalized = (value / tick).to_integral_value(rounding=rounding) * tick
    if normalized <= 0:
        raise BybitDemoSizingError("normalized price is not positive")
    return float(normalized)


def is_allowed_signal(strategy: str | None, confirmation_level: str | None) -> bool:
    """Apply the explicit three-strategy Bybit whitelist."""
    if strategy not in _ALLOWED_STRATEGIES:
        return False
    required = _ALLOWED_STRATEGIES[strategy]
    return required is None or confirmation_level == required


def make_signal_key(
    strategy: str,
    symbol: str,
    direction: str,
    signal_ts: int,
    confirmation_level: str | None,
    source_demo_position_id: int | None = None,
) -> str:
    source = str(source_demo_position_id) if source_demo_position_id is not None else "-"
    return "|".join(
        (
            strategy,
            symbol.upper(),
            direction.upper(),
            str(int(signal_ts)),
            confirmation_level or "-",
            source,
        )
    )


def make_order_link_id(signal_key: str) -> str:
    """Bybit-compatible deterministic idempotency key (<= 36 chars)."""
    return "bd" + hashlib.sha256(signal_key.encode("utf-8")).hexdigest()[:30]


class BybitDemoClient:
    """Small signed REST client for the Bybit V5 Demo service."""

    def __init__(
        self,
        api_key: str | None,
        api_secret: str | None,
        *,
        base_url: str = BYBIT_DEMO_BASE_URL,
        session: Any | None = None,
        clock: Any = time.time,
        timeout: float = BYBIT_DEMO_REQUEST_TIMEOUT,
        recv_window: int = BYBIT_DEMO_RECV_WINDOW,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.clock = clock
        self.timeout = timeout
        self.recv_window = int(recv_window)

    @classmethod
    def from_env(cls) -> "BybitDemoClient":
        return cls(
            os.environ.get("BYBIT_DEMO_API_KEY"),
            os.environ.get("BYBIT_DEMO_API_SECRET"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise BybitDemoError(endpoint, "credentials_not_configured")

        method = method.upper()
        clean_params = {
            str(key): value
            for key, value in (params or {}).items()
            if value is not None
        }
        query = urlencode(sorted(clean_params.items()), doseq=True)
        body_text = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
            if body is not None
            else ""
        )
        timestamp = str(int(float(self.clock()) * 1000))
        payload = query if method == "GET" else body_text
        sign_payload = timestamp + self.api_key + str(self.recv_window) + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            sign_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json",
        }

        attempts = 2 if retry_safe else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{endpoint}",
                    params=clean_params if method == "GET" else None,
                    data=body_text if method != "GET" else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    continue
                raise BybitDemoError(
                    endpoint,
                    "transport_error",
                    retryable=True,
                    transport=True,
                ) from exc

            status = int(getattr(response, "status_code", 200))
            if status >= 500 or status == 429:
                if attempt + 1 < attempts:
                    continue
                raise BybitDemoError(
                    endpoint,
                    f"http_{status}",
                    retryable=True,
                )
            if status >= 400:
                raise BybitDemoError(endpoint, f"http_{status}")

            try:
                payload_json = response.json()
            except (TypeError, ValueError) as exc:
                raise BybitDemoError(endpoint, "invalid_json") from exc
            if not isinstance(payload_json, dict):
                raise BybitDemoError(endpoint, "invalid_response")
            ret_code = payload_json.get("retCode", 0)
            if int(ret_code) != 0:
                # retMsg is intentionally not copied into logs or exceptions:
                # the endpoint/code is sufficient and avoids leaking response data.
                raise BybitDemoError(
                    endpoint,
                    f"bybit_ret_code_{ret_code}",
                    retryable=int(ret_code) in {10006, 10016},
                )
            return payload_json
        raise BybitDemoError(endpoint, "request_failed", retryable=True)

    @staticmethod
    def _result_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = payload.get("result") or {}
        items = result.get("list") if isinstance(result, dict) else None
        return items if isinstance(items, list) else []

    def get_instrument_info(self, symbol: str) -> dict[str, float]:
        payload = self._request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": BYBIT_DEMO_CATEGORY, "symbol": symbol.upper()},
            retry_safe=True,
        )
        items = self._result_list(payload)
        if not items:
            raise BybitDemoError("/v5/market/instruments-info", "symbol_not_found")
        item = items[0]
        lot = item.get("lotSizeFilter") or {}
        price_filter = item.get("priceFilter") or {}
        return {
            "min_order_qty": float(lot.get("minOrderQty", 0)),
            "qty_step": float(lot.get("qtyStep", 0)),
            "tick_size": float(price_filter.get("tickSize", 0)),
        }

    def create_market_order(
        self,
        *,
        symbol: str,
        direction: str,
        qty: float,
        take_profit: float,
        stop_loss: float,
        order_link_id: str,
    ) -> dict[str, Any]:
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        body = {
            "category": BYBIT_DEMO_CATEGORY,
            "symbol": symbol.upper(),
            "side": "Buy" if direction == "LONG" else "Sell",
            "orderType": "Market",
            "qty": _decimal_text(_decimal(qty, "qty")),
            "positionIdx": 0,
            "orderLinkId": order_link_id,
            "takeProfit": _decimal_text(_decimal(take_profit, "take_profit")),
            "stopLoss": _decimal_text(_decimal(stop_loss, "stop_loss")),
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "tpTriggerBy": "MarkPrice",
            "slTriggerBy": "MarkPrice",
        }
        payload = self._request("POST", "/v5/order/create", body=body)
        result = payload.get("result") or {}
        return result if isinstance(result, dict) else {}

    def get_order_realtime(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        order_link_id: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v5/order/realtime",
            params={
                "category": BYBIT_DEMO_CATEGORY,
                "symbol": symbol.upper(),
                "orderId": order_id,
                "orderLinkId": order_link_id,
            },
            retry_safe=True,
        )
        return self._result_list(payload)

    def get_position(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v5/position/list",
            params={"category": BYBIT_DEMO_CATEGORY, "symbol": symbol.upper()},
            retry_safe=True,
        )
        return self._result_list(payload)

    def get_closed_pnl(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v5/position/closed-pnl",
            params={"category": BYBIT_DEMO_CATEGORY, "symbol": symbol.upper(), "limit": 50},
            retry_safe=True,
        )
        return self._result_list(payload)

    def get_executions(self, symbol: str, order_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v5/execution/list",
            params={
                "category": BYBIT_DEMO_CATEGORY,
                "symbol": symbol.upper(),
                "orderId": order_id,
                "limit": 100,
            },
            retry_safe=True,
        )
        return self._result_list(payload)

    def apply_demo_money(self, amount_usdt: float) -> dict[str, Any]:
        """Explicit helper; funding is never requested automatically."""
        amount = _decimal(amount_usdt, "amount_usdt")
        return self._request(
            "POST",
            "/v5/account/demo-apply-money",
            body={
                "adjustType": 0,
                "utaDemoApplyMoney": [
                    {"coin": "USDT", "amountStr": _decimal_text(amount)}
                ],
            },
        )


def initialize_schema(conn: Any) -> None:
    """Create only the Bybit Demo ledger on the bot's existing connection."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bybit_demo_positions (
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
            qty REAL,
            min_order_qty REAL,
            qty_step REAL,
            tick_size REAL,
            status TEXT NOT NULL DEFAULT 'intent',
            order_id TEXT,
            order_link_id TEXT NOT NULL UNIQUE,
            order_status TEXT,
            executed_qty REAL,
            avg_entry_price REAL,
            position_size REAL,
            exit_price REAL,
            realized_pnl_usd REAL,
            fee_usd REAL,
            exit_reason TEXT,
            ts_created INTEGER NOT NULL,
            ts_submitted INTEGER,
            ts_filled INTEGER,
            ts_closed INTEGER,
            last_polled INTEGER,
            last_error TEXT,
            raw_order_json TEXT,
            raw_position_json TEXT,
            raw_execution_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bybit_demo_status "
        "ON bybit_demo_positions(status, symbol)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bybit_demo_signal_ts "
        "ON bybit_demo_positions(signal_ts)"
    )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _row_dict(cursor: Any, row: Any) -> dict[str, Any]:
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def _update_row(conn: Any, db_lock: threading.Lock, row_id: int, **fields: Any) -> None:
    allowed = {
        "qty",
        "min_order_qty",
        "qty_step",
        "tick_size",
        "sl_price",
        "tp_price",
        "status",
        "order_id",
        "order_link_id",
        "order_status",
        "executed_qty",
        "avg_entry_price",
        "position_size",
        "exit_price",
        "realized_pnl_usd",
        "fee_usd",
        "exit_reason",
        "ts_submitted",
        "ts_filled",
        "ts_closed",
        "last_polled",
        "last_error",
        "raw_order_json",
        "raw_position_json",
        "raw_execution_json",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"invalid ledger fields: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [row_id]
    with db_lock:
        conn.execute(
            f"UPDATE bybit_demo_positions SET {assignments} WHERE id=?",
            values,
        )
        conn.commit()


def _get_row(conn: Any, db_lock: threading.Lock, row_id: int) -> dict[str, Any] | None:
    with db_lock:
        cursor = conn.execute(
            "SELECT * FROM bybit_demo_positions WHERE id=?",
            (row_id,),
        )
        row = cursor.fetchone()
        return _row_dict(cursor, row) if row else None


def _map_order_status(order_status: str | None, executed_qty: float) -> str:
    status = (order_status or "").lower()
    if status in {"rejected", "cancelled", "deactivated"}:
        return "rejected"
    if status == "partiallyfilled" or executed_qty > 0 and status not in {"filled"}:
        return "partially_filled"
    return "submitted"


def _matching_position(
    positions: list[dict[str, Any]], symbol: str, direction: str
) -> dict[str, Any] | None:
    expected_side = "Buy" if direction == "LONG" else "Sell"
    for position in positions:
        if str(position.get("symbol", "")).upper() != symbol.upper():
            continue
        if position.get("side") not in {expected_side, "", None}:
            continue
        try:
            if float(position.get("size") or 0) > 0:
                return position
        except (TypeError, ValueError):
            continue
    return None


def _infer_exit_reason(
    direction: str,
    exit_price: float | None,
    sl_price: float,
    tp_price: float,
    tick_size: float | None,
) -> str:
    if exit_price is None or not math.isfinite(exit_price):
        return "exchange_close_unresolved"
    tolerance = max(abs(float(tick_size or 0)) * 2.0, abs(exit_price) * 0.001)
    if abs(exit_price - tp_price) <= tolerance:
        return "tp"
    if abs(exit_price - sl_price) <= tolerance:
        return "sl"
    return "manual_or_exchange"


def _latest_closed_pnl(
    closed_rows: list[dict[str, Any]], row: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = []
    minimum_ms = int((row.get("ts_submitted") or row["ts_created"]) * 1000)
    for item in closed_rows:
        if str(item.get("symbol", "")).upper() != str(row["symbol"]).upper():
            continue
        try:
            updated = int(item.get("updatedTime") or item.get("createdTime") or 0)
        except (TypeError, ValueError):
            updated = 0
        if updated and updated < minimum_ms:
            continue
        candidates.append((updated, item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def submit_signal(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    strategy: str,
    confirmation_level: str | None,
    source_demo_position_id: int | None,
    signal_ts: int,
    symbol: str,
    direction: str,
    signal_price: float,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    notional_usd: float = BYBIT_DEMO_NOTIONAL_USD,
) -> dict[str, Any]:
    """Create one idempotent intent and submit exactly one market order."""
    if not is_allowed_signal(strategy, confirmation_level):
        return {"status": "filtered", "reason": "strategy_or_confirmation"}
    if not client.enabled:
        return {"status": "disabled", "reason": "credentials_not_configured"}
    if direction not in {"LONG", "SHORT"}:
        return {"status": "rejected", "reason": "invalid_direction"}
    values = (signal_price, entry_price, sl_price, tp_price, notional_usd)
    if not all(math.isfinite(float(value)) and float(value) > 0 for value in values):
        return {"status": "rejected", "reason": "invalid_price_basis"}

    signal_key = make_signal_key(
        strategy,
        symbol,
        direction,
        signal_ts,
        confirmation_level,
        source_demo_position_id,
    )
    order_link_id = make_order_link_id(signal_key)
    now = int(time.time())

    with db_lock:
        initialize_schema(conn)
        cursor = conn.execute(
            "SELECT id, status, order_id FROM bybit_demo_positions WHERE signal_key=?",
            (signal_key,),
        )
        existing = cursor.fetchone()
        if existing:
            return {
                "status": "duplicate",
                "ledger_id": int(existing[0]),
                "existing_status": existing[1],
                "order_id": existing[2],
            }
        cursor = conn.execute(
            """
            INSERT INTO bybit_demo_positions (
                signal_key, signal_ts, source_demo_position_id, strategy,
                confirmation_level, symbol, direction, signal_price, entry_price,
                sl_price, tp_price, notional_usd, status, order_link_id, ts_created
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?, ?)
            """,
            (
                signal_key,
                int(signal_ts),
                source_demo_position_id,
                strategy,
                confirmation_level,
                symbol.upper(),
                direction,
                float(signal_price),
                float(entry_price),
                float(sl_price),
                float(tp_price),
                float(notional_usd),
                order_link_id,
                now,
            ),
        )
        ledger_id = int(cursor.lastrowid)
        conn.commit()

    try:
        instrument = client.get_instrument_info(symbol)
        qty = calculate_linear_quantity(
            notional_usd,
            entry_price,
            instrument["min_order_qty"],
            instrument["qty_step"],
        )
        aligned_sl = normalize_price(
            sl_price, instrument["tick_size"], direction=direction, is_tp=False
        )
        aligned_tp = normalize_price(
            tp_price, instrument["tick_size"], direction=direction, is_tp=True
        )
        _update_row(
            conn,
            db_lock,
            ledger_id,
            qty=qty,
            min_order_qty=instrument["min_order_qty"],
            qty_step=instrument["qty_step"],
            tick_size=instrument["tick_size"],
            sl_price=aligned_sl,
            tp_price=aligned_tp,
            last_error=None,
        )

        # A lookup failure is ambiguous: do not place an order we cannot
        # reconcile.  A successful empty lookup is safe to proceed.
        remote = client.get_order_realtime(
            symbol=symbol,
            order_link_id=order_link_id,
        )
        if remote:
            result = remote[0]
            _record_order(conn, db_lock, ledger_id, result, now)
            return {"status": "recovered", "ledger_id": ledger_id}

        _update_row(conn, db_lock, ledger_id, status="submitting")
        result = client.create_market_order(
            symbol=symbol,
            direction=direction,
            qty=qty,
            take_profit=aligned_tp,
            stop_loss=aligned_sl,
            order_link_id=order_link_id,
        )
        _record_order(conn, db_lock, ledger_id, result, int(time.time()))
        logger.info(
            "bybit_demo_order_submitted ledger_id=%d strategy=%s symbol=%s "
            "direction=%s qty=%.12g notional=%.2f",
            ledger_id,
            strategy,
            symbol.upper(),
            direction,
            qty,
            float(notional_usd),
        )
        return {"status": "submitted", "ledger_id": ledger_id}
    except BybitDemoSizingError as exc:
        _update_row(conn, db_lock, ledger_id, status="rejected", last_error=str(exc))
        logger.warning(
            "bybit_demo_order_rejected ledger_id=%d reason=sizing_error",
            ledger_id,
        )
        return {"status": "rejected", "ledger_id": ledger_id, "reason": "sizing_error"}
    except BybitDemoError as exc:
        status = "unknown" if exc.retryable or exc.transport else "rejected"
        _update_row(conn, db_lock, ledger_id, status=status, last_error=str(exc))
        logger.warning(
            "bybit_demo_order_%s ledger_id=%d endpoint=%s reason=%s",
            status,
            ledger_id,
            exc.endpoint,
            str(exc),
        )
        return {"status": status, "ledger_id": ledger_id, "reason": str(exc)}
    except Exception as exc:
        _update_row(conn, db_lock, ledger_id, status="unknown", last_error="unexpected_error")
        logger.warning(
            "bybit_demo_order_unknown ledger_id=%d reason=%s",
            ledger_id,
            type(exc).__name__,
        )
        return {"status": "unknown", "ledger_id": ledger_id, "reason": "unexpected_error"}


def _record_order(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    order: dict[str, Any],
    now: int,
) -> None:
    order_id = str(order.get("orderId") or "") or None
    order_link_id = str(order.get("orderLinkId") or "") or None
    try:
        executed_qty = float(order.get("cumExecQty") or 0)
    except (TypeError, ValueError):
        executed_qty = 0.0
    try:
        avg_price = float(order.get("avgPrice") or 0) or None
    except (TypeError, ValueError):
        avg_price = None
    status = _map_order_status(order.get("orderStatus"), executed_qty)
    fields = {
        "status": status,
        "order_id": order_id,
        "order_status": order.get("orderStatus"),
        "executed_qty": executed_qty,
        "avg_entry_price": avg_price,
        "ts_submitted": now,
        "ts_filled": now if str(order.get("orderStatus", "")).lower() == "filled" else None,
        "last_polled": now,
        "last_error": None,
        "raw_order_json": _json(order),
    }
    # The create-order acknowledgement may omit orderLinkId.  The local
    # deterministic link is authoritative and must remain NOT NULL.
    if order_link_id:
        fields["order_link_id"] = order_link_id
    _update_row(conn, db_lock, ledger_id, **fields)


def poll_positions(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    max_rows: int = BYBIT_DEMO_MAX_POLL_ROWS,
) -> dict[str, int | str]:
    """Reconcile unfinished ledger rows without ever creating new orders."""
    if not client.enabled:
        return {"status": "disabled", "polled": 0, "closed": 0}

    with db_lock:
        cursor = conn.execute(
            """
            SELECT * FROM bybit_demo_positions
            WHERE status NOT IN ('closed', 'rejected')
            ORDER BY id
            LIMIT ?
            """,
            (int(max_rows),),
        )
        rows = [_row_dict(cursor, row) for row in cursor.fetchall()]

    polled = 0
    closed = 0
    position_cache: dict[str, list[dict[str, Any]]] = {}
    closed_cache: dict[str, list[dict[str, Any]]] = {}
    execution_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for row in rows:
        row_id = int(row["id"])
        now = int(time.time())
        try:
            order_items: list[dict[str, Any]] = []
            if row.get("order_id"):
                order_items = client.get_order_realtime(
                    symbol=row["symbol"],
                    order_id=str(row["order_id"]),
                )
            else:
                order_items = client.get_order_realtime(
                    symbol=row["symbol"],
                    order_link_id=row["order_link_id"],
                )
            if order_items:
                _record_order(conn, db_lock, row_id, order_items[0], now)
                row = _get_row(conn, db_lock, row_id) or row
            elif not row.get("order_id"):
                # Never turn an ambiguous intent/submission into a second POST.
                _update_row(
                    conn,
                    db_lock,
                    row_id,
                    status="unknown",
                    last_polled=now,
                    last_error="order_not_found_during_recovery",
                )
                polled += 1
                continue

            # Execution records are the durable fill-level evidence.  The
            # order endpoint alone can omit individual fees/fills.
            if row.get("order_id"):
                execution_key = (str(row["symbol"]).upper(), str(row["order_id"]))
                if execution_key not in execution_cache:
                    execution_cache[execution_key] = client.get_executions(
                        row["symbol"], str(row["order_id"])
                    )
                executions = execution_cache[execution_key]
                if executions:
                    _update_row(
                        conn,
                        db_lock,
                        row_id,
                        raw_execution_json=_json({"executions": executions}),
                    )

            symbol = str(row["symbol"]).upper()
            if symbol not in position_cache:
                position_cache[symbol] = client.get_position(symbol)
            position = _matching_position(position_cache[symbol], symbol, row["direction"])
            if position:
                try:
                    position_size = float(position.get("size") or 0)
                except (TypeError, ValueError):
                    position_size = 0.0
                try:
                    avg_entry = float(position.get("avgPrice") or 0) or None
                except (TypeError, ValueError):
                    avg_entry = None
                _update_row(
                    conn,
                    db_lock,
                    row_id,
                    status="open",
                    position_size=position_size,
                    avg_entry_price=avg_entry or row.get("avg_entry_price"),
                    ts_filled=row.get("ts_filled") or now,
                    last_polled=now,
                    raw_position_json=_json(position),
                    last_error=None,
                )
                polled += 1
                continue

            if symbol not in closed_cache:
                closed_cache[symbol] = client.get_closed_pnl(symbol)
            closed_pnl = _latest_closed_pnl(closed_cache[symbol], row)
            if closed_pnl:
                try:
                    exit_price = float(closed_pnl.get("avgExitPrice") or 0) or None
                except (TypeError, ValueError):
                    exit_price = None
                try:
                    realized = float(closed_pnl.get("closedPnl") or 0)
                except (TypeError, ValueError):
                    realized = None
                fees = 0.0
                for fee_key in ("openFee", "closeFee"):
                    try:
                        fees += float(closed_pnl.get(fee_key) or 0)
                    except (TypeError, ValueError):
                        pass
                reason = _infer_exit_reason(
                    row["direction"],
                    exit_price,
                    float(row["sl_price"]),
                    float(row["tp_price"]),
                    row.get("tick_size"),
                )
                _update_row(
                    conn,
                    db_lock,
                    row_id,
                    status="closed",
                    position_size=0.0,
                    exit_price=exit_price,
                    realized_pnl_usd=realized,
                    fee_usd=fees,
                    exit_reason=reason,
                    ts_closed=now,
                    last_polled=now,
                    raw_execution_json=_json({
                        "executions": execution_cache.get(
                            (symbol, str(row.get("order_id") or "")), []
                        ),
                        "closed_pnl": closed_pnl,
                    }),
                    last_error=None,
                )
                logger.info(
                    "bybit_demo_position_closed ledger_id=%d symbol=%s "
                    "reason=%s pnl_usd=%s",
                    row_id,
                    symbol,
                    reason,
                    f"{realized:.8g}" if realized is not None else "unknown",
                )
                polled += 1
                closed += 1
                continue

            current_status = str(row.get("status") or "")
            if current_status in {"submitted", "partially_filled"}:
                _update_row(
                    conn,
                    db_lock,
                    row_id,
                    last_polled=now,
                    last_error="filled_without_visible_position",
                )
            else:
                _update_row(conn, db_lock, row_id, last_polled=now)
            polled += 1
        except BybitDemoError as exc:
            _update_row(
                conn,
                db_lock,
                row_id,
                status="unknown",
                last_polled=now,
                last_error=str(exc),
            )
            logger.warning(
                "bybit_demo_poll_unknown ledger_id=%d endpoint=%s reason=%s",
                row_id,
                exc.endpoint,
                str(exc),
            )
            polled += 1
        except Exception as exc:
            _update_row(
                conn,
                db_lock,
                row_id,
                status="unknown",
                last_polled=now,
                last_error="unexpected_poll_error",
            )
            logger.warning(
                "bybit_demo_poll_unknown ledger_id=%d reason=%s",
                row_id,
                type(exc).__name__,
            )
            polled += 1

    return {"status": "ok", "polled": polled, "closed": closed}


def status_snapshot(conn: Any, db_lock: threading.Lock, client: BybitDemoClient) -> dict[str, Any]:
    """Return safe operational state; never includes credentials or raw payloads."""
    with db_lock:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status IN ('intent','submitting','submitted',
                                       'partially_filled','open','unknown')
                       THEN 1 ELSE 0 END) AS unfinished,
              SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_count,
              MAX(last_polled) AS last_polled
            FROM bybit_demo_positions
            """
        ).fetchone()
    return {
        "enabled": client.enabled,
        "configured": client.enabled,
        "notional_usd": BYBIT_DEMO_NOTIONAL_USD,
        "total": int(row[0] or 0),
        "unfinished": int(row[1] or 0),
        "open": int(row[2] or 0),
        "last_polled": row[3],
    }