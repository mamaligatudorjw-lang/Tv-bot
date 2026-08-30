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
from urllib.parse import urlencode, urlparse

import requests

logger = logging.getLogger(__name__)

BYBIT_DEMO_BASE_URL = "https://api-demo.bybit.com"
BYBIT_DEMO_CATEGORY = "linear"
BYBIT_DEMO_NOTIONAL_USD = 50.0
BYBIT_DEMO_RECV_WINDOW = 5_000
BYBIT_DEMO_REQUEST_TIMEOUT = 8.0
BYBIT_DEMO_MAX_POLL_ROWS = 25
BYBIT_RELAY_URL_ENV = "BYBIT_RELAY_URL"
BYBIT_RELAY_TOKEN_ENV = "BYBIT_RELAY_TOKEN"
BYBIT_DEMO_EARLY_PROMOTED_ENV = "BYBIT_DEMO_OVERHEATED_EARLY_PROMOTED"
# Effective boundary for the unified shadow gate.  Existing orders were
# created before this implementation was activated and are classified as
# historical exceptions during the startup backfill.
BYBIT_DEMO_SHADOW_GATE_FIX_TS = 1_788_081_470

_TERMINAL_STATUSES = {"closed", "rejected"}
_POLL_STATUSES = {
    "intent",
    "submitting",
    "submitted",
    "partially_filled",
    "open",
    "unknown",
}

def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def overheated_early_promoted() -> bool:
    """Return the single configured decision for the third whitelist slot."""
    return _env_flag(BYBIT_DEMO_EARLY_PROMOTED_ENV, False)


