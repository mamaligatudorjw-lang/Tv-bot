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
import sqlite3
import threading
import time
from collections import deque
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
BYBIT_DEMO_POLL_STALE_AFTER_SEC = 120
BYBIT_DEMO_TRADING_ENABLED_ENV = "BYBIT_DEMO_TRADING_ENABLED"
BYBIT_RELAY_URL_ENV = "BYBIT_RELAY_URL"
BYBIT_RELAY_TOKEN_ENV = "BYBIT_RELAY_TOKEN"
BYBIT_DEMO_EARLY_PROMOTED_ENV = "BYBIT_DEMO_OVERHEATED_EARLY_PROMOTED"
BYBIT_DEMO_MAX_EXPOSURE_ENV = "BYBIT_DEMO_MAX_EXPOSURE_USD"
BYBIT_DEMO_EQUITY_RESERVE_ENV = "BYBIT_DEMO_EQUITY_RESERVE_USD"
BYBIT_DEMO_MAX_EXPOSURE_USD = 4000.0
BYBIT_DEMO_EQUITY_RESERVE_USD = 100.0
BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_ENV = (
    "BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_SEC"
)
BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV = "BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_SEC"
BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV = (
    "BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD"
)
BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_SEC = 60
BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_SEC = 600
BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD = 3
BYBIT_DEMO_REVERSAL_MAX_PASSES = 3
BYBIT_DEMO_REVERSAL_DEADLINE_SEC = 30.0
BYBIT_DEMO_REVERSAL_WATCHDOG_INTERVAL_SEC = 20
BYBIT_DEMO_TP_PLAN_VERSION = "atr_v1"
BYBIT_DEMO_MULTI_TP_ENABLED_ENV = "BYBIT_DEMO_MULTI_TP_ENABLED"
BYBIT_DEMO_BREAKEVEN_ENABLED_ENV = "BYBIT_DEMO_BREAKEVEN_ENABLED"
BYBIT_DEMO_TP_COUNT = 5
BYBIT_DEMO_TP_SETUP_DEADLINE_SEC = 60.0
BYBIT_DEMO_BE_PENDING_TIMEOUT_SEC = 180
BYBIT_DEMO_BE_MAX_READBACKS = 3
BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE = "recovery_required_manual"
BYBIT_DEMO_TP_PARTIAL_STATE = "armed_partial_manual"
BYBIT_DEMO_TP_ABANDONED_STATE = "recovery_abandoned"
BYBIT_DEMO_BE_NOT_ARMED_STATE = "not_armed"
BYBIT_DEMO_BE_PENDING_STATE = "pending"
BYBIT_DEMO_BE_ARMED_STATE = "armed"
BYBIT_DEMO_BE_RECOVERY_STATE = "recovery_required"
_BE_DEFINITE_REJECT_CODES = {
    10001,
    110005,
    110011,
    110034,
    110040,
    110046,
    110093,
}
_BE_RETRYABLE_CODES = {10006, 10016}
_TP_PLAN_SCHEDULES = {
    1: (("2",), ("1",)),
    2: (("1", "3"), ("0.3", "0.7")),
    3: (("1", "2", "3"), ("0.2", "0.3", "0.5")),
    5: (
        ("1", "1.5", "2", "2.5", "3"),
        ("0.1", "0.15", "0.2", "0.25", "0.3"),
    ),
}
_TP_PLAN_FALLBACKS = {
    3: (3, 2, 1),
    5: (5, 3, 2, 1),
}
_TP_CREATE_DEFINITE_REJECT_CODES = {
    10001,
    110003,
    110004,
    110007,
    110017,
    110020,
    110021,
    110023,
    110044,
    110045,
    170134,
    170136,
    170371,
    170372,
    170381,
    170382,
}
_TP_CREATE_DUPLICATE_CODES = {10014, 110072, 170141, 20006}
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
_REVERSAL_ACTIVE_STATES = {
    "CLAIMED",
    "CLOSING",
    "OPEN_PENDING",
    "ACTIVE_AFTER_REVERSAL",
    "RECOVERY_REQUIRED",
}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def bybit_demo_trading_enabled() -> bool:
    """Return the explicit trading decision; missing/invalid values are off."""
    raw_value = os.environ.get(BYBIT_DEMO_TRADING_ENABLED_ENV)
    if raw_value is None or not raw_value.strip():
        return False
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning(
        "%s has invalid value; Bybit Demo trading disabled",
        BYBIT_DEMO_TRADING_ENABLED_ENV,
    )
    return False


def bybit_demo_multi_tp_enabled() -> bool:
    """Return the explicit multi-TP decision; missing values are off."""
    return _env_flag(BYBIT_DEMO_MULTI_TP_ENABLED_ENV, False)


def bybit_demo_breakeven_enabled() -> bool:
    """Return the explicit breakeven decision; missing values are off."""
    return _env_flag(BYBIT_DEMO_BREAKEVEN_ENABLED_ENV, False)


def overheated_early_promoted() -> bool:
    """Return the single configured decision for the third whitelist slot."""
    return _env_flag(BYBIT_DEMO_EARLY_PROMOTED_ENV, False)


def _read_positive_usd_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return float(default)
    try:
        value = float(raw_value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}_invalid") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}_invalid")
    return value


def reserve_config() -> dict[str, Any]:
    """Read reserve gates with safe defaults and fail-closed validation."""
    try:
        max_exposure = _read_positive_usd_env(
            BYBIT_DEMO_MAX_EXPOSURE_ENV,
            BYBIT_DEMO_MAX_EXPOSURE_USD,
        )
        equity_reserve = _read_positive_usd_env(
            BYBIT_DEMO_EQUITY_RESERVE_ENV,
            BYBIT_DEMO_EQUITY_RESERVE_USD,
        )
    except ValueError as exc:
        return {
            "valid": False,
            "configuration_error": str(exc),
            "max_exposure_usd": None,
            "equity_reserve_usd": None,
        }
    return {
        "valid": True,
        "configuration_error": None,
        "max_exposure_usd": max_exposure,
        "equity_reserve_usd": equity_reserve,
    }


def _read_positive_int_env(name: str, default: int) -> tuple[int, bool]:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return int(default), False
    try:
        value = int(raw_value.strip())
    except (TypeError, ValueError):
        return int(default), True
    if value <= 0:
        return int(default), True
    return value, False


def reserve_health_config() -> dict[str, Any]:
    """Return alert settings, falling back safely when env values are invalid."""
    interval_sec, interval_invalid = _read_positive_int_env(
        BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_ENV,
        BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_SEC,
    )
    window_sec, window_invalid = _read_positive_int_env(
        BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV,
        BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_SEC,
    )
    threshold, threshold_invalid = _read_positive_int_env(
        BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV,
        BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD,
    )
    invalid = []
    if interval_invalid:
        invalid.append(BYBIT_DEMO_PREFLIGHT_HEALTH_INTERVAL_ENV)
    if window_invalid:
        invalid.append(BYBIT_DEMO_PREFLIGHT_ERROR_WINDOW_ENV)
    if threshold_invalid:
        invalid.append(BYBIT_DEMO_PREFLIGHT_ERROR_THRESHOLD_ENV)
    return {
        "interval_sec": interval_sec,
        "window_sec": window_sec,
        "threshold": threshold,
        "configuration_fallback": invalid,
    }


_RESERVE_HEALTH_LOCK = threading.Lock()
_reserve_health_failures: deque[float] = deque()
_reserve_health_alert_active = False
_reserve_health_last_error: str | None = None
_reserve_health_last_error_ts: int | None = None
_reserve_health_last_success_ts: int | None = None
_reserve_health_latest: dict[str, Any] | None = None

_POLL_HEALTH_LOCK = threading.Lock()
_poll_health_last_success_ts: int | None = None


def record_successful_poll(now: float | None = None) -> int:
    """Record the completion of a successful Bybit reconciliation cycle."""
    global _poll_health_last_success_ts
    timestamp = int(time.time() if now is None else now)
    with _POLL_HEALTH_LOCK:
        _poll_health_last_success_ts = timestamp
    return timestamp


def polling_health_status(now: float | None = None) -> dict[str, Any]:
    """Return the poll heartbeat and its explicit freshness decision."""
    current_ts = float(time.time() if now is None else now)
    with _POLL_HEALTH_LOCK:
        last_success = _poll_health_last_success_ts
    stale = (
        last_success is None
        or current_ts - float(last_success) >= BYBIT_DEMO_POLL_STALE_AFTER_SEC
    )
    return {
        "last_successful_poll_at": last_success,
        "polling_stale": stale,
        "polling_stale_after_sec": BYBIT_DEMO_POLL_STALE_AFTER_SEC,
    }


def _reset_poll_health_for_tests() -> None:
    global _poll_health_last_success_ts
    with _POLL_HEALTH_LOCK:
        _poll_health_last_success_ts = None


def _reserve_health_snapshot_values(
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "open_exposure_usd": snapshot.get("open_exposure_usd"),
        "balance_usd": snapshot.get("balance_usd"),
        "unrealized_pnl_usd": snapshot.get("unrealized_pnl_usd"),
        "equity_usd": snapshot.get("equity_usd"),
    }