def allowed_signal_variants(
    *,
    overheated_early_is_promoted: bool | None = None,
) -> dict[str, str | None]:
    """Return exactly three Bybit variants for the current policy state."""
    promoted = (
        overheated_early_promoted()
        if overheated_early_is_promoted is None
        else bool(overheated_early_is_promoted)
    )
    return {
        "overheated_24h": None,
        "overheated_confirmed": "1/3",
        "overheated_early" if promoted else "ema_cross_confirmed": (
            None if promoted else "1/3"
        ),
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


def is_allowed_signal(
    strategy: str | None,
    confirmation_level: str | None,
    *,
    overheated_early_is_promoted: bool | None = None,
) -> bool:
    """Apply the explicit three-strategy Bybit whitelist."""
    variants = allowed_signal_variants(
        overheated_early_is_promoted=overheated_early_is_promoted
    )
    if strategy not in variants:
        return False
    required = variants[strategy]
    return required is None or confirmation_level == required


def classify_gate_metadata(
    shadow_origin: int | bool | None,
    placement_ts: int | float | None,
    *,
    cutoff_ts: int = BYBIT_DEMO_SHADOW_GATE_FIX_TS,
) -> dict[str, int]:
    """Classify a ledger row, preferring source fact over timestamp fallback."""
    known_shadow = shadow_origin in (0, 1, False, True)
    if known_shadow:
        is_shadow = int(bool(shadow_origin))
        uncertain = 0
    else:
        is_shadow = None
        uncertain = 1

    try:
        placement_value = float(placement_ts)
        if not math.isfinite(placement_value):
            raise ValueError("non-finite placement timestamp")
        before_fix: bool | None = placement_value < float(cutoff_ts)
    except (TypeError, ValueError):
        before_fix = None
        uncertain = 1

    if is_shadow == 1 and before_fix is not None:
        pre_gate_exception = int(before_fix)
        post_fix_leak = int(not before_fix)
    elif is_shadow == 0:
        pre_gate_exception = 0
        post_fix_leak = 0
    else:
        # Time-only classification cannot prove shadow origin.  Keep the
        # canonical leak flags clear and expose the fallback separately.
        pre_gate_exception = 0
        post_fix_leak = 0

    return {
        "pre_gate_exception": pre_gate_exception,
        "post_fix_leak": post_fix_leak,
        "gate_classification_uncertain": uncertain,
        "fallback_pre_gate_exception": int(
            is_shadow is None and before_fix is True
        ),
        "fallback_post_fix_leak": int(
            is_shadow is None and before_fix is False
        ),
    }


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
        relay_url: str | None = None,
        relay_token: str | None = None,
        require_relay: bool = False,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.clock = clock
        self.timeout = timeout
        self.recv_window = int(recv_window)
        self.relay_url = (relay_url or "").strip().rstrip("/")
        self.relay_token = (relay_token or "").strip()
        self.require_relay = bool(require_relay)
        self._configuration_error = self._validate_relay_config()

    @classmethod
    def from_env(cls) -> "BybitDemoClient":
        return cls(
            os.environ.get("BYBIT_DEMO_API_KEY"),
            os.environ.get("BYBIT_DEMO_API_SECRET"),
            relay_url=os.environ.get(BYBIT_RELAY_URL_ENV),
            relay_token=os.environ.get(BYBIT_RELAY_TOKEN_ENV),
            require_relay=True,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret and not self._configuration_error)

    @property
    def route(self) -> str:
        if self.relay_url:
            return "relay"
        return "unconfigured" if self.require_relay else "direct"

    @property
    def relay_configured(self) -> bool:
        return bool(self.relay_url and self.relay_token and not self._configuration_error)

    @property
    def configuration_error(self) -> str | None:
        return self._configuration_error

    @property
    def disabled_reason(self) -> str:
        if not self.api_key or not self.api_secret:
            return "credentials_not_configured"
        return self._configuration_error or "disabled"

    def _validate_relay_config(self) -> str | None:
        if not self.relay_url:
            if self.relay_token:
                return "relay_url_missing"
            if self.require_relay:
                return "relay_url_missing"
            return None
        if not self.relay_token:
            return "relay_token_missing"
        parsed = urlparse(self.relay_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return "relay_url_must_be_https_origin"
        return None

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise BybitDemoError(endpoint, "credentials_not_configured")
        if self._configuration_error:
            raise BybitDemoError(endpoint, self._configuration_error)

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
        request_url = f"{self.relay_url if self.relay_url else self.base_url}{endpoint}"
        if method == "GET" and query:
            # Send the exact byte sequence used for the HMAC payload. Passing
            # the original dict via requests' params= would preserve insertion
            # order, which can differ from the sorted query signed above.
            request_url = f"{request_url}?{query}"
        if self.relay_url:
            headers["X-Bybit-Relay-Token"] = self.relay_token

        attempts = 2 if retry_safe else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    request_url,
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
            raw_execution_json TEXT,
            shadow_origin INTEGER,
            pre_gate_exception INTEGER NOT NULL DEFAULT 0,
            post_fix_leak INTEGER NOT NULL DEFAULT 0,
            gate_classification_uncertain INTEGER NOT NULL DEFAULT 0,
            fallback_pre_gate_exception INTEGER NOT NULL DEFAULT 0,
            fallback_post_fix_leak INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # These columns were added after the first live smoke orders.  Keep the
    # migration local to this separate ledger; demo_positions is not altered.
    for column, definition in (
        ("shadow_origin", "INTEGER"),
        ("pre_gate_exception", "INTEGER NOT NULL DEFAULT 0"),
        ("post_fix_leak", "INTEGER NOT NULL DEFAULT 0"),
        ("gate_classification_uncertain", "INTEGER NOT NULL DEFAULT 0"),
        ("fallback_pre_gate_exception", "INTEGER NOT NULL DEFAULT 0"),
        ("fallback_post_fix_leak", "INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(
                f"ALTER TABLE bybit_demo_positions ADD COLUMN {column} {definition}"
            )
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
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
        "pre_gate_exception",
        "post_fix_leak",
        "gate_classification_uncertain",
        "fallback_pre_gate_exception",
        "fallback_post_fix_leak",
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


def _log_post_fix_leak(
    row: dict[str, Any],
    metadata: dict[str, int],
    *,
    placement_ts: int | float | None,
) -> None:
    if not metadata["post_fix_leak"]:
        return
    source = (
        "timestamp_fallback"
        if metadata["gate_classification_uncertain"]
        else "source_is_shadow"
    )
    logger.warning(
        "bybit_demo_post_fix_leak ledger_id=%s strategy=%s symbol=%s "
        "source_demo_position_id=%s placement_ts=%s classification_source=%s "
        "reason=shadow_signal_after_gate_fix",
        row.get("id", "-"),
        row.get("strategy", "?"),
        row.get("symbol", "?"),
        row.get("source_demo_position_id", "-"),
        placement_ts if placement_ts is not None else "-",
        source,
    )


def _refresh_gate_metadata(
    conn: Any,
    db_lock: threading.Lock,
    row_id: int,
    *,
    placement_ts: int | float | None,
) -> dict[str, int]:
    """Recompute report flags without ever changing the shadow snapshot."""
    with db_lock:
        cursor = conn.execute(
            "SELECT id, strategy, symbol, source_demo_position_id, shadow_origin, "
            "pre_gate_exception, post_fix_leak, gate_classification_uncertain, "
            "fallback_pre_gate_exception, fallback_post_fix_leak "
            "FROM bybit_demo_positions WHERE id=?",
            (row_id,),
        )
        fetched = cursor.fetchone()
        row = _row_dict(cursor, fetched) if fetched else None
    if not row:
        return {
            "pre_gate_exception": 0,
            "post_fix_leak": 0,
            "gate_classification_uncertain": 1,
            "fallback_pre_gate_exception": 0,
            "fallback_post_fix_leak": 0,
        }
    metadata = classify_gate_metadata(row.get("shadow_origin"), placement_ts)
    changed = any(
        int(row.get(key) or 0) != value
        for key, value in metadata.items()
    )
    if changed:
        _update_row(conn, db_lock, row_id, **metadata)
    if metadata["post_fix_leak"] and not int(row.get("post_fix_leak") or 0):
        _log_post_fix_leak(row, metadata, placement_ts=placement_ts)
    return metadata


def backfill_gate_metadata(
    conn: Any,
    source_shadow_by_id: dict[int, int | None],
) -> dict[str, int]:
    """Backfill source snapshots and report flags using source facts first.

    The caller supplies the source facts so this module remains independent of
    the paper-trading table.  A resolved source value wins over timestamps;
    rows without a resolved source use an explicitly uncertain time fallback.
    """
    cursor = conn.execute(
        "SELECT id, strategy, symbol, source_demo_position_id, shadow_origin, "
        "ts_submitted, ts_created, pre_gate_exception, post_fix_leak, "
        "gate_classification_uncertain, fallback_pre_gate_exception, "
        "fallback_post_fix_leak FROM bybit_demo_positions ORDER BY id"
    )
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    counts = {
        "updated": 0,
        "pre_gate_exception": 0,
        "post_fix_leak": 0,
        "uncertain_fallback": 0,
    }
    for row in rows:
        source_id = row.get("source_demo_position_id")
        source_known = (
            source_id is not None
            and int(source_id) in source_shadow_by_id
            and source_shadow_by_id[int(source_id)] in (0, 1)
        )
        existing_shadow = row.get("shadow_origin")
        shadow_origin = (
            int(existing_shadow)
            if existing_shadow in (0, 1, False, True)
            else (
                int(source_shadow_by_id[int(source_id)])
                if source_known
                else None
            )
        )
        placement_ts = row.get("ts_submitted") or row.get("ts_created")
        # Older polling rewrote ts_submitted on every reconciliation.  For
        # rows created before this gate, a later value is that polling
        # timestamp rather than the first order attempt; restore the durable
        # creation-time boundary before classifying the historical row.
        repaired_legacy_submission = False
        try:
            if (
                row.get("ts_created") is not None
                and row.get("ts_submitted") is not None
                and int(row["ts_created"]) < BYBIT_DEMO_SHADOW_GATE_FIX_TS
                and int(row["ts_submitted"]) >= BYBIT_DEMO_SHADOW_GATE_FIX_TS
            ):
                placement_ts = row["ts_created"]
                repaired_legacy_submission = True
                conn.execute(
                    "UPDATE bybit_demo_positions SET ts_submitted=ts_created "
                    "WHERE id=?",
                    (row["id"],),
                )
        except (TypeError, ValueError):
            pass
        metadata = classify_gate_metadata(shadow_origin, placement_ts)
        needs_update = (
            (shadow_origin is not None and existing_shadow is None)
            or repaired_legacy_submission
            or any(int(row.get(key) or 0) != value for key, value in metadata.items())
        )
        if needs_update:
            conn.execute(
                """
                UPDATE bybit_demo_positions
                SET shadow_origin=?,
                    pre_gate_exception=?,
                    post_fix_leak=?,
                    gate_classification_uncertain=?,
                    fallback_pre_gate_exception=?,
                    fallback_post_fix_leak=?
                WHERE id=?
                """,
                (
                    shadow_origin,
                    metadata["pre_gate_exception"],
                    metadata["post_fix_leak"],
                    metadata["gate_classification_uncertain"],
                    metadata["fallback_pre_gate_exception"],
                    metadata["fallback_post_fix_leak"],
                    row["id"],
                ),
            )
            counts["updated"] += 1
        counts["pre_gate_exception"] += metadata["pre_gate_exception"]
        counts["post_fix_leak"] += metadata["post_fix_leak"]
        counts["uncertain_fallback"] += (
            metadata["fallback_pre_gate_exception"]
            + metadata["fallback_post_fix_leak"]
        )
        if metadata["post_fix_leak"] and not int(row.get("post_fix_leak") or 0):
            _log_post_fix_leak(
                {**row, "shadow_origin": shadow_origin},
                metadata,
                placement_ts=placement_ts,
            )
    conn.commit()
    return counts


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


def _exchange_order_created_ms(row: dict[str, Any]) -> int | None:
    """Read the exchange order creation time from the durable raw order."""
    raw_order = row.get("raw_order_json")
    if not raw_order:
        return None
    try:
        payload = json.loads(raw_order) if isinstance(raw_order, str) else raw_order
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        created = int(payload.get("createdTime") or 0)
    except (TypeError, ValueError):
        return None
    return created if created > 0 else None


def _latest_closed_pnl(
    closed_rows: list[dict[str, Any]], row: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = []
    minimum_ms = _exchange_order_created_ms(row)
    if minimum_ms is None:
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


def _closed_event_ts(closed_pnl: dict[str, Any], fallback: int) -> int:
    """Return the exchange close time, falling back to the poll time."""
    for field in ("updatedTime", "closeTime", "createdTime"):
        try:
            value = int(closed_pnl.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value // 1000 if value > 10_000_000_000 else value
    return fallback


def _closed_pnl_allocation(
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    closed_rows: list[dict[str, Any]],
    closed_pnl: dict[str, Any],
) -> float:
    """Allocate one aggregate exchange close across matching ledger entries."""
    matching_rows = []
    for candidate in rows:
        if (
            str(candidate.get("symbol", "")).upper()
            != str(row.get("symbol", "")).upper()
            or str(candidate.get("direction", "")).upper()
            != str(row.get("direction", "")).upper()
            or str(candidate.get("status", "")).lower() in _TERMINAL_STATUSES
        ):
            continue
        if _latest_closed_pnl(closed_rows, candidate) is closed_pnl:
            matching_rows.append(candidate)
    if not matching_rows:
        # On the first poll, sibling rows may not have received their raw
        # exchange order metadata yet.  The current batch is still the
        # authoritative set for this symbol/direction, so allocate across
        # that group rather than writing the aggregate twice.
        matching_rows = [
            candidate
            for candidate in rows
            if (
                str(candidate.get("symbol", "")).upper()
                == str(row.get("symbol", "")).upper()
                and str(candidate.get("direction", "")).upper()
                == str(row.get("direction", "")).upper()
                and str(candidate.get("status", "")).lower()
                not in _TERMINAL_STATUSES
            )
        ]
    if not matching_rows:
        return 1.0

    quantities: dict[int, float] = {}
    for candidate in matching_rows:
        try:
            quantity = float(
                candidate.get("executed_qty")
                or candidate.get("qty")
                or candidate.get("position_size")
                or 0
            )
        except (TypeError, ValueError):
            quantity = 0.0
        quantities[int(candidate["id"])] = max(quantity, 0.0)
    total_quantity = sum(quantities.values())
    if total_quantity <= 0:
        return 1.0
    return quantities.get(int(row["id"]), 0.0) / total_quantity


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
    source_is_shadow: bool | None = None,
    notional_usd: float = BYBIT_DEMO_NOTIONAL_USD,
) -> dict[str, Any]:
    """Create one idempotent intent and submit exactly one market order."""
    if not is_allowed_signal(strategy, confirmation_level):
        return {"status": "filtered", "reason": "strategy_or_confirmation"}
    if source_is_shadow is True:
        # Defense in depth: the app gate runs first, but this isolated client
        # must also refuse a shadow source before creating an intent.
        return {"status": "filtered", "reason": "shadow_source"}
    if not client.enabled:
        return {"status": "disabled", "reason": client.disabled_reason}
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
    gate_metadata = classify_gate_metadata(source_is_shadow, now)

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
                sl_price, tp_price, notional_usd, status, order_link_id, ts_created,
                shadow_origin, pre_gate_exception, post_fix_leak,
                gate_classification_uncertain, fallback_pre_gate_exception,
                fallback_post_fix_leak
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?)
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
                source_is_shadow,
                gate_metadata["pre_gate_exception"],
                gate_metadata["post_fix_leak"],
                gate_metadata["gate_classification_uncertain"],
                gate_metadata["fallback_pre_gate_exception"],
                gate_metadata["fallback_post_fix_leak"],
            ),
        )
        ledger_id = int(cursor.lastrowid)
        conn.commit()
    if gate_metadata["post_fix_leak"]:
        _log_post_fix_leak(
            {
                "id": ledger_id,
                "strategy": strategy,
                "symbol": symbol.upper(),
                "source_demo_position_id": source_demo_position_id,
            },
            gate_metadata,
            placement_ts=now,
        )

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
    with db_lock:
        existing_cursor = conn.execute(
            "SELECT ts_submitted FROM bybit_demo_positions WHERE id=?",
            (ledger_id,),
        )
        existing_submission = existing_cursor.fetchone()
    first_submitted_at = (
        int(existing_submission[0])
        if existing_submission and existing_submission[0] is not None
        else now
    )
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
        "ts_submitted": first_submitted_at,
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
    _refresh_gate_metadata(
        conn,
        db_lock,
        ledger_id,
        placement_ts=first_submitted_at,
    )


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
                allocation = _closed_pnl_allocation(
                    rows,
                    row,
                    closed_cache[symbol],
                    closed_pnl,
                )
                if realized is not None:
                    realized *= allocation
                fees *= allocation
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
                    ts_closed=_closed_event_ts(closed_pnl, now),
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
                    "reason=%s pnl_usd=%s allocation=%.6f",
                    row_id,
                    symbol,
                    reason,
                    f"{realized:.8g}" if realized is not None else "unknown",
                    allocation,
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
              MAX(last_polled) AS last_polled,
              SUM(CASE WHEN post_fix_leak=1 THEN 1 ELSE 0 END)
                AS post_fix_leak_count,
              SUM(CASE WHEN post_fix_leak=1
                         AND gate_classification_uncertain=0
                       THEN 1 ELSE 0 END) AS confirmed_post_fix_leak_count,
              SUM(CASE WHEN post_fix_leak=1
                         AND gate_classification_uncertain=1
                       THEN 1 ELSE 0 END) AS uncertain_post_fix_leak_count,
              SUM(CASE WHEN fallback_pre_gate_exception=1
                       THEN 1 ELSE 0 END) AS fallback_pre_gate_count,
              SUM(CASE WHEN fallback_post_fix_leak=1
                       THEN 1 ELSE 0 END) AS fallback_post_fix_count
            FROM bybit_demo_positions
            """
        ).fetchone()
        latest_leak = conn.execute(
            """
            SELECT symbol, COALESCE(ts_submitted, ts_created) AS placement_ts,
                   strategy, source_demo_position_id,
                   gate_classification_uncertain
            FROM bybit_demo_positions
            WHERE post_fix_leak=1
            ORDER BY placement_ts DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    leak_count = int(row[4] or 0)
    confirmed_leak_count = int(row[5] or 0)
    uncertain_leak_count = int(row[6] or 0)
    fallback_pre_gate_count = int(row[7] or 0)
    fallback_post_fix_count = int(row[8] or 0)
    return {
        "enabled": client.enabled,
        "configured": client.enabled,
        "route": client.route,
        "relay_required": client.require_relay,
        "relay_configured": client.relay_configured,
        "configuration_error": client.configuration_error,
        "notional_usd": BYBIT_DEMO_NOTIONAL_USD,
        "total": int(row[0] or 0),
        "unfinished": int(row[1] or 0),
        "open": int(row[2] or 0),
        "last_polled": row[3],
        "post_fix_leak_count": leak_count,
        "post_fix_leak_alert": confirmed_leak_count > 0,
        "post_fix_leak_confirmed_count": confirmed_leak_count,
        "post_fix_leak_uncertain_count": uncertain_leak_count,
        "gate_fallback_pre_fix_count": fallback_pre_gate_count,
        "gate_fallback_post_fix_count": fallback_post_fix_count,
        "post_fix_leak_latest": (
            {
                "timestamp": latest_leak[1],
                "symbol": latest_leak[0],
                "strategy": latest_leak[2],
                "source_demo_position_id": latest_leak[3],
                "uncertain": bool(latest_leak[4]),
            }
            if latest_leak
            else None
        ),
        "shadow_gate_fix_ts": BYBIT_DEMO_SHADOW_GATE_FIX_TS,
        "shadow_gate_whitelist": allowed_signal_variants(),
        "overheated_early_promoted": overheated_early_promoted(),
    }