def record_reserve_health(
    *,
    success: bool,
    error: str | None = None,
    snapshot: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Track real-time reserve read health without deciding order admission."""
    global _reserve_health_alert_active
    global _reserve_health_last_error
    global _reserve_health_last_error_ts
    global _reserve_health_last_success_ts
    global _reserve_health_latest

    current_ts = float(time.time() if now is None else now)
    config = reserve_health_config()
    alert_triggered = False
    with _RESERVE_HEALTH_LOCK:
        if success:
            _reserve_health_failures.clear()
            _reserve_health_alert_active = False
            _reserve_health_last_success_ts = int(current_ts)
            _reserve_health_latest = _reserve_health_snapshot_values(snapshot)
            _reserve_health_last_error = None
            _reserve_health_last_error_ts = None
        else:
            cutoff = current_ts - config["window_sec"]
            while _reserve_health_failures and _reserve_health_failures[0] < cutoff:
                _reserve_health_failures.popleft()
            _reserve_health_failures.append(current_ts)
            _reserve_health_last_error = error or "reserve_preflight_error"
            _reserve_health_last_error_ts = int(current_ts)
            if (
                len(_reserve_health_failures) >= config["threshold"]
                and not _reserve_health_alert_active
            ):
                _reserve_health_alert_active = True
                alert_triggered = True
        return {
            "failure_count": len(_reserve_health_failures),
            "window_sec": config["window_sec"],
            "threshold": config["threshold"],
            "alert_active": _reserve_health_alert_active,
            "alert_triggered": alert_triggered,
            "last_error": _reserve_health_last_error,
            "last_error_ts": _reserve_health_last_error_ts,
            "last_success_ts": _reserve_health_last_success_ts,
            "latest": _reserve_health_latest,
            "configuration_fallback": config["configuration_fallback"],
        }


def reserve_health_status() -> dict[str, Any]:
    """Return safe reserve-read health state for status endpoints."""
    return _reserve_health_status()


def _reserve_health_status() -> dict[str, Any]:
    config = reserve_health_config()
    current_ts = time.time()
    with _RESERVE_HEALTH_LOCK:
        cutoff = current_ts - config["window_sec"]
        while _reserve_health_failures and _reserve_health_failures[0] < cutoff:
            _reserve_health_failures.popleft()
        return {
            "failure_count": len(_reserve_health_failures),
            "window_sec": config["window_sec"],
            "threshold": config["threshold"],
            "interval_sec": config["interval_sec"],
            "alert_active": _reserve_health_alert_active,
            "last_error": _reserve_health_last_error,
            "last_error_ts": _reserve_health_last_error_ts,
            "last_success_ts": _reserve_health_last_success_ts,
            "latest": _reserve_health_latest,
            "configuration_fallback": config["configuration_fallback"],
        }


def _reset_reserve_health_for_tests() -> None:
    global _reserve_health_alert_active
    global _reserve_health_last_error
    global _reserve_health_last_error_ts
    global _reserve_health_last_success_ts
    global _reserve_health_latest
    with _RESERVE_HEALTH_LOCK:
        _reserve_health_failures.clear()
        _reserve_health_alert_active = False
        _reserve_health_last_error = None
        _reserve_health_last_error_ts = None
        _reserve_health_last_success_ts = None
        _reserve_health_latest = None


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
        ret_code: Any | None = None,
        ret_msg: Any | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.retryable = retryable
        self.transport = transport
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.payload = payload


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


def _normalize_price_decimal(
    price: float | str | Decimal,
    tick_size: float | str | Decimal,
    *,
    direction: str,
    is_tp: bool,
) -> Decimal:
    value = _decimal(price, "price")
    tick = _decimal(tick_size, "tick_size")
    rounding = ROUND_UP if (direction == "LONG") == is_tp else ROUND_DOWN
    normalized = (value / tick).to_integral_value(rounding=rounding) * tick
    if normalized <= 0:
        raise BybitDemoSizingError("normalized price is not positive")
    return normalized


def normalize_price(price: float, tick_size: float | str, *, direction: str, is_tp: bool) -> float:
    """Align a barrier to tick size while keeping it on the favorable side."""
    normalized = _normalize_price_decimal(
        price,
        tick_size,
        direction=direction,
        is_tp=is_tp,
    )
    return float(normalized)


def atr_provenance(
    atr_value: float | None,
    atr_candle_close_ts: int | None = None,
) -> dict[str, Any]:
    """Return the persisted ATR snapshot contract for a new TP plan."""
    if atr_value is None:
        if atr_candle_close_ts is not None:
            raise BybitDemoSizingError("fixed ATR fallback cannot have a candle timestamp")
        return {
            "atr_value": None,
            "atr_period": None,
            "atr_timeframe": None,
            "atr_method": None,
            "atr_candle_close_ts": None,
            "atr_source": "fixed_fallback",
        }
    value = _decimal(atr_value, "atr_value")
    if (
        isinstance(atr_candle_close_ts, bool)
        or not isinstance(atr_candle_close_ts, int)
        or atr_candle_close_ts <= 0
    ):
        raise BybitDemoSizingError("ATR candle close timestamp is required")
    return {
        "atr_value": float(value),
        "atr_period": 14,
        "atr_timeframe": "4h",
        "atr_method": "wilder",
        "atr_candle_close_ts": atr_candle_close_ts,
        "atr_source": "gateio_4h",
    }


def _tp_plan_quantities(
    total_qty: Decimal,
    minimum: Decimal,
    step: Decimal,
    shares: tuple[Decimal, ...],
) -> list[Decimal] | None:
    quantities: list[Decimal] = []
    allocated = Decimal("0")
    for share in shares[:-1]:
        quantity = (
            (total_qty * share / step).to_integral_value(rounding=ROUND_DOWN)
            * step
        )
        quantities.append(quantity)
        allocated += quantity
    quantities.append(total_qty - allocated)
    if any(quantity < minimum or quantity <= 0 for quantity in quantities):
        return None
    return quantities


def _tp_plan_prices(
    direction: str,
    entry: Decimal,
    atr_value: Decimal,
    tick_size: Decimal,
    multipliers: tuple[Decimal, ...],
) -> list[Decimal] | None:
    sign = Decimal("1") if direction == "LONG" else Decimal("-1")
    prices = [
        _normalize_price_decimal(
            entry + sign * atr_value * multiplier,
            tick_size,
            direction=direction,
            is_tp=True,
        )
        for multiplier in multipliers
    ]
    if direction == "LONG":
        valid = all(left < right for left, right in zip([entry, *prices], prices))
    else:
        valid = all(left > right for left, right in zip([entry, *prices], prices))
    return prices if valid else None


def calculate_multi_tp_plan(
    *,
    direction: str,
    entry_price: float,
    sl_price: float,
    atr_value: float | str,
    executed_qty: float,
    min_order_qty: float | str,
    qty_step: float | str,
    tick_size: float | str,
    tp_count: int,
) -> dict[str, Any]:
    """Build a pure ATR-based TP plan without writing to SQLite or Bybit."""
    if direction not in {"LONG", "SHORT"}:
        raise BybitDemoSizingError("invalid direction")
    if tp_count not in _TP_PLAN_FALLBACKS:
        raise BybitDemoSizingError("tp_count must be 3 or 5")
    entry = _decimal(entry_price, "entry_price")
    stop = _decimal(sl_price, "sl_price")
    if (direction == "LONG" and stop >= entry) or (
        direction == "SHORT" and stop <= entry
    ):
        raise BybitDemoSizingError("stop loss is not on the losing side")
    atr = _decimal(atr_value, "atr_value")
    if atr <= 0:
        raise BybitDemoSizingError("atr_value must be positive")
    total_qty = _decimal(executed_qty, "executed_qty")
    minimum = _decimal(min_order_qty, "min_order_qty")
    step = _decimal(qty_step, "qty_step")
    tick = _decimal(tick_size, "tick_size")
    aligned_total = (
        (total_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    )
    if aligned_total != total_qty:
        raise BybitDemoSizingError("executed_qty is not qty-step aligned")
    if total_qty < minimum:
        raise BybitDemoSizingError("executed_qty is below instrument minimum")

    requested_splits = [
        float(Decimal(value)) for value in _TP_PLAN_SCHEDULES[tp_count][1]
    ]
    last_rejected_reason: str | None = None
    for effective_count in _TP_PLAN_FALLBACKS[tp_count]:
        multiplier_values, split_values = _TP_PLAN_SCHEDULES[effective_count]
        multipliers = tuple(Decimal(value) for value in multiplier_values)
        shares = tuple(Decimal(value) for value in split_values)
        prices = _tp_plan_prices(direction, entry, atr, tick, multipliers)
        if prices is None:
            last_rejected_reason = "tick_size"
            continue
        quantities = _tp_plan_quantities(total_qty, minimum, step, shares)
        if quantities is None:
            last_rejected_reason = "min_order_qty"
            continue
        last_fallback_reason = (
            None if effective_count == tp_count else last_rejected_reason
        )
        return {
            "tp_plan_version": BYBIT_DEMO_TP_PLAN_VERSION,
            "direction": direction,
            "atr_value": float(atr),
            "executed_qty": float(total_qty),
            "requested_tp_count": tp_count,
            "effective_tp_count": effective_count,
            "requested_split": requested_splits,
            "effective_split": [float(value) for value in shares],
            "last_fallback_reason": last_fallback_reason,
            "legs": [
                {
                    "leg_index": index,
                    "target_multiplier": float(multiplier),
                    "target_price": float(price),
                    "planned_share": float(share),
                    "planned_qty": float(quantity),
                }
                for index, (multiplier, price, share, quantity) in enumerate(
                    zip(multipliers, prices, shares, quantities),
                    start=1,
                )
            ],
        }
    raise BybitDemoSizingError(
        "no valid TP plan for instrument filters "
        f"({last_rejected_reason or 'unknown'})"
    )


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
                ret_msg = payload_json.get("retMsg")
                # Keep the API response structured and on one log line.  The
                # JSON encoder escapes newlines/control characters in retMsg
                # and preserves the complete response for later diagnosis.
                logger.warning(
                    "bybit_api_error endpoint=%s retCode=%s retMsg=%s payload=%s",
                    endpoint,
                    ret_code,
                    json.dumps(ret_msg, ensure_ascii=False),
                    _json(payload_json),
                )
                raise BybitDemoError(
                    endpoint,
                    f"bybit_ret_code_{ret_code}",
                    retryable=int(ret_code) in {10006, 10016},
                    ret_code=ret_code,
                    ret_msg=ret_msg,
                    payload=payload_json,
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
            raise BybitDemoError(
                "/v5/market/instruments-info",
                "symbol_not_found",
                payload=payload,
                ret_code=payload.get("retCode"),
                ret_msg=payload.get("retMsg"),
            )
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
        take_profit: float | None = None,
        stop_loss: float | None = None,
        order_link_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        body = {
            "category": BYBIT_DEMO_CATEGORY,
            "symbol": symbol.upper(),
            "side": (
                ("Sell" if direction == "LONG" else "Buy")
                if reduce_only
                else ("Buy" if direction == "LONG" else "Sell")
            ),
            "orderType": "Market",
            "qty": _decimal_text(_decimal(qty, "qty")),
            "positionIdx": 0,
            "orderLinkId": order_link_id,
        }
        if reduce_only:
            body["reduceOnly"] = True
        else:
            if stop_loss is None:
                raise ValueError("stop_loss_required")
            body["stopLoss"] = _decimal_text(_decimal(stop_loss, "stop_loss"))
            body["tpslMode"] = "Full"
            body["slOrderType"] = "Market"
            body["slTriggerBy"] = "MarkPrice"
            if take_profit is not None:
                body.update(
                    {
                        "takeProfit": _decimal_text(_decimal(take_profit, "take_profit")),
                        "tpslMode": "Full",
                        "tpOrderType": "Market",
                        "tpTriggerBy": "MarkPrice",
                    }
                )
        payload = self._request("POST", "/v5/order/create", body=body)
        result = payload.get("result") or {}
        return result if isinstance(result, dict) else {}

    def create_limit_tp_order(
        self,
        *,
        symbol: str,
        direction: str,
        qty: float,
        price: float,
        order_link_id: str,
    ) -> dict[str, Any]:
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        body = {
            "category": BYBIT_DEMO_CATEGORY,
            "symbol": symbol.upper(),
            "side": "Sell" if direction == "LONG" else "Buy",
            "orderType": "Limit",
            "qty": _decimal_text(_decimal(qty, "qty")),
            "price": _decimal_text(_decimal(price, "price")),
            "timeInForce": "GTC",
            "positionIdx": 0,
            "reduceOnly": True,
            "orderLinkId": order_link_id,
        }
        payload = self._request("POST", "/v5/order/create", body=body)
        result = payload.get("result") or {}
        return result if isinstance(result, dict) else {}

    def set_trading_stop(
        self,
        *,
        symbol: str,
        stop_loss: float,
        sl_trigger_by: str = "MarkPrice",
    ) -> dict[str, Any]:
        body = {
            "category": BYBIT_DEMO_CATEGORY,
            "symbol": symbol.upper(),
            "tpslMode": "Full",
            "stopLoss": _decimal_text(_decimal(stop_loss, "stop_loss")),
            "slTriggerBy": sl_trigger_by,
            "positionIdx": 0,
        }
        payload = self._request(
            "POST",
            "/v5/position/trading-stop",
            body=body,
        )
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

    def cancel_order(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        order_link_id: str | None = None,
    ) -> dict[str, Any]:
        if bool(order_id) == bool(order_link_id):
            raise ValueError("exactly_one_order_selector_required")
        body = {
            "category": BYBIT_DEMO_CATEGORY,
            "symbol": symbol.upper(),
            "positionIdx": 0,
        }
        if order_id:
            body["orderId"] = order_id
        else:
            body["orderLinkId"] = order_link_id
        payload = self._request("POST", "/v5/order/cancel", body=body)
        result = payload.get("result") or {}
        return result if isinstance(result, dict) else {}

    def get_position(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v5/position/list",
            params={"category": BYBIT_DEMO_CATEGORY, "symbol": symbol.upper()},
            retry_safe=True,
        )
        return self._result_list(payload)

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Return all open linear positions used by the reserve preflight."""
        payload = self._request(
            "GET",
            "/v5/position/list",
            params={"category": BYBIT_DEMO_CATEGORY, "settleCoin": "USDT"},
            retry_safe=True,
        )
        return self._result_list(payload)

    def get_wallet_balance(self) -> list[dict[str, Any]]:
        """Return the unified USDT wallet account for reserve calculations."""
        payload = self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED", "coin": "USDT"},
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
            exchange_created_time INTEGER,
            exchange_exec_time INTEGER,
            ts_closed INTEGER,
            last_polled INTEGER,
            last_error TEXT,
            raw_order_json TEXT,
            raw_position_json TEXT,
            raw_execution_json TEXT,
            preflight_decision TEXT,
            preflight_reason TEXT,
            preflight_open_exposure_usd REAL,
            preflight_new_notional_usd REAL,
            preflight_max_exposure_usd REAL,
            preflight_balance_usd REAL,
            preflight_unrealized_pnl_usd REAL,
            preflight_equity_usd REAL,
            preflight_equity_reserve_usd REAL,
            preflight_ts INTEGER,
            shadow_origin INTEGER,
            pre_gate_exception INTEGER NOT NULL DEFAULT 0,
            post_fix_leak INTEGER NOT NULL DEFAULT 0,
            gate_classification_uncertain INTEGER NOT NULL DEFAULT 0,
            fallback_pre_gate_exception INTEGER NOT NULL DEFAULT 0,
            fallback_post_fix_leak INTEGER NOT NULL DEFAULT 0,
            tp_plan_version TEXT,
            atr_value REAL,
            atr_period INTEGER,
            atr_timeframe TEXT,
            atr_method TEXT,
            atr_candle_close_ts INTEGER,
            atr_source TEXT,
            requested_tp_count INTEGER,
            effective_tp_count INTEGER,
            requested_split_json TEXT,
            effective_split_json TEXT,
             be_state TEXT NOT NULL DEFAULT 'not_armed',
            be_price REAL,
            be_set_ts INTEGER,
             be_pending_since_ts INTEGER,
             be_readback_attempts INTEGER NOT NULL DEFAULT 0,
            protection_state TEXT NOT NULL DEFAULT 'legacy_full'
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
        ("exchange_created_time", "INTEGER"),
        ("exchange_exec_time", "INTEGER"),
        ("preflight_decision", "TEXT"),
        ("preflight_reason", "TEXT"),
        ("preflight_open_exposure_usd", "REAL"),
        ("preflight_new_notional_usd", "REAL"),
        ("preflight_max_exposure_usd", "REAL"),
        ("preflight_balance_usd", "REAL"),
        ("preflight_unrealized_pnl_usd", "REAL"),
        ("preflight_equity_usd", "REAL"),
        ("preflight_equity_reserve_usd", "REAL"),
        ("preflight_ts", "INTEGER"),
        ("origin", "TEXT NOT NULL DEFAULT 'signal'"),
        ("reversal_id", "INTEGER"),
        ("tp_plan_version", "TEXT"),
        ("atr_value", "REAL"),
        ("atr_period", "INTEGER"),
        ("atr_timeframe", "TEXT"),
        ("atr_method", "TEXT"),
        ("atr_candle_close_ts", "INTEGER"),
        ("atr_source", "TEXT"),
        ("requested_tp_count", "INTEGER"),
        ("effective_tp_count", "INTEGER"),
        ("requested_split_json", "TEXT"),
        ("effective_split_json", "TEXT"),
        ("be_state", "TEXT NOT NULL DEFAULT 'not_armed'"),
        ("be_price", "REAL"),
        ("be_set_ts", "INTEGER"),
        ("be_pending_since_ts", "INTEGER"),
        ("be_readback_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("protection_state", "TEXT NOT NULL DEFAULT 'legacy_full'"),
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bybit_demo_tp_legs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_id INTEGER NOT NULL,
            leg_index INTEGER NOT NULL,
            target_multiplier REAL NOT NULL,
            target_price REAL NOT NULL,
            planned_share REAL NOT NULL,
            planned_qty REAL NOT NULL,
            executed_qty REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned',
            order_id TEXT,
            order_link_id TEXT,
            avg_exit_price REAL,
            realized_pnl_usd REAL,
            fee_usd REAL,
            reversal_id INTEGER,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            filled_ts INTEGER,
            cancelled_ts INTEGER,
            last_error TEXT,
            raw_order_json TEXT,
            raw_execution_json TEXT,
            UNIQUE (ledger_id, leg_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bybit_demo_tp_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_id INTEGER NOT NULL,
            leg_id INTEGER,
            event_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            order_id TEXT,
            order_link_id TEXT,
            reversal_id INTEGER,
            requested_qty REAL,
            executed_qty REAL,
            position_size_before REAL,
            position_size_after REAL,
            price REAL,
            realized_pnl_usd REAL,
            fee_usd REAL,
            status TEXT,
            reason TEXT,
            event_ts REAL NOT NULL,
            raw_order_json TEXT,
            raw_position_json TEXT,
            raw_execution_json TEXT,
            UNIQUE (ledger_id, idempotency_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bybit_demo_tp_leg_status "
        "ON bybit_demo_tp_legs(ledger_id, status, leg_index)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bybit_demo_tp_event_time "
        "ON bybit_demo_tp_events(ledger_id, event_ts, id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bybit_demo_reversals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            source_signal_key TEXT NOT NULL UNIQUE,
            source_direction TEXT NOT NULL,
            target_direction TEXT NOT NULL,
            source_ledger_ids_json TEXT,
            current_ledger_id INTEGER,
            state TEXT NOT NULL,
            reversal_claimed INTEGER NOT NULL DEFAULT 1,
            reversal_used INTEGER NOT NULL DEFAULT 0,
            close_attempts INTEGER NOT NULL DEFAULT 0,
            tp_legs_cancelled INTEGER NOT NULL DEFAULT 0,
            tp_cancel_errors INTEGER NOT NULL DEFAULT 0,
            close_deadline_ts REAL NOT NULL,
            close_order_id TEXT,
            close_order_link_id TEXT,
            close_order_status TEXT,
            close_requested_qty REAL,
            close_executed_qty REAL NOT NULL DEFAULT 0,
            position_size_before REAL,
            position_size_after REAL,
            claimed_ts INTEGER NOT NULL,
            used_ts INTEGER,
            recovery_reason TEXT,
            last_error TEXT,
            raw_position_json TEXT,
            raw_order_json TEXT,
            raw_execution_json TEXT,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL
        )
        """
    )
    for column, definition in (
        ("tp_legs_cancelled", "INTEGER NOT NULL DEFAULT 0"),
        ("tp_cancel_errors", "INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(
                f"ALTER TABLE bybit_demo_reversals ADD COLUMN {column} {definition}"
            )
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bybit_demo_reversal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reversal_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            attempt_no INTEGER,
            order_link_id TEXT,
            order_id TEXT,
            requested_qty REAL,
            executed_qty REAL,
            position_size_before REAL,
            position_size_after REAL,
            status TEXT,
            reason TEXT,
            event_ts REAL NOT NULL,
            raw_order_json TEXT,
            raw_position_json TEXT,
            raw_execution_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bybit_demo_active_reversal_symbol
        ON bybit_demo_reversals(symbol)
        WHERE state IN (
            'CLAIMED', 'CLOSING', 'OPEN_PENDING',
            'ACTIVE_AFTER_REVERSAL', 'RECOVERY_REQUIRED'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bybit_demo_reversal_state "
        "ON bybit_demo_reversals(state, symbol)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bybit_demo_reversal_event "
        "ON bybit_demo_reversal_events(reversal_id, attempt_no)"
    )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _raw_error_field(endpoint: str) -> str:
    """Choose the ledger raw-response column for a failed API call."""
    if endpoint.endswith("/position/list"):
        return "raw_position_json"
    if endpoint.endswith("/execution/list"):
        return "raw_execution_json"
    # An order-intent row owns instrument-info, order-realtime, and
    # order-create failures, so retain those responses with the order record.
    return "raw_order_json"


def _error_payload_fields(exc: BybitDemoError) -> dict[str, str]:
    """Return raw-response fields without changing the existing ledger shape."""
    if exc.payload is None:
        return {}
    return {_raw_error_field(exc.endpoint): _json(exc.payload)}


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
        "exchange_created_time",
        "exchange_exec_time",
        "ts_closed",
        "last_polled",
        "last_error",
        "raw_order_json",
        "raw_position_json",
        "raw_execution_json",
        "preflight_decision",
        "preflight_reason",
        "preflight_open_exposure_usd",
        "preflight_new_notional_usd",
        "preflight_max_exposure_usd",
        "preflight_balance_usd",
        "preflight_unrealized_pnl_usd",
        "preflight_equity_usd",
        "preflight_equity_reserve_usd",
        "preflight_ts",
        "pre_gate_exception",
        "post_fix_leak",
        "gate_classification_uncertain",
        "fallback_pre_gate_exception",
        "fallback_post_fix_leak",
        "origin",
        "reversal_id",
        "tp_plan_version",
        "atr_value",
        "atr_period",
        "atr_timeframe",
        "atr_method",
        "atr_candle_close_ts",
        "atr_source",
        "requested_tp_count",
        "effective_tp_count",
        "requested_split_json",
        "effective_split_json",
        "be_state",
        "be_price",
        "be_set_ts",
        "be_pending_since_ts",
        "be_readback_attempts",
        "protection_state",
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


def _update_tp_leg(
    conn: Any,
    db_lock: threading.Lock,
    leg_id: int,
    **fields: Any,
) -> None:
    allowed = {
        "executed_qty",
        "status",
        "order_id",
        "order_link_id",
        "reversal_id",
        "avg_exit_price",
        "realized_pnl_usd",
        "fee_usd",
        "filled_ts",
        "cancelled_ts",
        "last_error",
        "raw_order_json",
        "raw_execution_json",
        "updated_ts",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"invalid TP leg fields: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [leg_id]
    with db_lock:
        conn.execute(
            f"UPDATE bybit_demo_tp_legs SET {assignments} WHERE id=?",
            values,
        )
        conn.commit()


def _insert_tp_leg(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    *,
    leg_index: int,
    target_multiplier: float,
    target_price: float,
    planned_share: float,
    planned_qty: float,
    now: int | None = None,
) -> int:
    """Insert one immutable TP-plan leg and return its ledger-local row id."""
    created_ts = int(time.time() if now is None else now)
    with db_lock:
        cursor = conn.execute(
            """
            INSERT INTO bybit_demo_tp_legs (
                ledger_id, leg_index, target_multiplier, target_price,
                planned_share, planned_qty, created_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger_id,
                leg_index,
                target_multiplier,
                target_price,
                planned_share,
                planned_qty,
                created_ts,
                created_ts,
            ),
        )
        conn.commit()
    return int(cursor.lastrowid)


def _tp_leg_order_link_id(parent_order_link_id: str, leg_index: int) -> str:
    digest = hashlib.sha256(
        f"{parent_order_link_id}|tp|{int(leg_index)}".encode("utf-8")
    ).hexdigest()
    return "btp" + digest[:29]


def _tp_create_error_kind(exc: BaseException) -> str:
    if not isinstance(exc, BybitDemoError):
        return "ambiguous"
    try:
        ret_code = int(exc.ret_code) if exc.ret_code is not None else None
    except (TypeError, ValueError):
        ret_code = None
    if ret_code in _TP_CREATE_DUPLICATE_CODES:
        return "duplicate"
    if ret_code in _TP_CREATE_DEFINITE_REJECT_CODES:
        return "rejected"
    return "ambiguous"


def _tp_plan_error(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    reason: str,
    *,
    state: str = "recovery_required",
) -> None:
    _update_row(
        conn,
        db_lock,
        ledger_id,
        protection_state=state,
        last_error=reason,
    )
    logger.warning(
        "bybit_demo_tp_setup_failed ledger_id=%d state=%s reason=%s",
        ledger_id,
        state,
        reason,
    )


def _ensure_tp_plan_rows(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    plan: dict[str, Any],
    *,
    now: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leg in plan["legs"]:
        leg_index = int(leg["leg_index"])
        with db_lock:
            cursor = conn.execute(
                """
                SELECT * FROM bybit_demo_tp_legs
                WHERE ledger_id=? AND leg_index=?
                """,
                (ledger_id, leg_index),
            )
            existing = cursor.fetchone()
            description = cursor.description
        if existing:
            row = {
                column[0]: existing[index]
                for index, column in enumerate(description)
            }
            expected = (
                float(leg["target_price"]),
                float(leg["planned_qty"]),
            )
            actual = (float(row["target_price"]), float(row["planned_qty"]))
            if actual != expected:
                raise BybitDemoSizingError(
                    f"persisted TP leg {leg_index} does not match plan"
                )
            if not row.get("order_link_id"):
                order_link_id = _tp_leg_order_link_id(
                    str(( _get_row(conn, db_lock, ledger_id) or {}).get(
                        "order_link_id", ""
                    )),
                    leg_index,
                )
                _update_tp_leg(
                    conn,
                    db_lock,
                    int(row["id"]),
                    order_link_id=order_link_id,
                    updated_ts=now,
                )
                row["order_link_id"] = order_link_id
            rows.append(row)
            continue
        leg_id = _insert_tp_leg(
            conn,
            db_lock,
            ledger_id,
            leg_index=leg_index,
            target_multiplier=float(leg["target_multiplier"]),
            target_price=float(leg["target_price"]),
            planned_share=float(leg["planned_share"]),
            planned_qty=float(leg["planned_qty"]),
            now=now,
        )
        order_link_id = _tp_leg_order_link_id(
            str(( _get_row(conn, db_lock, ledger_id) or {}).get(
                "order_link_id", ""
            )),
            leg_index,
        )
        _update_tp_leg(
            conn,
            db_lock,
            leg_id,
            order_link_id=order_link_id,
            updated_ts=now,
        )
        rows.append(
            {
                "id": leg_id,
                "ledger_id": ledger_id,
                "leg_index": leg_index,
                "target_price": float(leg["target_price"]),
                "planned_qty": float(leg["planned_qty"]),
                "status": "planned",
                "order_id": None,
                "order_link_id": order_link_id,
            }
        )
    return rows


def _get_tp_leg_rows(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
) -> list[dict[str, Any]]:
    with db_lock:
        cursor = conn.execute(
            """
            SELECT * FROM bybit_demo_tp_legs
            WHERE ledger_id=?
            ORDER BY leg_index
            """,
            (ledger_id,),
        )
        return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _place_tp_leg(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    parent: dict[str, Any],
    leg: dict[str, Any],
    now: int,
) -> str:
    leg_id = int(leg["id"])
    order_link_id = str(leg["order_link_id"])
    symbol = str(parent["symbol"])
    direction = str(parent["direction"])
    try:
        remote = client.get_order_realtime(
            symbol=symbol,
            order_link_id=order_link_id,
        )
        if remote:
            order = remote[0]
            executed_qty = float(order.get("cumExecQty") or 0)
            _update_tp_leg(
                conn,
                db_lock,
                leg_id,
                status=_tp_leg_status(order.get("orderStatus"), executed_qty),
                order_id=str(order.get("orderId") or "") or None,
                order_link_id=order_link_id,
                executed_qty=executed_qty,
                avg_exit_price=float(order.get("avgPrice") or 0) or None,
                raw_order_json=_json(order),
                updated_ts=now,
                filled_ts=now if str(order.get("orderStatus", "")).lower() == "filled" else None,
            )
            return "existing"

        result = client.create_limit_tp_order(
            symbol=symbol,
            direction=direction,
            qty=float(leg["planned_qty"]),
            price=float(leg["target_price"]),
            order_link_id=order_link_id,
        )
        executed_qty = float(result.get("cumExecQty") or 0)
        order_id = str(result.get("orderId") or "") or None
        _update_tp_leg(
            conn,
            db_lock,
            leg_id,
            status=_tp_leg_status(result.get("orderStatus"), executed_qty),
            order_id=order_id,
            order_link_id=order_link_id,
            executed_qty=executed_qty,
            avg_exit_price=float(result.get("avgPrice") or 0) or None,
            raw_order_json=_json(result),
            updated_ts=now,
            filled_ts=now if str(result.get("orderStatus", "")).lower() == "filled" else None,
        )
        if not order_id:
            return "ambiguous"
        return "created"
    except Exception as exc:
        kind = _tp_create_error_kind(exc)
        if kind == "duplicate":
            try:
                remote = client.get_order_realtime(
                    symbol=symbol,
                    order_link_id=order_link_id,
                )
            except Exception:
                remote = []
            if remote:
                order = remote[0]
                executed_qty = float(order.get("cumExecQty") or 0)
                _update_tp_leg(
                    conn,
                    db_lock,
                    leg_id,
                    status=_tp_leg_status(order.get("orderStatus"), executed_qty),
                    order_id=str(order.get("orderId") or "") or None,
                    order_link_id=order_link_id,
                    executed_qty=executed_qty,
                    raw_order_json=_json(order),
                    updated_ts=now,
                )
                return "existing"
        if isinstance(exc, BybitDemoError):
            reason = (
                f"tp_leg_create_{kind}:ret_code_{exc.ret_code}"
                if exc.ret_code is not None
                else f"tp_leg_create_{kind}:{exc}"
            )
            raw_order_json = _json(exc.payload)
        else:
            reason = f"tp_leg_create_{kind}:{exc}"
            raw_order_json = None
        _update_tp_leg(
            conn,
            db_lock,
            leg_id,
            status="rejected" if kind == "rejected" else "submitted",
            order_link_id=order_link_id,
            last_error=reason,
            raw_order_json=raw_order_json,
            updated_ts=now,
        )
        return kind


def _ensure_tp_orders_for_ledger(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
    now: int | None = None,
) -> dict[str, Any]:
    now = int(time.time() if now is None else now)
    row = _get_row(conn, db_lock, ledger_id)
    if not row or not bybit_demo_multi_tp_enabled():
        return {"status": "disabled", "ledger_id": ledger_id}
    if row.get("protection_state") == "legacy_full":
        return {"status": "legacy", "ledger_id": ledger_id}
    if row.get("protection_state") == BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE:
        return {"status": "manual_recovery_required", "ledger_id": ledger_id}
    if row.get("protection_state") in {
        "armed",
        BYBIT_DEMO_TP_PARTIAL_STATE,
        BYBIT_DEMO_TP_ABANDONED_STATE,
    }:
        return {
            "status": str(row["protection_state"]),
            "ledger_id": ledger_id,
        }
    submitted_ts = row.get("ts_submitted") or row.get("ts_created") or now
    if now > int(submitted_ts) + int(BYBIT_DEMO_TP_SETUP_DEADLINE_SEC):
        _tp_plan_error(
            conn,
            db_lock,
            ledger_id,
            "tp_setup_deadline_exceeded",
        )
        return {"status": "recovery_required", "ledger_id": ledger_id}

    if not row.get("executed_qty"):
        order_items = client.get_order_realtime(
            symbol=row["symbol"],
            order_id=str(row["order_id"]) if row.get("order_id") else None,
            order_link_id=None if row.get("order_id") else row["order_link_id"],
        )
        if order_items:
            _record_order(conn, db_lock, ledger_id, order_items[0], now)
            row = _get_row(conn, db_lock, ledger_id) or row
        if not row.get("executed_qty"):
            _update_row(
                conn,
                db_lock,
                ledger_id,
                protection_state="awaiting_entry_fill",
            )
            return {"status": "awaiting_entry_fill", "ledger_id": ledger_id}

    try:
        plan = calculate_multi_tp_plan(
            direction=str(row["direction"]),
            entry_price=float(row.get("avg_entry_price") or row["entry_price"]),
            sl_price=float(row["sl_price"]),
            atr_value=row.get("atr_value"),
            executed_qty=float(row["executed_qty"]),
            min_order_qty=float(row["min_order_qty"]),
            qty_step=float(row["qty_step"]),
            tick_size=float(row["tick_size"]),
            tp_count=BYBIT_DEMO_TP_COUNT,
        )
    except BybitDemoSizingError as exc:
        _tp_plan_error(conn, db_lock, ledger_id, f"tp_plan_failed:{exc}", state="tp_plan_failed")
        return {"status": "tp_plan_failed", "ledger_id": ledger_id}

    _update_row(
        conn,
        db_lock,
        ledger_id,
        tp_plan_version=plan["tp_plan_version"],
        atr_value=plan["atr_value"],
        requested_tp_count=plan["requested_tp_count"],
        effective_tp_count=plan["effective_tp_count"],
        requested_split_json=_json(plan["requested_split"]),
        effective_split_json=_json(plan["effective_split"]),
        protection_state="placing",
        last_error=None,
    )
    legs = _ensure_tp_plan_rows(conn, db_lock, ledger_id, plan, now=now)
    _tp_event(
        conn,
        db_lock,
        ledger_id,
        "plan_created",
        f"tp-plan:{plan['tp_plan_version']}:{plan['effective_tp_count']}",
        status="planned",
        reason=plan.get("last_fallback_reason"),
        event_ts=now,
    )
    parent = _get_row(conn, db_lock, ledger_id) or row
    if any(str(leg.get("status") or "").lower() == "rejected" for leg in legs):
        _tp_plan_error(
            conn,
            db_lock,
            ledger_id,
            "tp_leg_deterministic_reject_requires_manual_recovery",
            state=BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE,
        )
        return {"status": "manual_recovery_required", "ledger_id": ledger_id}
    results = [
        _place_tp_leg(
            conn,
            db_lock,
            client,
            parent=parent,
            leg=leg,
            now=now,
        )
        for leg in legs
        if not leg.get("order_id")
        and str(leg.get("status") or "").lower() != "rejected"
    ]
    if "rejected" in results:
        _tp_plan_error(
            conn,
            db_lock,
            ledger_id,
            "tp_leg_deterministic_reject_requires_manual_recovery",
            state=BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE,
        )
        return {"status": "manual_recovery_required", "ledger_id": ledger_id}
    if any(result in {"ambiguous", "duplicate"} for result in results):
        _tp_plan_error(conn, db_lock, ledger_id, "tp_leg_placement_recovery_required")
        return {"status": "recovery_required", "ledger_id": ledger_id}
    _update_row(
        conn,
        db_lock,
        ledger_id,
        protection_state="armed",
        last_error=None,
    )
    return {
        "status": "armed",
        "ledger_id": ledger_id,
        "effective_tp_count": int(plan["effective_tp_count"]),
    }


def _safe_post_entry_tp_setup(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
) -> dict[str, Any]:
    """Degrade TP setup without changing a successfully submitted entry."""
    try:
        return _ensure_tp_orders_for_ledger(
            conn,
            db_lock,
            client,
            ledger_id=ledger_id,
        )
    except Exception as exc:
        reason = f"tp_setup_post_entry_error:{type(exc).__name__}:{exc}"
        _tp_plan_error(conn, db_lock, ledger_id, reason)
        return {"status": "recovery_required", "ledger_id": ledger_id}


def manual_tp_recovery_snapshot(
    conn: Any,
    db_lock: threading.Lock,
    *,
    max_rows: int = 50,
) -> dict[str, Any]:
    """Return non-mutating details for operator review of manual recovery rows."""
    if not bybit_demo_multi_tp_enabled():
        return {"status": "disabled", "rows": []}
    with db_lock:
        cursor = conn.execute(
            """
            SELECT id, symbol, direction, status, protection_state,
                   last_error, requested_tp_count, effective_tp_count,
                   ts_submitted
            FROM bybit_demo_positions
            WHERE protection_state=?
            ORDER BY id
            LIMIT ?
            """,
            (BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE, int(max_rows)),
        )
        parents = [_row_dict(cursor, row) for row in cursor.fetchall()]
    rows = []
    for parent in parents:
        legs = _get_tp_leg_rows(conn, db_lock, int(parent["id"]))
        rows.append(
            {
                "ledger_id": int(parent["id"]),
                "symbol": parent["symbol"],
                "direction": parent["direction"],
                "status": parent["status"],
                "protection_state": parent["protection_state"],
                "last_error": parent["last_error"],
                "requested_tp_count": parent["requested_tp_count"],
                "effective_tp_count": parent["effective_tp_count"],
                "ts_submitted": parent["ts_submitted"],
                "legs": [
                    {
                        "id": int(leg["id"]),
                        "leg_index": int(leg["leg_index"]),
                        "status": leg["status"],
                        "order_id": leg["order_id"],
                        "order_link_id": leg["order_link_id"],
                        "target_price": leg["target_price"],
                        "planned_qty": leg["planned_qty"],
                        "executed_qty": leg["executed_qty"],
                        "last_error": leg["last_error"],
                    }
                    for leg in legs
                ],
            }
        )
    return {"status": "ok", "rows": rows}


def manual_breakeven_snapshot(
    conn: Any,
    db_lock: threading.Lock,
    *,
    max_rows: int = 50,
) -> dict[str, Any]:
    """Return non-mutating details for BE recovery rows."""
    if not bybit_demo_multi_tp_enabled() or not bybit_demo_breakeven_enabled():
        return {"status": "disabled", "rows": []}
    with db_lock:
        cursor = conn.execute(
            """
            SELECT id, symbol, direction, status, protection_state,
                   be_state, be_price, be_set_ts, be_pending_since_ts,
                   be_readback_attempts, last_error
            FROM bybit_demo_positions
            WHERE be_state=?
            ORDER BY id
            LIMIT ?
            """,
            (BYBIT_DEMO_BE_RECOVERY_STATE, int(max_rows)),
        )
        rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    return {
        "status": "ok",
        "rows": [
            {
                "ledger_id": int(row["id"]),
                "symbol": row["symbol"],
                "direction": row["direction"],
                "status": row["status"],
                "protection_state": row["protection_state"],
                "be_state": row["be_state"],
                "be_price": row["be_price"],
                "be_set_ts": row["be_set_ts"],
                "be_pending_since_ts": row["be_pending_since_ts"],
                "be_readback_attempts": row["be_readback_attempts"],
                "last_error": row["last_error"],
            }
            for row in rows
        ],
    }


def _manual_recovery_event(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    action: str,
    *,
    status: str,
    reason: str,
    now: int,
) -> None:
    _tp_event(
        conn,
        db_lock,
        ledger_id,
        f"manual_recovery_{action}",
        f"manual-recovery:{action}:{ledger_id}:{now}",
        status=status,
        reason=reason,
        event_ts=now,
    )


def manual_recover_breakeven(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
    action: str,
    reason: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Run one explicit operator retry for a failed BE mutation."""
    now = int(time.time() if now is None else now)
    if (
        not client.enabled
        or not bybit_demo_multi_tp_enabled()
        or not bybit_demo_breakeven_enabled()
    ):
        return {"status": "disabled", "ledger_id": ledger_id}
    if action != "retry_breakeven":
        return {"status": "invalid_action", "ledger_id": ledger_id}

    row = _get_row(conn, db_lock, ledger_id)
    if not row:
        return {"status": "not_found", "ledger_id": ledger_id}
    state = str(row.get("be_state") or BYBIT_DEMO_BE_NOT_ARMED_STATE)
    if state == BYBIT_DEMO_BE_ARMED_STATE:
        return {"status": BYBIT_DEMO_BE_ARMED_STATE, "ledger_id": ledger_id}
    if state != BYBIT_DEMO_BE_RECOVERY_STATE:
        return {
            "status": "invalid_state",
            "ledger_id": ledger_id,
            "be_state": state,
        }

    retry_reason = (reason or "operator_retry_breakeven").strip()[:500]
    _update_row(
        conn,
        db_lock,
        ledger_id,
        be_state=BYBIT_DEMO_BE_NOT_ARMED_STATE,
        be_price=None,
        be_pending_since_ts=None,
        be_readback_attempts=0,
        last_error=None,
    )
    _breakeven_event(
        conn,
        db_lock,
        ledger_id,
        "manual_retry",
        status=BYBIT_DEMO_BE_NOT_ARMED_STATE,
        reason=retry_reason,
        now=now,
    )
    return ensure_breakeven_sl(
        conn,
        db_lock,
        client,
        ledger_id=ledger_id,
        now=now,
    )


def manual_recover_tp_orders(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
    action: str,
    reason: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Run one explicit operator recovery action without the unattended deadline."""
    now = int(time.time() if now is None else now)
    if not client.enabled or not bybit_demo_multi_tp_enabled():
        return {"status": "disabled", "ledger_id": ledger_id}
    if action not in {"retry_all", "accept_partial", "abandon"}:
        return {"status": "invalid_action", "ledger_id": ledger_id}

    row = _get_row(conn, db_lock, ledger_id)
    if not row:
        return {"status": "not_found", "ledger_id": ledger_id}
    protection_state = str(row.get("protection_state") or "")
    if action == "abandon" and protection_state == BYBIT_DEMO_TP_ABANDONED_STATE:
        return {"status": BYBIT_DEMO_TP_ABANDONED_STATE, "ledger_id": ledger_id}
    if action == "accept_partial" and protection_state == BYBIT_DEMO_TP_PARTIAL_STATE:
        return {"status": BYBIT_DEMO_TP_PARTIAL_STATE, "ledger_id": ledger_id}
    if protection_state != BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE:
        return {
            "status": "invalid_state",
            "ledger_id": ledger_id,
            "protection_state": protection_state,
        }

    legs = _get_tp_leg_rows(conn, db_lock, ledger_id)
    if action == "abandon":
        abandon_reason = (reason or "operator_abandoned_recovery").strip()[:500]
        _update_row(
            conn,
            db_lock,
            ledger_id,
            protection_state=BYBIT_DEMO_TP_ABANDONED_STATE,
            last_error=abandon_reason,
        )
        _manual_recovery_event(
            conn,
            db_lock,
            ledger_id,
            "abandon",
            status=BYBIT_DEMO_TP_ABANDONED_STATE,
            reason=abandon_reason,
            now=now,
        )
        return {
            "status": BYBIT_DEMO_TP_ABANDONED_STATE,
            "ledger_id": ledger_id,
        }

    rejected = [
        leg for leg in legs
        if str(leg.get("status") or "").lower() == "rejected"
    ]
    if action == "accept_partial":
        pending = [
            leg for leg in legs
            if str(leg.get("status") or "").lower() != "rejected"
            and not leg.get("order_id")
        ]
        existing = [leg for leg in legs if leg.get("order_id")]
        if not rejected:
            return {
                "status": "partial_not_available",
                "ledger_id": ledger_id,
                "reason": "no_rejected_tp_leg",
            }
        if pending or not existing:
            return {
                "status": "partial_not_available",
                "ledger_id": ledger_id,
                "reason": "unresolved_or_empty_tp_legs",
            }
        _update_row(
            conn,
            db_lock,
            ledger_id,
            protection_state=BYBIT_DEMO_TP_PARTIAL_STATE,
            last_error=None,
        )
        _manual_recovery_event(
            conn,
            db_lock,
            ledger_id,
            "accept_partial",
            status=BYBIT_DEMO_TP_PARTIAL_STATE,
            reason="operator_accepted_partial_tp_protection",
            now=now,
        )
        return {
            "status": BYBIT_DEMO_TP_PARTIAL_STATE,
            "ledger_id": ledger_id,
            "existing_legs": len(existing),
            "rejected_legs": len(rejected),
        }

    parent = _get_row(conn, db_lock, ledger_id) or row
    results = []
    for leg in legs:
        if str(leg.get("status") or "").lower() == "rejected":
            results.append("rejected_skipped")
            continue
        results.append(
            _place_tp_leg(
                conn,
                db_lock,
                client,
                parent=parent,
                leg=leg,
                now=now,
            )
        )
    refreshed_legs = _get_tp_leg_rows(conn, db_lock, ledger_id)
    refreshed_rejected = [
        leg for leg in refreshed_legs
        if str(leg.get("status") or "").lower() == "rejected"
    ]
    unresolved = [
        leg for leg in refreshed_legs
        if str(leg.get("status") or "").lower() != "rejected"
        and not leg.get("order_id")
    ]
    if refreshed_rejected or unresolved:
        _update_row(
            conn,
            db_lock,
            ledger_id,
            protection_state=BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE,
            last_error="manual_recovery_incomplete",
        )
        _manual_recovery_event(
            conn,
            db_lock,
            ledger_id,
            "retry_all",
            status=BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE,
            reason="manual_recovery_incomplete",
            now=now,
        )
        return {
            "status": BYBIT_DEMO_TP_MANUAL_RECOVERY_STATE,
            "ledger_id": ledger_id,
            "results": results,
        }
    _update_row(
        conn,
        db_lock,
        ledger_id,
        protection_state="armed",
        last_error=None,
    )
    _manual_recovery_event(
        conn,
        db_lock,
        ledger_id,
        "retry_all",
        status="armed",
        reason="manual_recovery_completed",
        now=now,
    )
    return {"status": "armed", "ledger_id": ledger_id, "results": results}


def ensure_pending_tp_orders(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    max_rows: int = BYBIT_DEMO_MAX_POLL_ROWS,
) -> dict[str, int | str]:
    """Place pending multi-TP legs without changing read-only reconciliation."""
    if not client.enabled or not bybit_demo_multi_tp_enabled():
        return {"status": "disabled", "processed": 0, "armed": 0}
    with db_lock:
        rows = conn.execute(
            """
            SELECT id FROM bybit_demo_positions
            WHERE protection_state IN (
                'awaiting_entry_fill', 'placing', 'recovery_required'
            )
              AND status NOT IN ('closed', 'rejected')
            ORDER BY id
            LIMIT ?
            """,
            (int(max_rows),),
        ).fetchall()
    processed = 0
    armed = 0
    for (ledger_id,) in rows:
        processed += 1
        try:
            result = _ensure_tp_orders_for_ledger(
                conn,
                db_lock,
                client,
                ledger_id=int(ledger_id),
            )
            if result["status"] == "armed":
                armed += 1
        except Exception as exc:
            _tp_plan_error(
                conn,
                db_lock,
                int(ledger_id),
                f"tp_setup_unexpected_error:{exc}",
            )
    return {"status": "ok", "processed": processed, "armed": armed}


def _tp_event(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    event_type: str,
    idempotency_key: str,
    *,
    leg_id: int | None = None,
    order_id: str | None = None,
    order_link_id: str | None = None,
    reversal_id: int | None = None,
    requested_qty: float | None = None,
    executed_qty: float | None = None,
    position_size_before: float | None = None,
    position_size_after: float | None = None,
    price: float | None = None,
    realized_pnl_usd: float | None = None,
    fee_usd: float | None = None,
    status: str | None = None,
    reason: str | None = None,
    event_ts: float | None = None,
    raw_order: Any = None,
    raw_position: Any = None,
    raw_execution: Any = None,
) -> int | None:
    """Append one TP lifecycle event, deduplicated within its parent ledger."""
    with db_lock:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO bybit_demo_tp_events (
                ledger_id, leg_id, event_type, idempotency_key,
                order_id, order_link_id, reversal_id,
                requested_qty, executed_qty,
                position_size_before, position_size_after,
                price, realized_pnl_usd, fee_usd,
                status, reason, event_ts,
                raw_order_json, raw_position_json, raw_execution_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger_id,
                leg_id,
                event_type,
                idempotency_key,
                order_id,
                order_link_id,
                reversal_id,
                requested_qty,
                executed_qty,
                position_size_before,
                position_size_after,
                price,
                realized_pnl_usd,
                fee_usd,
                status,
                reason,
                float(time.time() if event_ts is None else event_ts),
                _json(raw_order),
                _json(raw_position),
                _json(raw_execution),
            ),
        )
        conn.commit()
    return int(cursor.lastrowid) if cursor.rowcount else None


def _tp_leg_status(
    order_status: str | None,
    executed_qty: float,
) -> str:
    normalized = str(order_status or "").lower().replace("_", "")
    if "cancel" in normalized or normalized in {"rejected", "deactivated"}:
        return "cancelled"
    if normalized == "filled":
        return "filled"
    if executed_qty > 0:
        return "partially_filled"
    if normalized in {"new", "created", "active", "untriggered"}:
        return "open"
    return "submitted"


def _tp_quantity_key(value: float) -> str:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        decimal_value = Decimal("0")
    if not decimal_value.is_finite():
        decimal_value = Decimal("0")
    return _decimal_text(decimal_value) if decimal_value > 0 else "0"


def _tp_execution_fee(executions: list[dict[str, Any]]) -> float | None:
    total = 0.0
    found = False
    for execution in executions:
        raw_fee = execution.get("execFee")
        if raw_fee is None:
            raw_fee = execution.get("fee")
        try:
            fee = float(raw_fee or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fee):
            total += fee
            found = True
    return total if found else None


def _breakeven_stop_is_protective(
    direction: str,
    observed_stop: Any,
    target_stop: Any,
) -> bool:
    try:
        observed = _decimal(observed_stop, "observed_stop")
        target = _decimal(target_stop, "target_stop")
    except (InvalidOperation, TypeError, ValueError):
        return False
    if observed <= 0 or target <= 0:
        return False
    epsilon = max(abs(target) * Decimal("1e-12"), Decimal("1e-12"))
    if direction == "LONG":
        return observed + epsilon >= target
    if direction == "SHORT":
        return observed - epsilon <= target
    return False


def _breakeven_position_stop(position: dict[str, Any]) -> float | None:
    raw_stop = position.get("stopLoss")
    try:
        stop = float(raw_stop or 0)
    except (TypeError, ValueError):
        return None
    return stop if math.isfinite(stop) and stop > 0 else None


def _breakeven_target_price(
    row: dict[str, Any],
    position: dict[str, Any],
) -> float | None:
    try:
        entry = _decimal(position.get("avgPrice"), "avgPrice")
        tick_size = _decimal(row.get("tick_size"), "tick_size")
        if entry <= 0 or tick_size <= 0:
            return None
        target = _normalize_price_decimal(
            entry,
            tick_size,
            direction=str(row.get("direction") or ""),
            is_tp=False,
        )
        return float(target)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _breakeven_reversal_blocked(
    conn: Any,
    db_lock: threading.Lock,
    *,
    ledger_id: int,
    symbol: str,
) -> bool:
    reversal = _reversal_row_for_symbol(conn, db_lock, symbol)
    if not reversal:
        return False
    state = str(reversal.get("state") or "")
    if state in {"CLAIMED", "CLOSING", "OPEN_PENDING", "RECOVERY_REQUIRED"}:
        return True
    if state == "ACTIVE_AFTER_REVERSAL":
        return int(reversal.get("current_ledger_id") or 0) != int(ledger_id)
    return False


def _claim_breakeven_pending(
    conn: Any,
    db_lock: threading.Lock,
    *,
    ledger_id: int,
    target_price: float,
    now: int,
) -> bool:
    """Atomically claim one initial BE mutation for a ledger."""
    with db_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE bybit_demo_positions
                SET be_state=?,
                    be_price=?,
                    be_pending_since_ts=?,
                    be_readback_attempts=0,
                    last_error=NULL
                WHERE id=? AND be_state=?
                """,
                (
                    BYBIT_DEMO_BE_PENDING_STATE,
                    target_price,
                    now,
                    ledger_id,
                    BYBIT_DEMO_BE_NOT_ARMED_STATE,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def _breakeven_event(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    event_type: str,
    *,
    status: str,
    reason: str,
    now: int,
    price: float | None = None,
    raw_position: Any = None,
    attempt: int | None = None,
) -> None:
    suffix = "" if attempt is None else f":{int(attempt)}"
    _tp_event(
        conn,
        db_lock,
        ledger_id,
        f"be_{event_type}",
        f"be:{event_type}:{ledger_id}:{price}:{now}{suffix}",
        price=price,
        status=status,
        reason=reason,
        event_ts=now,
        raw_position=raw_position,
    )


def _breakeven_arm(
    conn: Any,
    db_lock: threading.Lock,
    *,
    ledger_id: int,
    price: float,
    now: int,
    reason: str,
    raw_position: Any = None,
) -> dict[str, Any]:
    _update_row(
        conn,
        db_lock,
        ledger_id,
        be_state=BYBIT_DEMO_BE_ARMED_STATE,
        be_price=price,
        be_set_ts=now,
        be_pending_since_ts=None,
        be_readback_attempts=0,
        last_error=None,
    )
    _breakeven_event(
        conn,
        db_lock,
        ledger_id,
        "armed",
        status=BYBIT_DEMO_BE_ARMED_STATE,
        reason=reason,
        now=now,
        price=price,
        raw_position=raw_position,
    )
    return {
        "status": BYBIT_DEMO_BE_ARMED_STATE,
        "ledger_id": ledger_id,
        "be_price": price,
    }


def _breakeven_recovery(
    conn: Any,
    db_lock: threading.Lock,
    *,
    ledger_id: int,
    reason: str,
    now: int,
    price: float | None = None,
    raw_position: Any = None,
) -> dict[str, Any]:
    _update_row(
        conn,
        db_lock,
        ledger_id,
        be_state=BYBIT_DEMO_BE_RECOVERY_STATE,
        last_error=reason,
    )
    _breakeven_event(
        conn,
        db_lock,
        ledger_id,
        "recovery_required",
        status=BYBIT_DEMO_BE_RECOVERY_STATE,
        reason=reason,
        now=now,
        price=price,
        raw_position=raw_position,
    )
    return {"status": BYBIT_DEMO_BE_RECOVERY_STATE, "ledger_id": ledger_id}


def _breakeven_readback(
    conn: Any,
    db_lock: threading.Lock,
    *,
    row: dict[str, Any],
    position: dict[str, Any] | None,
    target_price: float,
    now: int,
    reason: str,
) -> dict[str, Any]:
    ledger_id = int(row["id"])
    direction = str(row["direction"])
    observed_stop = (
        _breakeven_position_stop(position) if position is not None else None
    )
    if _breakeven_stop_is_protective(direction, observed_stop, target_price):
        return _breakeven_arm(
            conn,
            db_lock,
            ledger_id=ledger_id,
            price=target_price,
            now=now,
            reason=reason,
            raw_position=position,
        )

    attempts = int(row.get("be_readback_attempts") or 0) + 1
    pending_since = row.get("be_pending_since_ts")
    if pending_since is None:
        pending_since = now
    expired = now - int(pending_since) >= BYBIT_DEMO_BE_PENDING_TIMEOUT_SEC
    exhausted = attempts >= BYBIT_DEMO_BE_MAX_READBACKS
    if expired or exhausted:
        return _breakeven_recovery(
            conn,
            db_lock,
            ledger_id=ledger_id,
            reason=(
                f"be_readback_timeout:{reason}"
                if expired
                else f"be_readback_exhausted:{reason}"
            ),
            now=now,
            price=target_price,
            raw_position=position,
        )

    _update_row(
        conn,
        db_lock,
        ledger_id,
        be_state=BYBIT_DEMO_BE_PENDING_STATE,
        be_pending_since_ts=int(pending_since),
        be_readback_attempts=attempts,
        last_error=None,
    )
    _breakeven_event(
        conn,
        db_lock,
        ledger_id,
        "readback_pending",
        status=BYBIT_DEMO_BE_PENDING_STATE,
        reason=reason,
        now=now,
        price=target_price,
        raw_position=position,
        attempt=attempts,
    )
    return {
        "status": BYBIT_DEMO_BE_PENDING_STATE,
        "ledger_id": ledger_id,
        "readback_attempts": attempts,
    }


def _breakeven_error_kind(exc: Exception) -> str:
    if not isinstance(exc, BybitDemoError):
        return "ambiguous"
    try:
        ret_code = int(exc.ret_code) if exc.ret_code is not None else None
    except (TypeError, ValueError):
        ret_code = None
    if ret_code == 10014:
        return "duplicate"
    if ret_code in _BE_DEFINITE_REJECT_CODES:
        return "rejected"
    if ret_code in _BE_RETRYABLE_CODES or exc.retryable or exc.transport:
        return "ambiguous"
    return "ambiguous"


def ensure_breakeven_sl(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
    position: dict[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Arm BE after confirmed TP1, keeping reconcile_tp_legs read-only."""
    now = int(time.time() if now is None else now)
    if (
        not client.enabled
        or not bybit_demo_multi_tp_enabled()
        or not bybit_demo_breakeven_enabled()
    ):
        return {"status": "disabled", "ledger_id": ledger_id}

    row = _get_row(conn, db_lock, ledger_id)
    if not row:
        return {"status": "not_found", "ledger_id": ledger_id}
    be_state = str(row.get("be_state") or BYBIT_DEMO_BE_NOT_ARMED_STATE)
    if be_state == BYBIT_DEMO_BE_ARMED_STATE:
        return {"status": BYBIT_DEMO_BE_ARMED_STATE, "ledger_id": ledger_id}
    if be_state == BYBIT_DEMO_BE_RECOVERY_STATE:
        return {"status": BYBIT_DEMO_BE_RECOVERY_STATE, "ledger_id": ledger_id}
    if be_state not in {
        BYBIT_DEMO_BE_NOT_ARMED_STATE,
        BYBIT_DEMO_BE_PENDING_STATE,
    }:
        return {"status": "invalid_state", "ledger_id": ledger_id}

    if row.get("protection_state") not in {"armed", BYBIT_DEMO_TP_PARTIAL_STATE}:
        return {"status": "not_eligible", "ledger_id": ledger_id}
    legs = _get_tp_leg_rows(conn, db_lock, ledger_id)
    tp1 = next((leg for leg in legs if int(leg["leg_index"]) == 1), None)
    if not tp1 or float(tp1.get("executed_qty") or 0) <= 0:
        return {"status": "not_eligible", "ledger_id": ledger_id}

    if position is None:
        try:
            positions = client.get_position(str(row["symbol"]))
            live = _live_position(positions, str(row["symbol"]))
        except (BybitDemoError, ValueError) as exc:
            if be_state == BYBIT_DEMO_BE_PENDING_STATE:
                return _breakeven_recovery(
                    conn,
                    db_lock,
                    ledger_id=ledger_id,
                    reason=f"be_position_read_failed:{type(exc).__name__}",
                    now=now,
                )
            return {"status": "position_unavailable", "ledger_id": ledger_id}
        position = live["raw"] if live else None
    if position is None:
        if be_state == BYBIT_DEMO_BE_PENDING_STATE:
            return _breakeven_recovery(
                conn,
                db_lock,
                ledger_id=ledger_id,
                reason="be_position_missing",
                now=now,
            )
        return {"status": "position_unavailable", "ledger_id": ledger_id}

    live = _live_position([position], str(row["symbol"]))
    if not live or live["direction"] != str(row["direction"]):
        if be_state == BYBIT_DEMO_BE_PENDING_STATE:
            return _breakeven_recovery(
                conn,
                db_lock,
                ledger_id=ledger_id,
                reason="be_live_direction_mismatch",
                now=now,
                raw_position=position,
            )
        return {"status": "direction_mismatch", "ledger_id": ledger_id}
    target_price = _breakeven_target_price(row, position)
    if target_price is None:
        if be_state == BYBIT_DEMO_BE_PENDING_STATE:
            return _breakeven_recovery(
                conn,
                db_lock,
                ledger_id=ledger_id,
                reason="be_entry_price_unavailable",
                now=now,
            )
        return {"status": "entry_price_unavailable", "ledger_id": ledger_id}

    observed_stop = _breakeven_position_stop(position)
    if _breakeven_stop_is_protective(
        str(row["direction"]), observed_stop, target_price
    ):
        return _breakeven_arm(
            conn,
            db_lock,
            ledger_id=ledger_id,
            price=target_price,
            now=now,
            reason="already_protected",
            raw_position=position,
        )

    if be_state == BYBIT_DEMO_BE_PENDING_STATE:
        return _breakeven_readback(
            conn,
            db_lock,
            row=row,
            position=position,
            target_price=target_price,
            now=now,
            reason="pending_readback",
        )

    if _breakeven_reversal_blocked(
        conn,
        db_lock,
        ledger_id=ledger_id,
        symbol=str(row["symbol"]),
    ):
        return {"status": "reversal_in_progress", "ledger_id": ledger_id}
    if not _claim_breakeven_pending(
        conn,
        db_lock,
        ledger_id=ledger_id,
        target_price=target_price,
        now=now,
    ):
        return ensure_breakeven_sl(
            conn,
            db_lock,
            client,
            ledger_id=ledger_id,
            position=position,
            now=now,
        )

    # Detect-and-back-off: if reversal wins the race, BE fails closed and the
    # reversal path is not required to acquire a reciprocal BE lock.
    if _breakeven_reversal_blocked(
        conn,
        db_lock,
        ledger_id=ledger_id,
        symbol=str(row["symbol"]),
    ):
        return _breakeven_recovery(
            conn,
            db_lock,
            ledger_id=ledger_id,
            reason="reversal_started_before_be_mutation",
            now=now,
            price=target_price,
            raw_position=position,
        )

    try:
        client.set_trading_stop(
            symbol=str(row["symbol"]),
            stop_loss=target_price,
        )
    except Exception as exc:
        kind = _breakeven_error_kind(exc)
        if kind == "rejected":
            return _breakeven_recovery(
                conn,
                db_lock,
                ledger_id=ledger_id,
                reason=f"be_set_rejected:{getattr(exc, 'ret_code', type(exc).__name__)}",
                now=now,
                price=target_price,
                raw_position=position,
            )
        if kind == "duplicate":
            try:
                duplicate_readback = client.get_position(str(row["symbol"]))
                duplicate_live = _live_position(
                    duplicate_readback,
                    str(row["symbol"]),
                )
                duplicate_position = (
                    duplicate_live["raw"] if duplicate_live else None
                )
            except (BybitDemoError, ValueError) as readback_exc:
                return _breakeven_readback(
                    conn,
                    db_lock,
                    row=_get_row(conn, db_lock, ledger_id) or row,
                    position=None,
                    target_price=target_price,
                    now=now,
                    reason=f"duplicate_readback_failed:{type(readback_exc).__name__}",
                )
            return _breakeven_readback(
                conn,
                db_lock,
                row=_get_row(conn, db_lock, ledger_id) or row,
                position=duplicate_position,
                target_price=target_price,
                now=now,
                reason="duplicate_readback",
            )
        _breakeven_event(
            conn,
            db_lock,
            ledger_id,
            "set_pending",
            status=BYBIT_DEMO_BE_PENDING_STATE,
            reason=f"be_set_{kind}:{type(exc).__name__}",
            now=now,
            price=target_price,
            raw_position=position,
        )
        return {
            "status": BYBIT_DEMO_BE_PENDING_STATE,
            "ledger_id": ledger_id,
        }

    try:
        readback_items = client.get_position(str(row["symbol"]))
        readback_live = _live_position(readback_items, str(row["symbol"]))
        readback_position = readback_live["raw"] if readback_live else None
    except (BybitDemoError, ValueError) as exc:
        return _breakeven_readback(
            conn,
            db_lock,
            row=_get_row(conn, db_lock, ledger_id) or row,
            position=None,
            target_price=target_price,
            now=now,
            reason=f"post_set_readback_failed:{type(exc).__name__}",
        )
    return _breakeven_readback(
        conn,
        db_lock,
        row=_get_row(conn, db_lock, ledger_id) or row,
        position=readback_position,
        target_price=target_price,
        now=now,
        reason="post_set_readback",
    )


def reconcile_tp_legs(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
    max_legs: int | None = None,
    now: int | None = None,
    reversal_id: int | None = None,
) -> dict[str, int | str]:
    """Reconcile already-submitted TP legs without creating any orders."""
    if not client.enabled:
        return {
            "status": "disabled",
            "polled": 0,
            "updated": 0,
            "events_created": 0,
            "errors": 0,
            "successful_requests": 0,
        }

    poll_ts = int(time.time() if now is None else now)
    with db_lock:
        parent_cursor = conn.execute(
            "SELECT symbol FROM bybit_demo_positions WHERE id=?",
            (ledger_id,),
        )
        parent = parent_cursor.fetchone()
        if not parent:
            return {
                "status": "unknown",
                "polled": 0,
                "updated": 0,
                "events_created": 0,
                "errors": 1,
                "successful_requests": 0,
            }
        symbol = str(parent[0]).upper()
        cursor = conn.execute(
            """
            SELECT *
            FROM bybit_demo_tp_legs
            WHERE ledger_id=? AND status NOT IN ('filled', 'cancelled')
            ORDER BY leg_index
            LIMIT ?
            """,
            (ledger_id, int(max_legs or BYBIT_DEMO_MAX_POLL_ROWS)),
        )
        legs = [_row_dict(cursor, row) for row in cursor.fetchall()]

    polled = 0
    updated = 0
    events_created = 0
    errors = 0
    successful_requests = 0

    for leg in legs:
        order_id = str(leg.get("order_id") or "") or None
        order_link_id = str(leg.get("order_link_id") or "") or None
        if not order_id and not order_link_id:
            continue
        try:
            order_lookup = {"symbol": symbol}
            if order_id:
                order_lookup["order_id"] = order_id
            else:
                order_lookup["order_link_id"] = order_link_id
            orders = client.get_order_realtime(**order_lookup)
            successful_requests += 1
            polled += 1
            if not orders:
                _update_tp_leg(
                    conn,
                    db_lock,
                    int(leg["id"]),
                    updated_ts=poll_ts,
                    last_error="tp_order_not_found",
                )
                errors += 1
                continue

            order = orders[0]
            observed_order_id = str(order.get("orderId") or "") or order_id
            observed_link_id = (
                str(order.get("orderLinkId") or "") or order_link_id
            )
            observed_qty = _order_executed_qty(order)
            previous_qty = float(leg.get("executed_qty") or 0)
            executed_qty = max(previous_qty, observed_qty)
            status = _tp_leg_status(order.get("orderStatus"), executed_qty)
            try:
                avg_exit_price = float(order.get("avgPrice") or 0) or None
            except (TypeError, ValueError):
                avg_exit_price = None

            executions: list[dict[str, Any]] = []
            if observed_order_id:
                executions = client.get_executions(
                    symbol,
                    observed_order_id,
                )
                successful_requests += 1
            fee_usd = _tp_execution_fee(executions)
            fields: dict[str, Any] = {
                "executed_qty": executed_qty,
                "status": status,
                "order_id": observed_order_id,
                "order_link_id": observed_link_id,
                "avg_exit_price": avg_exit_price,
                "updated_ts": poll_ts,
                "last_error": None,
                "raw_order_json": _json(order),
            }
            if fee_usd is not None:
                fields["fee_usd"] = fee_usd
            if executions:
                fields["raw_execution_json"] = _json({"executions": executions})
            if status == "filled" and leg.get("filled_ts") is None:
                fields["filled_ts"] = poll_ts
            if status == "cancelled" and leg.get("cancelled_ts") is None:
                fields["cancelled_ts"] = poll_ts
            _update_tp_leg(conn, db_lock, int(leg["id"]), **fields)
            updated += 1

            event_type = (
                "tp_leg_filled"
                if status == "filled"
                else "tp_leg_cancelled"
                if status == "cancelled"
                else "tp_leg_partial_fill"
                if executed_qty > 0
                else "tp_leg_reconciled"
            )
            idempotency_key = (
                f"leg:{int(leg['id'])}:{event_type}:"
                f"{_tp_quantity_key(executed_qty)}:{status}"
            )
            event_id = _tp_event(
                conn,
                db_lock,
                ledger_id,
                event_type,
                idempotency_key,
                leg_id=int(leg["id"]),
                order_id=observed_order_id,
                order_link_id=observed_link_id,
                reversal_id=reversal_id,
                requested_qty=float(leg["planned_qty"]),
                executed_qty=executed_qty,
                price=avg_exit_price,
                fee_usd=fee_usd,
                status=status,
                reason="poll_reconciliation",
                event_ts=poll_ts,
                raw_order=order,
                raw_execution={"executions": executions} if executions else None,
            )
            if event_id is not None:
                events_created += 1
        except BybitDemoError as exc:
            _update_tp_leg(
                conn,
                db_lock,
                int(leg["id"]),
                updated_ts=poll_ts,
                last_error=str(exc),
            )
            errors += 1
        except Exception:
            _update_tp_leg(
                conn,
                db_lock,
                int(leg["id"]),
                updated_ts=poll_ts,
                last_error="unexpected_tp_reconciliation_error",
            )
            errors += 1

    return {
        "status": "ok",
        "polled": polled,
        "updated": updated,
        "events_created": events_created,
        "errors": errors,
        "successful_requests": successful_requests,
    }


def _inverse_direction(direction: str) -> str:
    if direction == "LONG":
        return "SHORT"
    if direction == "SHORT":
        return "LONG"
    raise ValueError("invalid_direction")


def _live_position(
    positions: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    """Normalize the single non-zero one-way position for a symbol."""
    matches = []
    for position in positions:
        if str(position.get("symbol", "")).upper() != symbol.upper():
            continue
        try:
            size = float(position.get("size") or 0)
        except (TypeError, ValueError):
            raise ValueError("live_position_size_invalid")
        if not math.isfinite(size) or size < 0:
            raise ValueError("live_position_size_invalid")
        if size <= 0:
            continue
        side = str(position.get("side") or "")
        if side not in {"Buy", "Sell"}:
            raise ValueError("live_position_side_invalid")
        matches.append(
            {
                "symbol": symbol.upper(),
                "direction": "LONG" if side == "Buy" else "SHORT",
                "size": size,
                "raw": position,
            }
        )
    if len(matches) > 1:
        raise ValueError("multiple_live_positions_for_symbol")
    return matches[0] if matches else None


def _reversal_event(
    conn: Any,
    db_lock: threading.Lock,
    reversal_id: int,
    event_type: str,
    *,
    attempt_no: int | None = None,
    order_link_id: str | None = None,
    order_id: str | None = None,
    requested_qty: float | None = None,
    executed_qty: float | None = None,
    position_size_before: float | None = None,
    position_size_after: float | None = None,
    status: str | None = None,
    reason: str | None = None,
    event_ts: float | None = None,
    raw_order: Any = None,
    raw_position: Any = None,
    raw_execution: Any = None,
) -> None:
    with db_lock:
        conn.execute(
            """
            INSERT INTO bybit_demo_reversal_events (
                reversal_id, event_type, attempt_no, order_link_id, order_id,
                requested_qty, executed_qty, position_size_before,
                position_size_after, status, reason, event_ts,
                raw_order_json, raw_position_json, raw_execution_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reversal_id,
                event_type,
                attempt_no,
                order_link_id,
                order_id,
                requested_qty,
                executed_qty,
                position_size_before,
                position_size_after,
                status,
                reason,
                float(time.time() if event_ts is None else event_ts),
                _json(raw_order),
                _json(raw_position),
                _json(raw_execution),
            ),
        )
        conn.commit()


def _update_reversal(
    conn: Any, db_lock: threading.Lock, reversal_id: int, **fields: Any
) -> None:
    allowed = {
        "current_ledger_id",
        "state",
        "reversal_claimed",
        "reversal_used",
        "close_attempts",
        "tp_legs_cancelled",
        "tp_cancel_errors",
        "close_order_id",
        "close_order_link_id",
        "close_order_status",
        "close_requested_qty",
        "close_executed_qty",
        "position_size_before",
        "position_size_after",
        "used_ts",
        "recovery_reason",
        "last_error",
        "raw_position_json",
        "raw_order_json",
        "raw_execution_json",
        "updated_ts",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"invalid reversal fields: {sorted(unknown)}")
    if not fields:
        return
    fields.setdefault("updated_ts", int(time.time()))
    assignments = ", ".join(f"{key}=?" for key in fields)
    with db_lock:
        conn.execute(
            f"UPDATE bybit_demo_reversals SET {assignments} WHERE id=?",
            [*fields.values(), reversal_id],
        )
        conn.commit()


def _reversal_row_for_symbol(
    conn: Any, db_lock: threading.Lock, symbol: str
) -> dict[str, Any] | None:
    with db_lock:
        cursor = conn.execute(
            """
            SELECT * FROM bybit_demo_reversals
            WHERE symbol=? AND state IN (
                'CLAIMED', 'CLOSING', 'OPEN_PENDING',
                'ACTIVE_AFTER_REVERSAL', 'RECOVERY_REQUIRED'
            )
            ORDER BY id DESC LIMIT 1
            """,
            (symbol.upper(),),
        )
        row = cursor.fetchone()
        return _row_dict(cursor, row) if row else None


def _active_source_ledger_ids(
    conn: Any,
    db_lock: threading.Lock,
    symbol: str,
    direction: str,
) -> list[int]:
    with db_lock:
        rows = conn.execute(
            """
            SELECT id FROM bybit_demo_positions
            WHERE symbol=?
              AND direction=?
              AND status IN (
                  'intent', 'submitting', 'submitted',
                  'partially_filled', 'open', 'unknown'
              )
            ORDER BY id
            """,
            (symbol.upper(), direction),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _has_active_symbol_entries(
    conn: Any, db_lock: threading.Lock, symbol: str, *, exclude_ledger_id: int | None = None
) -> bool:
    with db_lock:
        row = conn.execute(
            """
            SELECT 1 FROM bybit_demo_positions
            WHERE symbol=?
              AND (? IS NULL OR id != ?)
              AND status IN (
                  'intent', 'submitting', 'submitted',
                  'partially_filled', 'open', 'unknown'
              )
            LIMIT 1
            """,
            (symbol.upper(), exclude_ledger_id, exclude_ledger_id),
        ).fetchone()
    return row is not None


def _claim_reversal(
    conn: Any,
    db_lock: threading.Lock,
    *,
    symbol: str,
    source_signal_key: str,
    source_direction: str,
    target_direction: str,
    source_ledger_ids: list[int],
    now: int,
) -> int | None:
    """Claim a symbol once using SQLite's writer lock and a unique index."""
    with db_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO bybit_demo_reversals (
                    symbol, source_signal_key, source_direction, target_direction,
                    source_ledger_ids_json, state, reversal_claimed,
                    reversal_used, close_deadline_ts, claimed_ts,
                    created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, 'CLOSING', 1, 0, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    source_signal_key,
                    source_direction,
                    target_direction,
                    _json(source_ledger_ids),
                    float(now) + BYBIT_DEMO_REVERSAL_DEADLINE_SEC,
                    now,
                    now,
                    now,
                ),
            )
            reversal_id = int(cursor.lastrowid)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        except Exception:
            conn.rollback()
            raise
    _reversal_event(
        conn,
        db_lock,
        reversal_id,
        "claim",
        status="claimed",
        reason="atomic_symbol_claim",
        event_ts=now,
    )
    logger.info(
        "bybit_demo_reversal_claimed reversal_id=%d symbol=%s "
        "from_direction=%s to_direction=%s",
        reversal_id,
        symbol.upper(),
        source_direction,
        target_direction,
    )
    return reversal_id


def _reversal_close_link_id(reversal_id: int, attempt_no: int) -> str:
    material = f"reversal-close|{reversal_id}|{attempt_no}"
    return "brc" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:29]


def recover_expired_reversals(
    conn: Any,
    db_lock: threading.Lock,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Move expired close/open-pending lifecycles into recovery atomically.

    This is deliberately DB-only: a watchdog must not submit an exchange order
    after a restart.  The conditional UPDATE prevents a stale scan from
    overwriting a lifecycle that another worker finalized concurrently.
    """
    timestamp = float(time.time() if now is None else now)
    with db_lock:
        rows = conn.execute(
            """
            SELECT id, symbol, state, close_attempts
            FROM bybit_demo_reversals
            WHERE state IN ('CLOSING', 'OPEN_PENDING')
              AND close_deadline_ts <= ?
            ORDER BY id
            """,
            (timestamp,),
        ).fetchall()

    recovered_ids: list[int] = []
    for reversal_id, symbol, state, close_attempts in rows:
        previous_state = str(state)
        reason = f"watchdog_expired_{previous_state.lower()}"
        with db_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE bybit_demo_reversals
                    SET state='RECOVERY_REQUIRED',
                        recovery_reason=?,
                        last_error=?,
                        updated_ts=?
                    WHERE id=?
                      AND state=?
                      AND close_deadline_ts <= ?
                    """,
                    (
                        reason,
                        reason,
                        int(timestamp),
                        int(reversal_id),
                        previous_state,
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    continue
                conn.execute(
                    """
                    INSERT INTO bybit_demo_reversal_events (
                        reversal_id, event_type, attempt_no, status,
                        reason, event_ts
                    ) VALUES (?, 'watchdog_recovery_required', ?, ?, ?, ?)
                    """,
                    (
                        int(reversal_id),
                        close_attempts,
                        "recovery_required",
                        reason,
                        timestamp,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        recovered_ids.append(int(reversal_id))
        logger.error(
            "bybit_demo_reversal_watchdog_recovery reversal_id=%d "
            "symbol=%s previous_state=%s reason=%s",
            int(reversal_id),
            str(symbol).upper(),
            previous_state,
            reason,
        )

    return {
        "scanned": len(rows),
        "recovered": len(recovered_ids),
        "reversal_ids": recovered_ids,
    }


def _order_executed_qty(order: dict[str, Any] | None) -> float:
    if not order:
        return 0.0
    try:
        value = float(order.get("cumExecQty") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


_EXPECTED_TERMINAL_CANCEL_CODES = {110001, 110008, 110010}


def _cancel_error_is_expected_terminal(exc: BybitDemoError) -> bool:
    """Classify an already-gone order separately from an ambiguous cancel."""
    try:
        ret_code = int(exc.ret_code)
    except (TypeError, ValueError):
        ret_code = None
    if ret_code in _EXPECTED_TERMINAL_CANCEL_CODES:
        return True
    message = f"{exc} {exc.ret_msg or ''}".lower()
    return any(
        phrase in message
        for phrase in (
            "order does not exist",
            "order not exists",
            "order not found",
            "already cancelled",
            "already canceled",
            "order completed",
            "order filled",
            "order cancelled",
            "order canceled",
        )
    )


def _cancel_tp_legs_for_reversal(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    reversal_id: int,
    source_ledger_ids: list[int],
    now: int | None = None,
) -> dict[str, int | str]:
    """Best-effort cancel of every submitted TP leg before reversal close."""
    result: dict[str, int | str] = {
        "status": "ok",
        "legs_seen": 0,
        "cancel_attempts": 0,
        "tp_legs_cancelled": 0,
        "tp_cancel_errors": 0,
        "tp_legs_filled": 0,
        "active_legs": 0,
        "successful_requests": 0,
    }
    if not source_ledger_ids:
        return result

    placeholders = ",".join("?" for _ in source_ledger_ids)
    with db_lock:
        cursor = conn.execute(
            f"""
            SELECT legs.*, positions.symbol
            FROM bybit_demo_tp_legs AS legs
            JOIN bybit_demo_positions AS positions
              ON positions.id=legs.ledger_id
            WHERE legs.ledger_id IN ({placeholders})
              AND legs.status NOT IN ('filled', 'cancelled')
            ORDER BY legs.ledger_id, legs.leg_index
            """,
            tuple(int(ledger_id) for ledger_id in source_ledger_ids),
        )
        legs = [_row_dict(cursor, row) for row in cursor.fetchall()]
    result["legs_seen"] = len(legs)
    candidate_ids: list[int] = []
    candidate_ledger_ids: set[int] = set()

    for leg in legs:
        leg_id = int(leg["id"])
        order_id = str(leg.get("order_id") or "") or None
        order_link_id = str(leg.get("order_link_id") or "") or None
        executed_qty = float(leg.get("executed_qty") or 0)
        if not order_id and not order_link_id:
            # A planned leg without an exchange identity has no submitted
            # order to cancel.  A partially executed leg without one is
            # unsafe and therefore enters recovery.
            if executed_qty > 0 or str(leg.get("status") or "") != "planned":
                result["tp_cancel_errors"] += 1
            continue

        candidate_ids.append(leg_id)
        candidate_ledger_ids.add(int(leg["ledger_id"]))
        _update_tp_leg(
            conn,
            db_lock,
            leg_id,
            reversal_id=reversal_id,
        )
        result["cancel_attempts"] += 1
        try:
            if order_id:
                client.cancel_order(symbol=str(leg["symbol"]).upper(), order_id=order_id)
            else:
                client.cancel_order(
                    symbol=str(leg["symbol"]).upper(),
                    order_link_id=order_link_id,
                )
            result["successful_requests"] += 1
        except BybitDemoError as exc:
            if _cancel_error_is_expected_terminal(exc):
                continue
            # Transport, timeout, and unknown response errors are ambiguous:
            # the exchange may have accepted the cancel despite the error.
            result["tp_cancel_errors"] += 1
        except Exception:
            result["tp_cancel_errors"] += 1

    poll_ts = int(time.time() if now is None else now)
    for ledger_id in sorted(candidate_ledger_ids):
        reconciliation = reconcile_tp_legs(
            conn,
            db_lock,
            client,
            ledger_id=ledger_id,
            now=poll_ts,
            reversal_id=reversal_id,
        )
        result["successful_requests"] += int(reconciliation["successful_requests"])
        result["tp_cancel_errors"] += int(reconciliation["errors"])

    if candidate_ids:
        with db_lock:
            marks = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                f"""
                SELECT id, status
                FROM bybit_demo_tp_legs
                WHERE id IN ({marks})
                """,
                tuple(candidate_ids),
            ).fetchall()
        cancelled_ids = {
            int(leg_id) for leg_id, status in rows if str(status) == "cancelled"
        }
        filled_ids = {
            int(leg_id) for leg_id, status in rows if str(status) == "filled"
        }
        result["tp_legs_cancelled"] = len(cancelled_ids)
        result["tp_legs_filled"] = len(filled_ids)
        result["active_legs"] = len(
            [leg_id for leg_id, status in rows if str(status) not in {"filled", "cancelled"}]
        )
        result["tp_cancel_errors"] += int(result["active_legs"])

    if int(result["tp_cancel_errors"]) > 0:
        result["status"] = "recovery_required"
    return result


def _executions_qty(executions: list[dict[str, Any]]) -> float:
    total = 0.0
    for execution in executions:
        try:
            value = float(execution.get("execQty") or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            total += value
    return total


def _close_order_is_resolved(order: dict[str, Any]) -> bool:
    return str(order.get("orderStatus") or "").lower() in {
        "filled",
        "partiallyfilled",
        "cancelled",
        "rejected",
        "deactivated",
    }


def _mark_source_rows_reversed(
    conn: Any,
    db_lock: threading.Lock,
    source_ledger_ids: list[int],
    *,
    ts_closed: int,
    raw_position: Any,
) -> None:
    for ledger_id in source_ledger_ids:
        _update_row(
            conn,
            db_lock,
            ledger_id,
            status="closed",
            position_size=0.0,
            exit_reason="reversal_used",
            ts_closed=ts_closed,
            raw_position_json=_json(raw_position),
            last_error=None,
        )


def _set_reversal_recovery(
    conn: Any,
    db_lock: threading.Lock,
    reversal_id: int,
    reason: str,
    *,
    attempt_no: int | None = None,
    order_link_id: str | None = None,
    order_id: str | None = None,
    raw_order: Any = None,
    raw_position: Any = None,
) -> None:
    _update_reversal(
        conn,
        db_lock,
        reversal_id,
        state="RECOVERY_REQUIRED",
        recovery_reason=reason,
        last_error=reason,
        raw_order_json=_json(raw_order),
        raw_position_json=_json(raw_position),
    )
    _reversal_event(
        conn,
        db_lock,
        reversal_id,
        "recovery_required",
        attempt_no=attempt_no,
        order_link_id=order_link_id,
        order_id=order_id,
        status="recovery_required",
        reason=reason,
        raw_order=raw_order,
        raw_position=raw_position,
    )
    logger.error(
        "bybit_demo_reversal_recovery_required reversal_id=%d reason=%s",
        reversal_id,
        reason,
    )


def _run_reversal_close(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    reversal_id: int,
    symbol: str,
    source_direction: str,
    source_ledger_ids: list[int],
) -> str:
    """Close the live one-way position with bounded, idempotent attempts."""
    reversal = _reversal_row_for_id(conn, db_lock, reversal_id)
    if not reversal:
        return "recovery_required"
    deadline = float(reversal["close_deadline_ts"])
    total_executed = 0.0
    total_tp_legs_cancelled = int(reversal.get("tp_legs_cancelled") or 0)
    total_tp_cancel_errors = int(reversal.get("tp_cancel_errors") or 0)

    for attempt_no in range(1, BYBIT_DEMO_REVERSAL_MAX_PASSES + 1):
        if time.time() >= deadline:
            _set_reversal_recovery(
                conn, db_lock, reversal_id, "close_deadline_exceeded",
                attempt_no=attempt_no,
            )
            return "recovery_required"
        try:
            before_items = client.get_position(symbol)
            before = _live_position(before_items, symbol)
        except (BybitDemoError, ValueError) as exc:
            reason = (
                f"close_position_read_failed:{exc}"
                if isinstance(exc, BybitDemoError)
                else str(exc)
            )
            _set_reversal_recovery(conn, db_lock, reversal_id, reason, attempt_no=attempt_no)
            return "recovery_required"
        if before is None:
            _update_reversal(
                conn,
                db_lock,
                reversal_id,
                state="CLOSED",
                position_size_before=0.0,
                position_size_after=0.0,
                close_attempts=attempt_no - 1,
                last_error=None,
            )
            _reversal_event(
                conn,
                db_lock,
                reversal_id,
                "close_cancelled_no_position",
                attempt_no=attempt_no,
                status="cancelled",
                reason="position_flat_before_close",
                position_size_before=0.0,
                position_size_after=0.0,
            )
            logger.info(
                "bybit_demo_reversal_cancelled reversal_id=%d symbol=%s "
                "reason=position_flat_before_close",
                reversal_id,
                symbol.upper(),
            )
            return "cancelled"
        if before["direction"] != source_direction:
            _set_reversal_recovery(
                conn,
                db_lock,
                reversal_id,
                "live_position_direction_changed",
                attempt_no=attempt_no,
                raw_position=before["raw"],
            )
            return "recovery_required"

        tp_cancel_result = _cancel_tp_legs_for_reversal(
            conn,
            db_lock,
            client,
            reversal_id=reversal_id,
            source_ledger_ids=source_ledger_ids,
        )
        total_tp_legs_cancelled += int(tp_cancel_result["tp_legs_cancelled"])
        total_tp_cancel_errors += int(tp_cancel_result["tp_cancel_errors"])
        _update_reversal(
            conn,
            db_lock,
            reversal_id,
            tp_legs_cancelled=total_tp_legs_cancelled,
            tp_cancel_errors=total_tp_cancel_errors,
        )
        logger.info(
            "bybit_demo_reversal_tp_cancel_summary reversal_id=%d attempt=%d "
            "legs_seen=%d cancel_attempts=%d tp_legs_cancelled=%d "
            "tp_legs_filled=%d tp_cancel_errors=%d active_legs=%d",
            reversal_id,
            attempt_no,
            int(tp_cancel_result["legs_seen"]),
            int(tp_cancel_result["cancel_attempts"]),
            int(tp_cancel_result["tp_legs_cancelled"]),
            int(tp_cancel_result["tp_legs_filled"]),
            int(tp_cancel_result["tp_cancel_errors"]),
            int(tp_cancel_result["active_legs"]),
        )
        if tp_cancel_result["status"] != "ok":
            _set_reversal_recovery(
                conn,
                db_lock,
                reversal_id,
                "tp_cancel_recovery_required",
                attempt_no=attempt_no,
            )
            return "recovery_required"

        try:
            after_tp_cancel_items = client.get_position(symbol)
            after_tp_cancel = _live_position(after_tp_cancel_items, symbol)
        except Exception as exc:
            _set_reversal_recovery(
                conn,
                db_lock,
                reversal_id,
                f"close_position_read_after_tp_cancel_failed:{exc}",
                attempt_no=attempt_no,
                raw_position=before["raw"],
            )
            return "recovery_required"
        if after_tp_cancel is None:
            _update_reversal(
                conn,
                db_lock,
                reversal_id,
                state="CLOSED",
                position_size_before=before["size"],
                position_size_after=0.0,
                close_attempts=attempt_no - 1,
                last_error=None,
            )
            _reversal_event(
                conn,
                db_lock,
                reversal_id,
                "close_cancelled_no_position",
                attempt_no=attempt_no,
                status="cancelled",
                reason="position_flat_after_tp_cancel",
                position_size_before=before["size"],
                position_size_after=0.0,
                raw_position=before["raw"],
            )
            logger.info(
                "bybit_demo_reversal_cancelled reversal_id=%d symbol=%s "
                "reason=position_flat_after_tp_cancel",
                reversal_id,
                symbol.upper(),
            )
            return "cancelled"
        if after_tp_cancel["direction"] != source_direction:
            _set_reversal_recovery(
                conn,
                db_lock,
                reversal_id,
                "live_position_direction_changed_after_tp_cancel",
                attempt_no=attempt_no,
                raw_position=after_tp_cancel["raw"],
            )
            return "recovery_required"

        requested_qty = after_tp_cancel["size"]
        order_link_id = _reversal_close_link_id(reversal_id, attempt_no)
        order: dict[str, Any] | None = None
        try:
            remote = client.get_order_realtime(
                symbol=symbol, order_link_id=order_link_id
            )
            if remote:
                order = remote[0]
            else:
                if not bybit_demo_trading_enabled():
                    _set_reversal_recovery(
                        conn,
                        db_lock,
                        reversal_id,
                        "trading_disabled",
                        attempt_no=attempt_no,
                    )
                    logger.warning(
                        "bybit_demo_reversal_close_blocked reversal_id=%d "
                        "symbol=%s reason=trading_disabled",
                        reversal_id,
                        symbol.upper(),
                    )
                    return "recovery_required"
                order = client.create_market_order(
                    symbol=symbol,
                    direction=source_direction,
                    qty=requested_qty,
                    order_link_id=order_link_id,
                    reduce_only=True,
                )
        except BybitDemoError as exc:
            if exc.retryable or exc.transport:
                # An absent order after an ambiguous POST is not safe to retry
                # with another link; the accepted order may arrive late.
                _set_reversal_recovery(
                    conn,
                    db_lock,
                    reversal_id,
                    f"ambiguous_close_submission:{exc}",
                    attempt_no=attempt_no,
                    order_link_id=order_link_id,
                )
                return "recovery_required"
            order = {
                "orderStatus": "Rejected",
                "orderLinkId": order_link_id,
            }
            _update_reversal(
                conn, db_lock, reversal_id,
                close_attempts=attempt_no,
                close_order_link_id=order_link_id,
                close_order_status="Rejected",
                close_requested_qty=requested_qty,
                position_size_before=requested_qty,
                last_error=str(exc),
            )
            _reversal_event(
                conn, db_lock, reversal_id, "close_attempt",
                attempt_no=attempt_no,
                order_link_id=order_link_id,
                requested_qty=requested_qty,
                position_size_before=requested_qty,
                status="rejected",
                reason=str(exc),
            )
            continue

        order_id = str(order.get("orderId") or "") or None
        executed_qty = _order_executed_qty(order)
        executions: list[dict[str, Any]] = []
        if order_id:
            try:
                executions = client.get_executions(symbol, order_id)
            except BybitDemoError as exc:
                _set_reversal_recovery(
                    conn,
                    db_lock,
                    reversal_id,
                    f"close_execution_reconciliation_failed:{exc}",
                    attempt_no=attempt_no,
                    order_link_id=order_link_id,
                    order_id=order_id,
                    raw_order=order,
                )
                return "recovery_required"
            executed_qty = max(executed_qty, _executions_qty(executions))
        _update_reversal(
            conn,
            db_lock,
            reversal_id,
            close_attempts=attempt_no,
            close_order_id=order_id,
            close_order_link_id=order_link_id,
            close_order_status=order.get("orderStatus"),
            close_requested_qty=requested_qty,
            close_executed_qty=total_executed,
            position_size_before=requested_qty,
            raw_order_json=_json(order),
            raw_execution_json=_json({"executions": executions}),
        )
        try:
            after_items = client.get_position(symbol)
            after = _live_position(after_items, symbol)
        except (BybitDemoError, ValueError) as exc:
            _set_reversal_recovery(
                conn,
                db_lock,
                reversal_id,
                f"close_reconciliation_failed:{exc}",
                attempt_no=attempt_no,
                order_link_id=order_link_id,
                order_id=order_id,
                raw_order=order,
            )
            return "recovery_required"
        after_size = after["size"] if after else 0.0
        observed_executed_qty = max(executed_qty, requested_qty - after_size)
        total_executed += observed_executed_qty
        _update_reversal(
            conn,
            db_lock,
            reversal_id,
            close_executed_qty=total_executed,
            position_size_after=after_size,
        )
        if observed_executed_qty > 0:
            _update_reversal(
                conn,
                db_lock,
                reversal_id,
                reversal_used=1,
                used_ts=int(time.time()),
            )
        _reversal_event(
            conn,
            db_lock,
            reversal_id,
            "close_reconciled",
            attempt_no=attempt_no,
            order_link_id=order_link_id,
            order_id=order_id,
            requested_qty=requested_qty,
            executed_qty=observed_executed_qty,
            position_size_before=requested_qty,
            position_size_after=after_size,
            status=str(order.get("orderStatus") or "").lower(),
            reason="position_flat" if after is None else "position_remaining",
            raw_order=order,
            raw_position=after["raw"] if after else None,
            raw_execution={"executions": executions},
        )
        logger.info(
            "bybit_demo_reversal_close_reconciled reversal_id=%d attempt=%d "
            "symbol=%s requested_qty=%.12g executed_qty=%.12g remaining_qty=%.12g",
            reversal_id,
            attempt_no,
            symbol.upper(),
            requested_qty,
            executed_qty,
            after_size,
        )
        if after is None:
            now = int(time.time())
            _update_reversal(
                conn,
                db_lock,
                reversal_id,
                state="OPEN_PENDING",
                reversal_used=1,
                used_ts=now,
                position_size_after=0.0,
                raw_position_json=None,
                last_error=None,
            )
            _mark_source_rows_reversed(
                conn,
                db_lock,
                source_ledger_ids,
                ts_closed=now,
                raw_position=None,
            )
            return "closed"
        if not _close_order_is_resolved(order):
            _set_reversal_recovery(
                conn,
                db_lock,
                reversal_id,
                "close_order_still_open",
                attempt_no=attempt_no,
                order_link_id=order_link_id,
                order_id=order_id,
                raw_order=order,
                raw_position=after["raw"],
            )
            return "recovery_required"

    _set_reversal_recovery(
        conn,
        db_lock,
        reversal_id,
        "close_max_passes_exceeded",
        attempt_no=BYBIT_DEMO_REVERSAL_MAX_PASSES,
    )
    return "recovery_required"


def _reversal_row_for_id(
    conn: Any, db_lock: threading.Lock, reversal_id: int
) -> dict[str, Any] | None:
    with db_lock:
        cursor = conn.execute(
            "SELECT * FROM bybit_demo_reversals WHERE id=?",
            (reversal_id,),
        )
        row = cursor.fetchone()
        return _row_dict(cursor, row) if row else None


def _finalize_reversal_lifecycle(
    conn: Any, db_lock: threading.Lock, ledger_id: int
) -> None:
    with db_lock:
        row = conn.execute(
            """
            SELECT id FROM bybit_demo_reversals
            WHERE current_ledger_id=? AND state='ACTIVE_AFTER_REVERSAL'
            """,
            (ledger_id,),
        ).fetchone()
    if row:
        _update_reversal(
            conn,
            db_lock,
            int(row[0]),
            state="CLOSED",
            last_error=None,
        )
        logger.info(
            "bybit_demo_reversal_lifecycle_closed reversal_id=%d ledger_id=%d",
            int(row[0]),
            ledger_id,
        )


def _parse_api_number(
    value: Any,
    field: str,
    *,
    allow_negative: bool = False,
) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field}_missing")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not math.isfinite(parsed) or (not allow_negative and parsed < 0):
        raise ValueError(f"{field}_invalid")
    return parsed


def _wallet_balance_usd(items: list[dict[str, Any]]) -> float:
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("wallet_balance_missing")
    account = items[0]
    raw_balance = account.get("totalWalletBalance")
    if raw_balance is None or (
        isinstance(raw_balance, str) and not raw_balance.strip()
    ):
        coins = account.get("coin")
        if isinstance(coins, list):
            usdt = next(
                (
                    item
                    for item in coins
                    if isinstance(item, dict)
                    and str(item.get("coin") or "").upper() == "USDT"
                ),
                None,
            )
            raw_balance = usdt.get("walletBalance") if usdt else None
    return _parse_api_number(raw_balance, "wallet_balance")


def _position_totals(
    positions: list[dict[str, Any]],
) -> tuple[float, float]:
    open_exposure = 0.0
    unrealized_pnl = 0.0
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise ValueError(f"position_{index}_invalid")
        size = _parse_api_number(position.get("size"), f"position_{index}_size")
        if size == 0:
            continue
        raw_value = position.get("positionValue")
        if raw_value is None or (
            isinstance(raw_value, str) and not raw_value.strip()
        ):
            avg_price = _parse_api_number(
                position.get("avgPrice"),
                f"position_{index}_avg_price",
            )
            position_value = abs(size * avg_price)
        else:
            position_value = _parse_api_number(
                raw_value,
                f"position_{index}_value",
            )
        if position_value <= 0:
            raise ValueError(f"position_{index}_value_invalid")
        raw_pnl = position.get("unrealisedPnl")
        if raw_pnl is None:
            raw_pnl = position.get("unrealizedPnl")
        pnl = _parse_api_number(
            raw_pnl,
            f"position_{index}_unrealized_pnl",
            allow_negative=True,
        )
        open_exposure += position_value
        unrealized_pnl += pnl
    if not math.isfinite(open_exposure) or not math.isfinite(unrealized_pnl):
        raise ValueError("position_totals_invalid")
    return open_exposure, unrealized_pnl


def read_reserve_snapshot(client: BybitDemoClient) -> dict[str, float]:
    """Read and normalize the remote inputs shared by gates and health probes."""
    balance = _wallet_balance_usd(client.get_wallet_balance())
    open_exposure, unrealized_pnl = _position_totals(
        client.get_open_positions()
    )
    return {
        "open_exposure_usd": open_exposure,
        "balance_usd": balance,
        "unrealized_pnl_usd": unrealized_pnl,
        "equity_usd": balance + unrealized_pnl,
    }


def reserve_preflight(
    client: BybitDemoClient,
    new_notional_usd: float,
) -> dict[str, Any]:
    """Read remote funds/positions and decide whether one new order is safe."""
    try:
        new_notional = float(new_notional_usd)
    except (TypeError, ValueError):
        new_notional = None
    if new_notional is None or not math.isfinite(new_notional) or new_notional <= 0:
        return {
            "decision": "error",
            "reason": "invalid_notional",
            "max_exposure_usd": None,
            "equity_reserve_usd": None,
            "new_notional_usd": new_notional,
            "open_exposure_usd": None,
            "balance_usd": None,
            "unrealized_pnl_usd": None,
            "equity_usd": None,
            "exposure_gate_passed": False,
            "equity_gate_passed": False,
            "error": "new_notional_usd_invalid",
        }
    config = reserve_config()
    result: dict[str, Any] = {
        "decision": "error",
        "reason": "configuration_invalid",
        "max_exposure_usd": config["max_exposure_usd"],
        "equity_reserve_usd": config["equity_reserve_usd"],
        "new_notional_usd": new_notional,
        "open_exposure_usd": None,
        "balance_usd": None,
        "unrealized_pnl_usd": None,
        "equity_usd": None,
        "exposure_gate_passed": False,
        "equity_gate_passed": False,
        "error": config["configuration_error"],
    }
    if not config["valid"]:
        return result

    result["reason"] = "relay_or_api_error"
    try:
        snapshot = read_reserve_snapshot(client)
    except BybitDemoError as exc:
        result["error"] = f"{exc.endpoint}:{exc}"
        result["error_endpoint"] = exc.endpoint
        result["error_payload"] = exc.payload
        return result
    except (TypeError, ValueError) as exc:
        result["reason"] = "invalid_response"
        result["error"] = str(exc)
        return result

    open_exposure = snapshot["open_exposure_usd"]
    balance = snapshot["balance_usd"]
    unrealized_pnl = snapshot["unrealized_pnl_usd"]
    equity = snapshot["equity_usd"]
    max_exposure = float(config["max_exposure_usd"])
    equity_reserve = float(config["equity_reserve_usd"])
    exposure_passed = (
        Decimal(str(open_exposure)) + Decimal(str(new_notional))
        <= Decimal(str(max_exposure))
    )
    equity_passed = Decimal(str(equity)) >= Decimal(str(equity_reserve))
    result.update(
        {
            "open_exposure_usd": open_exposure,
            "balance_usd": balance,
            "unrealized_pnl_usd": unrealized_pnl,
            "equity_usd": equity,
            "exposure_gate_passed": exposure_passed,
            "equity_gate_passed": equity_passed,
            "error": None,
        }
    )
    if exposure_passed and equity_passed:
        result["decision"] = "allow"
        result["reason"] = "allowed"
    elif not exposure_passed and not equity_passed:
        result["decision"] = "blocked"
        result["reason"] = "exposure_cap_and_equity_floor"
    elif not exposure_passed:
        result["decision"] = "blocked"
        result["reason"] = "exposure_cap"
    else:
        result["decision"] = "blocked"
        result["reason"] = "equity_floor"
    return result


def _record_reserve_preflight(
    conn: Any,
    db_lock: threading.Lock,
    ledger_id: int,
    preflight: dict[str, Any],
    *,
    ts: int,
) -> None:
    fields = {
        "preflight_decision": preflight["decision"],
        "preflight_reason": preflight["reason"],
        "preflight_open_exposure_usd": preflight["open_exposure_usd"],
        "preflight_new_notional_usd": preflight["new_notional_usd"],
        "preflight_max_exposure_usd": preflight["max_exposure_usd"],
        "preflight_balance_usd": preflight["balance_usd"],
        "preflight_unrealized_pnl_usd": preflight["unrealized_pnl_usd"],
        "preflight_equity_usd": preflight["equity_usd"],
        "preflight_equity_reserve_usd": preflight["equity_reserve_usd"],
        "preflight_ts": ts,
    }
    _update_row(conn, db_lock, ledger_id, **fields)
    if preflight.get("error_payload") is not None:
        endpoint = str(preflight.get("error_endpoint") or "")
        _update_row(
            conn,
            db_lock,
            ledger_id,
            **{
                _raw_error_field(endpoint): _json(preflight["error_payload"]),
            },
        )
    health = record_reserve_health(
        success=preflight["decision"] != "error",
        error=preflight["reason"] if preflight["decision"] == "error" else None,
        snapshot=preflight if preflight["decision"] != "error" else None,
    )
    logger.info(
        "bybit_demo_reserve_preflight ledger_id=%d decision=%s reason=%s "
        "open_exposure_usd=%s new_notional_usd=%s max_exposure_usd=%s "
        "balance_usd=%s unrealized_pnl_usd=%s equity_usd=%s "
        "equity_reserve_usd=%s",
        ledger_id,
        preflight["decision"],
        preflight["reason"],
        preflight["open_exposure_usd"],
        preflight["new_notional_usd"],
        preflight["max_exposure_usd"],
        preflight["balance_usd"],
        preflight["unrealized_pnl_usd"],
        preflight["equity_usd"],
        preflight["equity_reserve_usd"],
    )
    if health["alert_triggered"]:
        logger.warning(
            "bybit_demo_reserve_health_alert failures=%d threshold=%d "
            "window_sec=%d reason=%s",
            health["failure_count"],
            health["threshold"],
            health["window_sec"],
            health["last_error"] or "reserve_preflight_error",
        )


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


def _exchange_time_ms(value: Any) -> int | None:
    """Parse a positive Bybit millisecond timestamp without inventing one."""
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _latest_execution_time_ms(executions: list[dict[str, Any]]) -> int | None:
    """Return the latest exchange execution timestamp from fill records."""
    timestamps = [
        parsed
        for item in executions
        if isinstance(item, dict)
        for parsed in [_exchange_time_ms(item.get("execTime"))]
        if parsed is not None
    ]
    return max(timestamps) if timestamps else None


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


def _submit_entry_order(
    conn: Any,
    db_lock: threading.Lock,
    client: BybitDemoClient,
    *,
    ledger_id: int,
    strategy: str,
    symbol: str,
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    order_link_id: str,
    origin: str,
    reversal_id: int | None,
    notional_usd: float,
) -> dict[str, Any]:
    """Run the existing reserve-gated entry POST for a persisted intent."""
    multi_tp = bybit_demo_multi_tp_enabled()
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
            origin=origin,
            reversal_id=reversal_id,
            last_error=None,
        )
        remote = client.get_order_realtime(
            symbol=symbol,
            order_link_id=order_link_id,
        )
        if remote:
            _record_order(conn, db_lock, ledger_id, remote[0], int(time.time()))
            tp_setup = None
            if multi_tp:
                tp_setup = _safe_post_entry_tp_setup(
                    conn,
                    db_lock,
                    client,
                    ledger_id=ledger_id,
                )
            response = {"status": "recovered", "ledger_id": ledger_id}
            if tp_setup is not None:
                response["tp_setup"] = tp_setup["status"]
            return response

        preflight = reserve_preflight(client, notional_usd)
        _record_reserve_preflight(
            conn, db_lock, ledger_id, preflight, ts=int(time.time())
        )
        if preflight["decision"] != "allow":
            _update_row(
                conn,
                db_lock,
                ledger_id,
                status="rejected",
                last_error=f"reserve_preflight:{preflight['reason']}",
            )
            return {
                "status": "rejected",
                "ledger_id": ledger_id,
                "reason": preflight["reason"],
                "preflight": preflight,
            }

        _update_row(conn, db_lock, ledger_id, status="submitting")
        result = client.create_market_order(
            symbol=symbol,
            direction=direction,
            qty=qty,
            take_profit=None if multi_tp else aligned_tp,
            stop_loss=aligned_sl,
            order_link_id=order_link_id,
        )
        _record_order(conn, db_lock, ledger_id, result, int(time.time()))
        tp_setup = None
        if multi_tp:
            tp_setup = _safe_post_entry_tp_setup(
                conn,
                db_lock,
                client,
                ledger_id=ledger_id,
            )
        logger.info(
            "bybit_demo_order_submitted ledger_id=%d strategy=%s symbol=%s "
            "direction=%s qty=%.12g notional=%.2f origin=%s reversal_id=%s",
            ledger_id,
            strategy,
            symbol.upper(),
            direction,
            qty,
            float(notional_usd),
            origin,
            reversal_id if reversal_id is not None else "-",
        )
        response = {"status": "submitted", "ledger_id": ledger_id}
        if tp_setup is not None:
            response["tp_setup"] = tp_setup["status"]
        return response
    except BybitDemoSizingError as exc:
        _update_row(conn, db_lock, ledger_id, status="rejected", last_error=str(exc))
        logger.warning(
            "bybit_demo_order_rejected ledger_id=%d reason=sizing_error",
            ledger_id,
        )
        return {"status": "rejected", "ledger_id": ledger_id, "reason": "sizing_error"}
    except BybitDemoError as exc:
        status = "unknown" if exc.retryable or exc.transport else "rejected"
        _update_row(
            conn,
            db_lock,
            ledger_id,
            status=status,
            last_error=str(exc),
            **_error_payload_fields(exc),
        )
        logger.warning(
            "bybit_demo_order_%s ledger_id=%d endpoint=%s reason=%s",
            status,
            ledger_id,
            exc.endpoint,
            str(exc),
        )
        return {"status": status, "ledger_id": ledger_id, "reason": str(exc)}
    except Exception:
        _update_row(
            conn, db_lock, ledger_id, status="unknown", last_error="unexpected_error"
        )
        logger.warning(
            "bybit_demo_order_unknown ledger_id=%d reason=unexpected_error",
            ledger_id,
        )
        return {"status": "unknown", "ledger_id": ledger_id, "reason": "unexpected_error"}


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
    atr_value: float | None = None,
    atr_candle_close_ts: int | None = None,
    notional_usd: float = BYBIT_DEMO_NOTIONAL_USD,
) -> dict[str, Any]:
    """Create one idempotent intent and submit exactly one market order."""
    if not is_allowed_signal(strategy, confirmation_level):
        return {"status": "filtered", "reason": "strategy_or_confirmation"}
    if source_is_shadow is True:
        # Defense in depth: the app gate runs first, but this isolated client
        # must also refuse a shadow source before creating an intent.
        return {"status": "filtered", "reason": "shadow_source"}
    if not bybit_demo_trading_enabled():
        # Keep this defense-in-depth gate immediately before any Bybit client
        # call.  The app-level gate preserves the local paper/shadow path, but
        # direct callers must be unable to create entries or reversals either.
        return {"status": "disabled", "reason": "trading_disabled"}
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
    multi_tp = bybit_demo_multi_tp_enabled()

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
    if multi_tp:
        atr_fields = {"protection_state": "awaiting_entry_fill"}
        if atr_value is None:
            atr_fields.update(
                {
                    "atr_value": None,
                    "atr_period": None,
                    "atr_timeframe": None,
                    "atr_method": None,
                    "atr_candle_close_ts": None,
                    "atr_source": None,
                }
            )
        else:
            try:
                atr_fields.update(
                    atr_provenance(atr_value, atr_candle_close_ts)
                )
            except BybitDemoSizingError as exc:
                atr_fields.update(
                    {
                        "atr_value": None,
                        "atr_period": None,
                        "atr_timeframe": None,
                        "atr_method": None,
                        "atr_candle_close_ts": None,
                        "atr_source": None,
                        "last_error": f"atr_provenance_invalid:{exc}",
                    }
                )
        _update_row(conn, db_lock, ledger_id, **atr_fields)
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

    existing_reversal = _reversal_row_for_symbol(conn, db_lock, symbol)
    live: dict[str, Any] | None = None
    managed_symbol_context = existing_reversal or _has_active_symbol_entries(
        conn, db_lock, symbol, exclude_ledger_id=ledger_id
    )
    try:
        live = _live_position(client.get_position(symbol), symbol)
    except BybitDemoError as exc:
        if managed_symbol_context:
            _update_row(
                conn,
                db_lock,
                ledger_id,
                status="unknown",
                last_error=f"live_position_read_failed:{exc}",
                **_error_payload_fields(exc),
            )
            logger.warning(
                "bybit_demo_reversal_position_read_failed symbol=%s endpoint=%s",
                symbol.upper(),
                exc.endpoint,
            )
            return {
                "status": "unknown",
                "ledger_id": ledger_id,
                "reason": "live_position_read_failed",
            }
        logger.warning(
            "bybit_demo_reversal_position_probe_unavailable symbol=%s endpoint=%s "
            "continuing_without_managed_position",
            symbol.upper(),
            exc.endpoint,
        )
        live = None
    except ValueError as exc:
        if managed_symbol_context:
            _update_row(
                conn,
                db_lock,
                ledger_id,
                status="unknown",
                last_error=f"live_position_invalid:{exc}",
            )
            return {
                "status": "unknown",
                "ledger_id": ledger_id,
                "reason": "live_position_invalid",
            }
        logger.warning(
            "bybit_demo_reversal_position_probe_invalid symbol=%s reason=%s "
            "continuing_without_managed_position",
            symbol.upper(),
            str(exc),
        )
        live = None

    if existing_reversal:
        state = str(existing_reversal["state"])
        if live is None:
            _update_reversal(
                conn,
                db_lock,
                int(existing_reversal["id"]),
                state="CLOSED",
                last_error=None,
            )
            existing_reversal = None
        elif state == "ACTIVE_AFTER_REVERSAL" and live["direction"] == direction:
            # A same-direction add is not a second reversal.
            existing_reversal = None
        else:
            reason = (
                "reversal_already_used"
                if state == "ACTIVE_AFTER_REVERSAL"
                else "reversal_recovery_required"
                if state == "RECOVERY_REQUIRED"
                else "reversal_in_progress"
            )
            _update_row(
                conn,
                db_lock,
                ledger_id,
                status="rejected",
                last_error=reason,
            )
            _reversal_event(
                conn,
                db_lock,
                int(existing_reversal["id"]),
                "blocked",
                status="blocked",
                reason=reason,
            )
            logger.warning(
                "bybit_demo_reversal_blocked symbol=%s direction=%s reason=%s",
                symbol.upper(),
                direction,
                reason,
            )
            return {
                "status": "blocked",
                "ledger_id": ledger_id,
                "reason": reason,
                "reversal_id": int(existing_reversal["id"]),
            }

    reversal_id: int | None = None
    source_ledger_ids: list[int] = []
    if live is not None and live["direction"] != direction:
        source_ledger_ids = _active_source_ledger_ids(
            conn, db_lock, symbol, live["direction"]
        )
        reversal_id = _claim_reversal(
            conn,
            db_lock,
            symbol=symbol,
            source_signal_key=signal_key,
            source_direction=live["direction"],
            target_direction=direction,
            source_ledger_ids=source_ledger_ids,
            now=now,
        )
        if reversal_id is None:
            _update_row(
                conn,
                db_lock,
                ledger_id,
                status="rejected",
                last_error="reversal_claim_lost",
            )
            logger.warning(
                "bybit_demo_reversal_blocked symbol=%s direction=%s "
                "reason=claim_lost",
                symbol.upper(),
                direction,
            )
            return {
                "status": "blocked",
                "ledger_id": ledger_id,
                "reason": "reversal_claim_lost",
            }
        close_result = _run_reversal_close(
            conn,
            db_lock,
            client,
            reversal_id=reversal_id,
            symbol=symbol,
            source_direction=live["direction"],
            source_ledger_ids=source_ledger_ids,
        )
        if close_result == "closed":
            _update_row(
                conn,
                db_lock,
                ledger_id,
                origin="reversal",
                reversal_id=reversal_id,
            )
            entry_result = _submit_entry_order(
                conn,
                db_lock,
                client,
                ledger_id=ledger_id,
                strategy=strategy,
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                order_link_id=order_link_id,
                origin="reversal",
                reversal_id=reversal_id,
                notional_usd=notional_usd,
            )
            if entry_result["status"] in {"submitted", "recovered"}:
                _update_reversal(
                    conn,
                    db_lock,
                    reversal_id,
                    state="ACTIVE_AFTER_REVERSAL",
                    current_ledger_id=ledger_id,
                    last_error=None,
                )
                _reversal_event(
                    conn,
                    db_lock,
                    reversal_id,
                    "open_submitted",
                    order_id=None,
                    status=entry_result["status"],
                    reason="reversal_entry",
                )
            else:
                _set_reversal_recovery(
                    conn,
                    db_lock,
                    reversal_id,
                    "reversal_open_failed",
                )
            return {**entry_result, "reversal_id": reversal_id}
        if close_result == "cancelled":
            # The position disappeared between the first read and the
            # close claim.  No reversal was used; continue as a normal entry.
            reversal_id = None
        else:
            _update_row(
                conn,
                db_lock,
                ledger_id,
                status="rejected",
                last_error="reversal_recovery_required",
            )
            return {
                "status": "blocked",
                "ledger_id": ledger_id,
                "reason": "reversal_recovery_required",
                "reversal_id": reversal_id,
            }

    return _submit_entry_order(
        conn,
        db_lock,
        client,
        ledger_id=ledger_id,
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        order_link_id=order_link_id,
        origin="signal",
        reversal_id=None,
        notional_usd=notional_usd,
    )


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
            "SELECT status, symbol, ts_submitted, ts_filled "
            "FROM bybit_demo_positions WHERE id=?",
            (ledger_id,),
        )
        existing_submission = existing_cursor.fetchone()
    first_submitted_at = (
        int(existing_submission[2])
        if existing_submission and existing_submission[2] is not None
        else now
    )
    existing_status = str(existing_submission[0] or "") if existing_submission else ""
    existing_filled_at = (
        int(existing_submission[3])
        if existing_submission and existing_submission[3] is not None
        else None
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
    is_filled = str(order.get("orderStatus", "")).lower() == "filled"
    first_observed_filled_at = existing_filled_at
    newly_observed_filled = (
        is_filled
        and first_observed_filled_at is None
        and existing_status in {
            "intent",
            "submitting",
            "submitted",
            "partially_filled",
            "unknown",
        }
    )
    if is_filled and first_observed_filled_at is None:
        first_observed_filled_at = now
    exchange_created_time = _exchange_time_ms(order.get("createdTime"))
    exchange_exec_time = _exchange_time_ms(order.get("execTime"))
    fields = {
        "status": status,
        "order_id": order_id,
        "order_status": order.get("orderStatus"),
        "executed_qty": executed_qty,
        "avg_entry_price": avg_price,
        "ts_submitted": first_submitted_at,
        "ts_filled": first_observed_filled_at,
        "last_polled": now,
        "last_error": None,
        "raw_order_json": _json(order),
    }
    if exchange_created_time is not None:
        fields["exchange_created_time"] = exchange_created_time
    if exchange_exec_time is not None:
        fields["exchange_exec_time"] = exchange_exec_time
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
    if newly_observed_filled:
        latency_sec = max(0.0, float(now) - float(first_submitted_at))
        logger.info(
            "bybit_demo_fill_observed ledger_id=%d order_id=%s symbol=%s "
            "ts_submitted=%s ts_filled=%s latency_sec=%.3f "
            "exchange_created_time=%s exchange_exec_time=%s",
            ledger_id,
            order_id or "-",
            existing_submission[1] if existing_submission else "-",
            first_submitted_at,
            first_observed_filled_at,
            latency_sec,
            exchange_created_time or "-",
            exchange_exec_time or "-",
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
    successful_requests = 0
    tp_legs_polled = 0
    tp_events_created = 0
    tp_reconciliation_errors = 0
    position_cache: dict[str, list[dict[str, Any]]] = {}
    closed_cache: dict[str, list[dict[str, Any]]] = {}
    execution_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def poll_request(fn, *args, **kwargs):
        nonlocal successful_requests
        result = fn(*args, **kwargs)
        successful_requests += 1
        return result

    for row in rows:
        row_id = int(row["id"])
        now = int(time.time())
        try:
            tp_result = reconcile_tp_legs(
                conn,
                db_lock,
                client,
                ledger_id=row_id,
                now=now,
            )
            tp_legs_polled += int(tp_result["polled"])
            tp_events_created += int(tp_result["events_created"])
            tp_reconciliation_errors += int(tp_result["errors"])
            successful_requests += int(tp_result["successful_requests"])
            order_items: list[dict[str, Any]] = []
            if row.get("order_id"):
                order_items = poll_request(
                    client.get_order_realtime,
                    symbol=row["symbol"],
                    order_id=str(row["order_id"]),
                )
            else:
                order_items = poll_request(
                    client.get_order_realtime,
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
                    execution_cache[execution_key] = poll_request(
                        client.get_executions,
                        row["symbol"], str(row["order_id"])
                    )
                executions = execution_cache[execution_key]
                if executions:
                    execution_fields = {
                        "raw_execution_json": _json({"executions": executions})
                    }
                    execution_time = _latest_execution_time_ms(executions)
                    if execution_time is not None:
                        execution_fields["exchange_exec_time"] = execution_time
                    _update_row(conn, db_lock, row_id, **execution_fields)

            symbol = str(row["symbol"]).upper()
            if symbol not in position_cache:
                position_cache[symbol] = poll_request(client.get_position, symbol)
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
                open_fields = {
                    "status": "open",
                    "position_size": position_size,
                    "avg_entry_price": avg_entry or row.get("avg_entry_price"),
                    "last_polled": now,
                    "raw_position_json": _json(position),
                }
                if row.get("be_state") != BYBIT_DEMO_BE_RECOVERY_STATE:
                    open_fields["last_error"] = None
                _update_row(
                    conn,
                    db_lock,
                    row_id,
                    **open_fields,
                )
                try:
                    ensure_breakeven_sl(
                        conn,
                        db_lock,
                        client,
                        ledger_id=row_id,
                        position=position,
                        now=now,
                    )
                except Exception as exc:
                    logger.warning(
                        "bybit_demo_breakeven_failed ledger_id=%d reason=%s",
                        row_id,
                        type(exc).__name__,
                    )
                    _breakeven_recovery(
                        conn,
                        db_lock,
                        ledger_id=row_id,
                        reason=f"be_unexpected_error:{type(exc).__name__}",
                        now=now,
                        raw_position=position,
                    )
                polled += 1
                continue

            if str(row.get("be_state") or "") == BYBIT_DEMO_BE_PENDING_STATE:
                try:
                    ensure_breakeven_sl(
                        conn,
                        db_lock,
                        client,
                        ledger_id=row_id,
                        now=now,
                    )
                except Exception as exc:
                    logger.warning(
                        "bybit_demo_breakeven_pending_failed ledger_id=%d reason=%s",
                        row_id,
                        type(exc).__name__,
                    )
                    _breakeven_recovery(
                        conn,
                        db_lock,
                        ledger_id=row_id,
                        reason=f"be_pending_unexpected_error:{type(exc).__name__}",
                        now=now,
                    )

            if symbol not in closed_cache:
                closed_cache[symbol] = poll_request(client.get_closed_pnl, symbol)
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
                _finalize_reversal_lifecycle(conn, db_lock, row_id)
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
                **_error_payload_fields(exc),
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

    if successful_requests:
        record_successful_poll()
    return {
        "status": "ok",
        "polled": polled,
        "closed": closed,
        "successful_requests": successful_requests,
        "tp_legs_polled": tp_legs_polled,
        "tp_events_created": tp_events_created,
        "tp_reconciliation_errors": tp_reconciliation_errors,
    }


def active_whitelist() -> list[dict[str, Any]]:
    """Return the three configured Bybit slots and the early-slot decision."""
    promoted = overheated_early_promoted()
    variants = allowed_signal_variants(overheated_early_is_promoted=promoted)
    third_strategy = "overheated_early" if promoted else "ema_cross_confirmed"
    return [
        {
            "slot": 1,
            "strategy": "overheated_24h",
            "confirmation_level": variants["overheated_24h"],
            "status": "active",
        },
        {
            "slot": 2,
            "strategy": "overheated_confirmed",
            "confirmation_level": variants["overheated_confirmed"],
            "status": "active",
        },
        {
            "slot": 3,
            "strategy": third_strategy,
            "confirmation_level": variants[third_strategy],
            "status": "active",
            "overheated_early_decision": (
                "promoted" if promoted else "not_promoted"
            ),
        },
    ]


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
        latest_preflight = conn.execute(
            """
            SELECT preflight_ts, preflight_decision, preflight_reason,
                   preflight_open_exposure_usd, preflight_new_notional_usd,
                   preflight_max_exposure_usd, preflight_balance_usd,
                   preflight_unrealized_pnl_usd, preflight_equity_usd,
                   preflight_equity_reserve_usd
            FROM bybit_demo_positions
            WHERE preflight_ts IS NOT NULL
            ORDER BY preflight_ts DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        reversal_counts = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN state IN (
                  'CLAIMED', 'CLOSING', 'OPEN_PENDING',
                  'ACTIVE_AFTER_REVERSAL', 'RECOVERY_REQUIRED'
              ) THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN state='RECOVERY_REQUIRED'
                       THEN 1 ELSE 0 END) AS recovery_required,
              SUM(CASE WHEN reversal_used=1 THEN 1 ELSE 0 END) AS used
            FROM bybit_demo_reversals
            """
        ).fetchone()
        latest_recovery = conn.execute(
            """
            SELECT id, symbol, state, recovery_reason, close_attempts,
                   reversal_claimed, reversal_used, tp_legs_cancelled,
                   tp_cancel_errors, updated_ts
            FROM bybit_demo_reversals
            WHERE state='RECOVERY_REQUIRED'
            ORDER BY updated_ts DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        open_positions = conn.execute(
            """
            SELECT strategy, symbol,
                   COALESCE(ts_filled, ts_submitted, ts_created) AS opened_at
            FROM bybit_demo_positions
            WHERE status='open'
              AND (position_size IS NULL OR position_size > 0)
            ORDER BY opened_at ASC, id ASC
            """
        ).fetchall()
    leak_count = int(row[4] or 0)
    confirmed_leak_count = int(row[5] or 0)
    uncertain_leak_count = int(row[6] or 0)
    fallback_pre_gate_count = int(row[7] or 0)
    fallback_post_fix_count = int(row[8] or 0)
    config = reserve_config()
    poll_health = polling_health_status()
    early_promoted = overheated_early_promoted()
    return {
        "enabled": client.enabled,
        "configured": client.enabled,
        "trading_enabled": bybit_demo_trading_enabled(),
        "route": client.route,
        "relay_required": client.require_relay,
        "relay_configured": client.relay_configured,
        "configuration_error": client.configuration_error,
        "notional_usd": BYBIT_DEMO_NOTIONAL_USD,
        "reserve_preflight": {
            "max_exposure_usd": config["max_exposure_usd"],
            "equity_reserve_usd": config["equity_reserve_usd"],
            "configuration_error": config["configuration_error"],
            "health": reserve_health_status(),
            "latest": (
                {
                    "timestamp": latest_preflight[0],
                    "decision": latest_preflight[1],
                    "reason": latest_preflight[2],
                    "open_exposure_usd": latest_preflight[3],
                    "new_notional_usd": latest_preflight[4],
                    "max_exposure_usd": latest_preflight[5],
                    "balance_usd": latest_preflight[6],
                    "unrealized_pnl_usd": latest_preflight[7],
                    "equity_usd": latest_preflight[8],
                    "equity_reserve_usd": latest_preflight[9],
                }
                if latest_preflight
                else None
            ),
        },
        "total": int(row[0] or 0),
        "unfinished": int(row[1] or 0),
        "open": int(row[2] or 0),
        "last_polled": row[3],
        **poll_health,
        "active_whitelist": active_whitelist(),
        "overheated_early_decision": {
            "status": "promoted" if early_promoted else "not_promoted",
            "active": early_promoted,
            "strategy": "overheated_early",
        },
        "open_positions": [
            {
                "strategy": position[0],
                "symbol": position[1],
                "opened_at": position[2],
            }
            for position in open_positions
        ],
        "reversal_telemetry": {
            "total": int(reversal_counts[0] or 0),
            "active": int(reversal_counts[1] or 0),
            "recovery_required": int(reversal_counts[2] or 0),
            "used": int(reversal_counts[3] or 0),
            "latest_recovery": (
                {
                    "reversal_id": latest_recovery[0],
                    "symbol": latest_recovery[1],
                    "state": latest_recovery[2],
                    "reason": latest_recovery[3],
                    "close_attempts": latest_recovery[4],
                    "reversal_claimed": bool(latest_recovery[5]),
                    "reversal_used": bool(latest_recovery[6]),
                    "tp_legs_cancelled": int(latest_recovery[7] or 0),
                    "tp_cancel_errors": int(latest_recovery[8] or 0),
                    "updated_at": latest_recovery[9],
                }
                if latest_recovery
                else None
            ),
        },
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
        "overheated_early_promoted": early_promoted,
    }