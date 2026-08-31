#!/usr/bin/env python3
"""Read-only Gate.io historical pump/liquidation pattern scanner.

This module deliberately does not import ``app`` or any production trading
module.  It reads public Gate.io Futures data and writes standalone reports
for the exploratory pattern:

    pump -> correction -> long liquidations -> large_5m_flow -> support retest

The liquidation sign mapping is never assumed.  A caller must provide one or
two externally verified examples in ``--sign-examples`` before a scan can run.

The examples file is a JSON list such as::

    {
      "created_at": "2026-08-30T12:00:00Z",
      "examples": [
        {
          "symbol": "BTCUSDT",
          "ts": "2026-06-04T02:00:00Z",
          "expected_side": "long",
          "rationale": "Externally documented long-squeeze event ...",
          "sources": ["https://example.invalid/source"]
        }
      ]
    }

``expected_side`` must come from independent evidence for that historical
example.  The size sign is discovered from the complete Gate response and is
never copied from production code or supplied as an expected input.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests


GATE_API_BASE = "https://api.gateio.ws/api/v4/futures/usdt"
FIFTEEN_MINUTES = 15 * 60
FIVE_MINUTES = 5 * 60
ONE_HOUR = 60 * 60
DAY = 24 * 60 * 60

LOOKBACK_DAYS = 91
TOP_N = 50
CONTROL_SYMBOLS = ("BTC_USDT", "ETH_USDT", "SOL_USDT")
PUMP_RETURN = 0.15
PUMP_LOOKBACK_BARS = 32
CORRECTION_RETURN = 0.08
CORRECTION_BARS = 48
LIQUIDATION_MIN_USD = 100_000.0
LIQUIDATION_HOURLY_FRACTION = 0.02
FLOW_MULTIPLIER = 3.0
FLOW_LOOKAHEAD_HOURS = 6
FLOW_BASELINE_BARS = 288
OUTCOME_HOURS = 24
MIN_COMPLETE_EVENT_HOURS = 12 + 1 + FLOW_LOOKAHEAD_HOURS + OUTCOME_HOURS
LIQ_LIMIT = 1000
MAX_CANDLE_CHUNK_BARS = 999
MIN_LIQ_SUBRANGE_SECONDS = 60

EVENT_FIELDS = [
    "symbol",
    "cohort",
    "pump_ts",
    "pump_utc",
    "pump_episode_end_ts",
    "support",
    "pump_high",
    "correction_ts",
    "correction_utc",
    "liq_window_start_ts",
    "liq_window_end_ts",
    "long_liq_notional_usd",
    "hour_futures_notional_usd",
    "liq_threshold_usd",
    "flow_ts",
    "flow_utc",
    "flow_notional_usd",
    "flow_baseline_median_usd",
    "flow_threshold_usd",
    "outcome_ts",
    "outcome_utc",
    "support_retest_ts",
    "support_retest_utc",
    "outcome",
    "reason",
]

COVERAGE_FIELDS = [
    "symbol",
    "cohort",
    "ticker_notional_24h",
    "ticker_rank",
    "included_primary",
    "event_capable",
    "candle_15m_status",
    "candle_15m_count",
    "candle_15m_start_ts",
    "candle_15m_end_ts",
    "liquidation_status",
    "flow_5m_status",
    "reason",
]
PREFLIGHT_FIELDS = [
    "ok",
    "size_field",
    "reason",
    "sign_to_side",
    "example_count",
    "created_at",
    "inference_basis",
]
SUMMARY_FIELDS = ["metric", "value"]
RESOLVED_OUTCOMES = frozenset(
    {"success_continuation", "success_retest_hold", "failure_breakdown"}
)
SUCCESS_OUTCOMES = frozenset({"success_continuation", "success_retest_hold"})
MIN_RATIONALE_LENGTH = 40


class ScanError(RuntimeError):
    """Expected, user-facing scan failure."""


class PreflightError(ScanError):
    """The required liquidation sign calibration is not safe to use."""


def utc(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_ts(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip()
        try:
            result = float(text)
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp ISO-8601 value must include a timezone")
            result = parsed.timestamp()
    if not math.isfinite(result) or result < 0:
        raise ValueError("timestamp must be a finite non-negative value")
    return int(result)


def floor_ts(ts: int, step: int) -> int:
    return int(ts) // step * step


def gate_contract(symbol: str) -> str:
    value = symbol.upper().strip()
    if value.endswith("USDT") and not value.endswith("_USDT"):
        return value[:-4] + "_USDT"
    return value


def internal_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    return value[:-5] + "USDT" if value.endswith("_USDT") else value


def _positive_notional(size: Any, price: Any) -> float:
    parsed_size = finite_float(size)
    parsed_price = finite_float(price)
    if parsed_size is None or parsed_price is None or parsed_price <= 0:
        return 0.0
    return abs(parsed_size) * parsed_price


def _record_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("contract"),
        record.get("time"),
        record.get("size"),
        record.get("order_size"),
        record.get("fill_price"),
        record.get("order_price"),
    )


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_notional: float


@dataclass
class FetchStatus:
    status: str
    count: int = 0
    reason: str = ""
    requests: int = 0
    overflow_splits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PumpEpisode:
    symbol: str
    first_ts: int
    last_candidate_ts: int
    support: float | None
    pump_high: float


@dataclass
class SignPreflight:
    ok: bool
    size_field: str
    examples: list[dict[str, Any]]
    sign_to_side: dict[str, str]
    reason: str
    created_at_ts: int
    inference_basis: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sign_to_side"] = dict(self.sign_to_side)
        payload["created_at"] = utc(self.created_at_ts)
        return payload


@dataclass(frozen=True)
class SignExamplesFile:
    created_at_ts: int
    examples: list[dict[str, Any]]


class GateClient:
    """Small public REST client with bounded retries.

    ``request_fn`` is injectable for unit tests.  No credentials are accepted
    or sent because every endpoint used by this scanner is public.
    """

    def __init__(
        self,
        *,
        base_url: str = GATE_API_BASE,
        timeout: float = 20.0,
        retries: int = 3,
        request_fn: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.request_fn = request_fn or requests.get
        self.sleep_fn = sleep_fn

    def get(self, path: str, params: Mapping[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.request_fn(
                    url,
                    params=dict(params),
                    timeout=self.timeout,
                    headers={"Accept": "application/json"},
                )
                status_code = getattr(response, "status_code", 200)
                if status_code == 429 or status_code >= 500:
                    if attempt >= self.retries:
                        raise ScanError(
                            f"Gate HTTP {status_code} after {attempt + 1} attempts"
                        )
                    retry_after = finite_float(
                        getattr(response, "headers", {}).get("Retry-After")
                    )
                    self.sleep_fn(
                        retry_after
                        if retry_after is not None and retry_after >= 0
                        else min(2.0**attempt, 8.0)
                    )
                    continue
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                return response.json()
            except ScanError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                self.sleep_fn(min(2.0**attempt, 8.0))
        raise ScanError(f"Gate request failed for {path}: {last_error}")

    def fetch_tickers(self) -> list[dict[str, Any]]:
        raw = self.get("/tickers", {})
        if not isinstance(raw, list):
            raise ScanError("Gate tickers response is not a list")
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            contract = str(item.get("contract") or "")
            quote = finite_float(
                item.get("volume_24h_quote") or item.get("volume_24h_settle")
            )
            if contract.endswith("_USDT") and quote is not None and quote > 0:
                result.append(
                    {
                        "contract": contract,
                        "ticker_notional_24h": quote,
                        "last": finite_float(item.get("last")),
                    }
                )
        return sorted(
            result,
            key=lambda item: item["ticker_notional_24h"],
            reverse=True,
        )

    def fetch_candles(
        self,
        contract: str,
        interval: str,
        start_ts: int,
        end_ts: int,
    ) -> tuple[list[Candle], FetchStatus]:
        step = FIVE_MINUTES if interval == "5m" else FIFTEEN_MINUTES if interval == "15m" else None
        if step is None:
            raise ValueError(f"unsupported interval: {interval}")
        start_ts = floor_ts(start_ts, step)
        end_ts = floor_ts(end_ts, step)
        if end_ts <= start_ts:
            return [], FetchStatus("empty")

        rows: dict[int, Candle] = {}
        status = FetchStatus("complete")
        cursor = start_ts
        while cursor < end_ts:
            chunk_end = min(
                end_ts,
                cursor + MAX_CANDLE_CHUNK_BARS * step,
            )
            try:
                raw = self.get(
                    "/candlesticks",
                    {
                        "contract": contract,
                        "interval": interval,
                        # Gate rejects limit together with from/to.  Keep this
                        # request bounded by 999 candles without sending limit.
                        "from": cursor,
                        "to": chunk_end - 1,
                    },
                )
                status.requests += 1
                if not isinstance(raw, list):
                    raise ScanError("candlesticks response is not a list")
                if len(raw) > MAX_CANDLE_CHUNK_BARS:
                    status.status = "incomplete"
                    status.reason = "candle_response_exceeded_999"
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    ts = int(item["t"])
                    values = [
                        finite_float(item.get(key))
                        for key in ("o", "h", "l", "c", "sum")
                    ]
                    if ts < start_ts or ts >= end_ts or any(v is None for v in values):
                        continue
                    rows[ts] = Candle(ts, *[float(v) for v in values])
            except Exception as exc:
                status.status = "incomplete"
                status.reason = f"candle_fetch_error:{exc}"
            cursor = chunk_end

        candles = [rows[ts] for ts in sorted(rows)]
        expected = set(range(start_ts, end_ts, step))
        missing = expected.difference(rows)
        status.count = len(candles)
        if missing and status.status == "complete":
            status.status = "incomplete"
            status.reason = f"missing_candles:{len(missing)}"
        if not candles and status.status == "complete":
            status.status = "empty"
        return candles, status

    def fetch_liquidations(
        self,
        contract: str,
        start_ts: int,
        end_ts: int,
    ) -> tuple[list[dict[str, Any]], FetchStatus]:
        """Fetch one bounded window, recursively splitting a 1000-row response.

        Gate allows at most one hour per request for this endpoint.  A
        full-limit response is never treated as complete: it is split until
        the response is below the cap or one-minute granularity is reached.
        """
        start_ts = int(start_ts)
        end_ts = int(end_ts)
        if end_ts <= start_ts:
            return [], FetchStatus("empty")
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        status = FetchStatus("complete")

        def collect(left: int, right: int) -> None:
            nonlocal status
            if right <= left:
                return
            try:
                raw = self.get(
                    "/liq_orders",
                    {
                        "contract": contract,
                        "from": left,
                        "to": right - 1,
                        "limit": LIQ_LIMIT,
                    },
                )
                status.requests += 1
                if not isinstance(raw, list):
                    status.status = "incomplete"
                    status.reason = "liquidation_response_not_list"
                    return
                if len(raw) < LIQ_LIMIT:
                    for item in raw:
                        if isinstance(item, dict):
                            rows[_record_key(item)] = item
                    return
                if right - left <= MIN_LIQ_SUBRANGE_SECONDS:
                    for item in raw:
                        if isinstance(item, dict):
                            rows[_record_key(item)] = item
                    status.status = "incomplete"
                    status.reason = "liquidation_limit_at_minute"
                    return
                midpoint = left + (right - left) // 2
                status.overflow_splits += 1
                collect(left, midpoint)
                collect(midpoint, right)
            except Exception as exc:
                status.status = "incomplete"
                status.reason = f"liquidation_fetch_error:{exc}"

        # The public endpoint rejects wider windows; callers currently pass
        # one hour, but splitting here also protects direct function callers.
        cursor = start_ts
        while cursor < end_ts:
            right = min(cursor + ONE_HOUR, end_ts)
            collect(cursor, right)
            cursor = right
        result = sorted(
            rows.values(),
            key=lambda item: (
                finite_float(item.get("time")) or 0,
                str(item.get("contract") or ""),
            ),
        )
        status.count = len(result)
        return result, status


def _candle_map(candles: Sequence[Candle]) -> dict[int, Candle]:
    return {candle.ts: candle for candle in candles}


def detect_pump_episodes(
    symbol: str,
    candles: Sequence[Candle],
    *,
    pump_return: float = PUMP_RETURN,
    lookback_bars: int = PUMP_LOOKBACK_BARS,
) -> list[PumpEpisode]:
    """Detect completed 15m pump candidates and merge adjacent detections."""
    ordered = sorted(candles, key=lambda candle: candle.ts)
    if len(ordered) <= lookback_bars:
        return []
    candidates: list[int] = []
    for index in range(lookback_bars, len(ordered)):
        baseline = ordered[index - lookback_bars].close
        if baseline > 0 and ordered[index].close >= baseline * (1.0 + pump_return):
            candidates.append(index)
    if not candidates:
        return []

    groups: list[list[int]] = [[candidates[0]]]
    for index in candidates[1:]:
        if ordered[index].ts - ordered[groups[-1][-1]].ts == FIFTEEN_MINUTES:
            groups[-1].append(index)
        else:
            groups.append([index])

    episodes: list[PumpEpisode] = []
    for group in groups:
        first_index = group[0]
        last_index = group[-1]
        window_start = max(0, first_index - lookback_bars)
        window = ordered[window_start : last_index + 1]
        support = ordered[first_index - lookback_bars].close
        pump_high = max(candle.high for candle in window)
        episodes.append(
            PumpEpisode(
                symbol=symbol,
                first_ts=ordered[first_index].ts,
                last_candidate_ts=ordered[last_index].ts,
                support=support,
                pump_high=pump_high,
            )
        )
    return episodes


def correction_for_episode(
    episode: PumpEpisode,
    candles: Sequence[Candle],
    *,
    correction_return: float = CORRECTION_RETURN,
    correction_bars: int = CORRECTION_BARS,
) -> Candle | None:
    ordered = sorted(candles, key=lambda candle: candle.ts)
    after = [
        candle
        for candle in ordered
        if candle.ts > episode.last_candidate_ts
    ][:correction_bars]
    target = episode.pump_high * (1.0 - correction_return)
    return next((candle for candle in after if candle.low <= target), None)


def dominant_size_sign(
    records: Iterable[Mapping[str, Any]],
    *,
    size_field: str = "size",
) -> tuple[int | None, dict[str, float]]:
    totals = {"+1": 0.0, "-1": 0.0}
    for record in records:
        value = finite_float(record.get(size_field))
        notional = _positive_notional(value, record.get("fill_price") or record.get("order_price"))
        if value is None or notional <= 0:
            continue
        totals["+1" if value > 0 else "-1"] += notional
    if totals["+1"] == totals["-1"] or max(totals.values()) <= 0:
        return None, totals
    sign = 1 if totals["+1"] > totals["-1"] else -1
    return sign, totals


def validate_sign_preflight(
    examples: Sequence[Mapping[str, Any]],
    fetch_fn: Callable[[str, int, int], tuple[list[dict[str, Any]], FetchStatus]],
    *,
    size_field: str = "size",
) -> SignPreflight:
    """Validate the sign mapping from manually supplied external examples.

    Each example must contain ``symbol``, ``ts``, ``expected_side`` and
    ``expected_size_sign``.  The expected values are operator-supplied from
    an independently recognizable historical example; the API response is
    then checked against them.  This prevents silently inheriting an
    undocumented convention from production code.
    """
    if not 1 <= len(examples) <= 2:
        raise PreflightError("provide exactly one or two sign examples")
    observed: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for index, example in enumerate(examples, start=1):
        try:
            symbol = gate_contract(str(example["symbol"]))
            ts = parse_ts(example["ts"])
            expected_side = str(example["expected_side"]).lower()
            expected_sign = int(example["expected_size_sign"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PreflightError(f"invalid sign example #{index}: {exc}") from exc
        if expected_side not in {"long", "short"} or expected_sign not in {-1, 1}:
            raise PreflightError(
                f"sign example #{index} needs expected_side long/short and "
                "expected_size_sign -1/1"
            )
        records, status = fetch_fn(symbol, ts - 30 * 60, ts + 30 * 60)
        if status.status != "complete":
            raise PreflightError(
                f"sign example #{index} has incomplete liquidation coverage: "
                f"{status.reason or status.status}"
            )
        sign, totals = dominant_size_sign(records, size_field=size_field)
        if sign is None:
            raise PreflightError(f"sign example #{index} has no dominant sign")
        if sign != expected_sign:
            raise PreflightError(
                f"sign example #{index} observed sign {sign}, expected "
                f"{expected_sign}; refusing to scan"
            )
        key = str(sign)
        previous = mapping.get(key)
        if previous is not None and previous != expected_side:
            raise PreflightError(
                f"sign examples contradict each other for size sign {sign}"
            )
        mapping[key] = expected_side
        observed.append(
            {
                "symbol": symbol,
                "ts": ts,
                "utc": utc(ts),
                "expected_side": expected_side,
                "expected_size_sign": expected_sign,
                "observed_size_sign": sign,
                "notional_by_sign_usd": totals,
                "record_count": len(records),
                "coverage": status.as_dict(),
            }
        )
    if "long" not in mapping.values():
        raise PreflightError("preflight must validate the long-liquidation sign")
    return SignPreflight(
        ok=True,
        size_field=size_field,
        examples=observed,
        sign_to_side=mapping,
        reason="manual sign examples validated",
    )


def classify_support_retest(
    candles: Sequence[Candle],
    *,
    support: float,
    start_ts: int,
    end_ts: int,
) -> tuple[str, int | None, str]:
    if support <= 0 or end_ts <= start_ts:
        return "invalid", None, "invalid_support_or_outcome_window"
    for candle in sorted(candles, key=lambda item: item.ts):
        if start_ts <= candle.ts < end_ts and candle.low <= support <= candle.high:
            if candle.close >= support:
                return "success", candle.ts, "support_touched_and_15m_closed_above"
    return "failure", None, "support_not_retested_and_held"


def find_large_flow(
    candles_5m: Sequence[Candle],
    *,
    start_ts: int,
    end_ts: int,
    multiplier: float = FLOW_MULTIPLIER,
    baseline_bars: int = FLOW_BASELINE_BARS,
) -> tuple[Candle | None, str]:
    ordered = sorted(candles_5m, key=lambda candle: candle.ts)
    by_ts = _candle_map(ordered)
    for ts in range(start_ts, end_ts, FIVE_MINUTES):
        candle = by_ts.get(ts)
        if candle is None:
            return None, f"missing_5m_candle:{ts}"
        baseline = [
            by_ts.get(ts - FIVE_MINUTES * offset)
            for offset in range(1, baseline_bars + 1)
        ]
        if any(item is None for item in baseline):
            return None, f"missing_5m_baseline:{ts}"
        values = [item.quote_notional for item in baseline if item is not None]
        median = statistics.median(values)
        if median > 0 and candle.quote_notional >= multiplier * median and candle.close > candle.open:
            return candle, "large_5m_flow_found"
    return None, "large_5m_flow_not_found"


def _sum_hour_notional(
    candles_15m: Sequence[Candle],
    start_ts: int,
) -> tuple[float, str]:
    by_ts = _candle_map(candles_15m)
    hour = [by_ts.get(start_ts + index * FIFTEEN_MINUTES) for index in range(4)]
    if any(item is None for item in hour):
        return 0.0, "missing_15m_hour_notional"
    return sum(item.quote_notional for item in hour if item is not None), "complete"


def scan_symbol_events(
    symbol: str,
    candles_15m: Sequence[Candle],
    candles_5m: Sequence[Candle],
    client: GateClient,
    *,
    cohort: str,
    size_field: str,
    long_size_sign: int,
    complete_event_cutoff_ts: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    episodes = detect_pump_episodes(symbol, candles_15m)
    if complete_event_cutoff_ts is not None:
        episodes = [
            episode
            for episode in episodes
            if episode.first_ts <= complete_event_cutoff_ts
        ]
    coverage = {
        "symbol": symbol,
        "cohort": cohort,
        "candle_15m_status": "complete",
        "candle_15m_count": len(candles_15m),
        "candle_15m_start_ts": candles_15m[0].ts if candles_15m else None,
        "candle_15m_end_ts": candles_15m[-1].ts if candles_15m else None,
        "liquidation_status": "not_requested",
        "flow_5m_status": "not_requested",
        "reason": "no_pump_candidates" if not episodes else "",
    }
    events: list[dict[str, Any]] = []
    for episode in episodes:
        correction = correction_for_episode(episode, candles_15m)
        if correction is None:
            events.append(
                _event_row(
                    symbol,
                    cohort,
                    episode,
                    reason="correction_not_found_in_12h",
                )
            )
            continue
        liq_start = correction.ts
        liq_end = liq_start + ONE_HOUR
        records, liq_status = client.fetch_liquidations(
            gate_contract(symbol), liq_start, liq_end
        )
        coverage["liquidation_status"] = liq_status.status
        if liq_status.status != "complete":
            events.append(
                _event_row(
                    symbol,
                    cohort,
                    episode,
                    correction=correction,
                    reason=f"liquidation_coverage_{liq_status.status}:"
                    f"{liq_status.reason}",
                )
            )
            continue
        long_liq = sum(
            _positive_notional(
                item.get(size_field),
                item.get("fill_price") or item.get("order_price"),
            )
            for item in records
            if (finite_float(item.get(size_field)) or 0) * long_size_sign > 0
        )
        hour_notional, hour_status = _sum_hour_notional(candles_15m, liq_start)
        if hour_status != "complete":
            events.append(
                _event_row(
                    symbol,
                    cohort,
                    episode,
                    correction=correction,
                    long_liq=long_liq,
                    reason=hour_status,
                )
            )
            continue
        threshold = max(
            LIQUIDATION_MIN_USD,
            LIQUIDATION_HOURLY_FRACTION * hour_notional,
        )
        if long_liq < threshold:
            events.append(
                _event_row(
                    symbol,
                    cohort,
                    episode,
                    correction=correction,
                    long_liq=long_liq,
                    hour_notional=hour_notional,
                    threshold=threshold,
                    reason="long_liquidation_threshold_not_met",
                )
            )
            continue
        flow_start = liq_end
        flow_end = flow_start + FLOW_LOOKAHEAD_HOURS * 60 * 60
        flow, flow_reason = find_large_flow(
            candles_5m,
            start_ts=flow_start,
            end_ts=flow_end,
        )
        coverage["flow_5m_status"] = (
            "incomplete" if flow_reason.startswith("missing_") else "complete"
        )
        if flow is None:
            events.append(
                _event_row(
                    symbol,
                    cohort,
                    episode,
                    correction=correction,
                    long_liq=long_liq,
                    hour_notional=hour_notional,
                    threshold=threshold,
                    reason=flow_reason,
                )
            )
            continue
        baseline = [
            candle.quote_notional
            for candle in candles_5m
            if flow.ts - DAY <= candle.ts < flow.ts
        ]
        baseline_median = statistics.median(baseline) if baseline else 0.0
        support_start = flow.ts + FIVE_MINUTES
        outcome_end = support_start + OUTCOME_HOURS * 60 * 60
        outcome, retest_ts, outcome_reason = classify_support_retest(
            candles_15m,
            support=episode.support,
            start_ts=support_start,
            end_ts=outcome_end,
        )
        events.append(
            _event_row(
                symbol,
                cohort,
                episode,
                correction=correction,
                long_liq=long_liq,
                hour_notional=hour_notional,
                threshold=threshold,
                flow=flow,
                baseline_median=baseline_median,
                outcome=outcome,
                retest_ts=retest_ts,
                reason=outcome_reason,
            )
        )
    return events, coverage


def _event_row(
    symbol: str,
    cohort: str,
    episode: PumpEpisode,
    *,
    correction: Candle | None = None,
    long_liq: float | None = None,
    hour_notional: float | None = None,
    threshold: float | None = None,
    flow: Candle | None = None,
    baseline_median: float | None = None,
    outcome: str = "not_reached",
    retest_ts: int | None = None,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "cohort": cohort,
        "pump_ts": episode.first_ts,
        "pump_utc": utc(episode.first_ts),
        "pump_episode_end_ts": episode.last_candidate_ts,
        "support": episode.support,
        "pump_high": episode.pump_high,
        "correction_ts": correction.ts if correction else None,
        "correction_utc": utc(correction.ts) if correction else None,
        "liq_window_start_ts": correction.ts if correction else None,
        "liq_window_end_ts": correction.ts + ONE_HOUR if correction else None,
        "long_liq_notional_usd": long_liq,
        "hour_futures_notional_usd": hour_notional,
        "liq_threshold_usd": threshold,
        "flow_ts": flow.ts if flow else None,
        "flow_utc": utc(flow.ts) if flow else None,
        "flow_notional_usd": flow.quote_notional if flow else None,
        "flow_baseline_median_usd": baseline_median,
        "flow_threshold_usd": (
            baseline_median * FLOW_MULTIPLIER
            if baseline_median is not None
            else None
        ),
        "support_retest_ts": retest_ts,
        "support_retest_utc": utc(retest_ts),
        "outcome": outcome,
        "reason": reason,
    }


def load_sign_examples(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PreflightError(f"cannot read sign examples: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("examples")
    if not isinstance(payload, list):
        raise PreflightError("sign examples file must contain a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_markdown(report: Mapping[str, Any]) -> str:
    config = report["config"]
    preflight = report["preflight"]
    events = report["events"]
    coverage = report["coverage"]
    lines = [
        "# Historical pump → liquidation pattern scan",
        "",
        "**Read-only report. Production scoring, filters, whitelist, execution, "
        "polling, TP/SL, reserve protection, and Telegram behavior are unchanged.**",
        "",
        f"- Generated: **{report['generated_utc']}**",
        f"- Window: **{utc(config['start_ts'])} → {utc(config['end_ts'])}**",
        f"- Lookback requested: **{config['lookback_days']} days**",
        f"- Primary universe: top **{config['top_n']}** by 24h quote notional, "
        "then event-capable filter",
        f"- Controls: **{', '.join(config['control_symbols'])}**",
        "",
        "## Mandatory liquidation sign preflight",
        "",
        f"- Result: **{'PASS' if preflight.get('ok') else 'FAIL'}**",
        f"- Calibrated field: `{preflight.get('size_field')}`",
        f"- Mapping: `{preflight.get('sign_to_side')}`",
        f"- Reason: {preflight.get('reason')}",
        "",
        "The mapping is calibrated from operator-supplied, independently "
        "recognizable examples. It is not inherited from production code.",
        "",
        "## Definitions",
        "",
        "- Pump: completed 15m close is at least **+15%** versus 32 bars earlier.",
        "- Adjacent pump detections are one episode; only the first timestamp "
        "creates downstream work.",
        "- Correction: first low at least **8% below pump high** in the next 12h.",
        "- Long liquidation: real Gate `/liq_orders` records whose calibrated "
        "size sign maps to `long`; notional is `abs(size) × fill_price`.",
        "- Liquidation threshold: `max($100,000, 2% × hourly futures notional)`.",
        "- `large_5m_flow`: 5m quote notional at least **3×** the previous 24h "
        "median and `close > open`; this is not proof of one large buyer.",
        "- Support: close immediately before the 8h pump window.",
        "- Success: within 24h after flow, a completed 15m candle touches support "
        "and closes at or above it.",
        "",
        "## Aggregate results",
        "",
        f"- Pump episodes: **{len(events)}**",
        f"- Successful support retests: **{sum(row.get('outcome') == 'success' for row in events)}**",
        f"- Primary events: **{sum(row.get('cohort') == 'primary' for row in events)}**",
        f"- Control events: **{sum(row.get('cohort') == 'control' for row in events)}**",
        "",
        "## Event rows",
        "",
        "| Symbol | Cohort | Pump UTC | Liquidation USD | Flow UTC | Outcome | Reason |",
        "|---|---|---|---:|---|---|---|",
    ]
    for row in events:
        lines.append(
            f"| {row['symbol']} | {row['cohort']} | {row['pump_utc']} | "
            f"{_display_number(row.get('long_liq_notional_usd'))} | "
            f"{row.get('flow_utc') or '—'} | {row.get('outcome')} | "
            f"{row.get('reason') or '—'} |"
        )
    lines += [
        "",
        "## Coverage and exclusions",
        "",
        "| Symbol | Cohort | 15m status | 15m bars | Primary | Event-capable | Reason |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in coverage:
        lines.append(
            f"| {row['symbol']} | {row['cohort']} | {row.get('candle_15m_status')} | "
            f"{row.get('candle_15m_count', 0)} | {row.get('included_primary')} | "
            f"{row.get('event_capable')} | {row.get('reason') or '—'} |"
        )
    lines += [
        "",
        "## Data-quality guardrails",
        "",
        "- Gate liquidation history is real exchange liquidation data, not a "
        "price/volume liquidation proxy.",
        "- Gate liquidation requests are limited to one hour and use `limit=1000`; "
        "a full response is recursively split and never accepted as complete.",
        "- Missing candles, rate-limit failures, and capped liquidation windows "
        "remain incomplete statuses; they are not converted to zero.",
        "- BTC/ETH/SOL are controls and do not dilute the primary event-capable cohort.",
    ]
    return "\n".join(lines) + "\n"


def _display_number(value: Any) -> str:
    number = finite_float(value)
    return "—" if number is None else f"{number:,.0f}"


def _default_end_ts() -> int:
    return floor_ts(int(time.time()), FIFTEEN_MINUTES)


def run_scan(
    client: GateClient,
    *,
    sign_examples: Sequence[Mapping[str, Any]],
    output_dir: Path,
    start_ts: int,
    end_ts: int,
    lookback_days: int = LOOKBACK_DAYS,
    top_n: int = TOP_N,
    control_symbols: Sequence[str] = CONTROL_SYMBOLS,
    workers: int = 4,
    size_field: str = "size",
) -> dict[str, Any]:
    if end_ts <= start_ts:
        raise ScanError("end timestamp must be after start timestamp")
    if lookback_days < LOOKBACK_DAYS:
        raise ScanError(f"lookback must be at least {LOOKBACK_DAYS} days")
    if workers < 1:
        raise ScanError("workers must be positive")

    preflight = validate_sign_preflight(
        sign_examples,
        lambda symbol, left, right: client.fetch_liquidations(symbol, left, right),
        size_field=size_field,
    )
    long_signs = [
        int(sign)
        for sign, side in preflight.sign_to_side.items()
        if side == "long"
    ]
    if len(long_signs) != 1:
        raise PreflightError(
            "preflight must produce exactly one unambiguous long size sign"
        )
    long_size_sign = long_signs[0]
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "preflight.json").write_text(
        json.dumps(preflight.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    tickers = client.fetch_tickers()
    top = tickers[:top_n]
    ticker_by_contract = {item["contract"]: item for item in tickers}
    controls = [gate_contract(symbol) for symbol in control_symbols]
    contracts: list[str] = []
    for item in top:
        if item["contract"] not in contracts:
            contracts.append(item["contract"])
    for control in controls:
        if control not in contracts:
            contracts.append(control)
    ranks = {item["contract"]: index + 1 for index, item in enumerate(tickers)}

    def load_15m(contract: str) -> tuple[str, list[Candle], FetchStatus]:
        candles, status = client.fetch_candles(
            contract,
            "15m",
            start_ts - PUMP_LOOKBACK_BARS * FIFTEEN_MINUTES,
            end_ts,
        )
        return contract, candles, status

    candles_15m: dict[str, list[Candle]] = {}
    candle_status: dict[str, FetchStatus] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(load_15m, contract) for contract in contracts]
        for future in as_completed(futures):
            contract, candles, status = future.result()
            candles_15m[contract] = candles
            candle_status[contract] = status

    eligible: set[str] = set()
    for contract, candles in candles_15m.items():
        if candle_status.get(contract, FetchStatus("empty")).status == "complete" and detect_pump_episodes(
            internal_symbol(contract), candles
        ):
            eligible.add(contract)

    primary = [
        contract
        for contract in top
        if contract["contract"] in eligible
        and contract["contract"] not in controls
    ]
    primary_contracts = {item["contract"] for item in primary}
    scan_contracts = list(primary_contracts | set(controls))
    coverage: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    complete_event_cutoff_ts = end_ts - MIN_COMPLETE_EVENT_HOURS * 60 * 60

    for contract in scan_contracts:
        symbol = internal_symbol(contract)
        cohort = "control" if contract in controls else "primary"
        status = candle_status.get(contract, FetchStatus("empty"))
        candles = candles_15m.get(contract, [])
        event_capable = contract in eligible
        ticker = ticker_by_contract.get(contract, {})
        row = {
            "symbol": symbol,
            "cohort": cohort,
            "ticker_notional_24h": ticker.get("ticker_notional_24h"),
            "ticker_rank": ranks.get(contract),
            "included_primary": contract in primary_contracts,
            "event_capable": event_capable,
            "candle_15m_status": status.status,
            "candle_15m_count": len(candles),
            "candle_15m_start_ts": candles[0].ts if candles else None,
            "candle_15m_end_ts": candles[-1].ts if candles else None,
            "liquidation_status": "not_requested",
            "flow_5m_status": "not_requested",
            "reason": (
                status.reason
                or ("not_event_capable" if not event_capable and cohort == "primary" else "")
            ),
        }
        coverage.append(row)
        if status.status != "complete" or not candles:
            continue
        if cohort == "primary" and not event_capable:
            continue
        episodes = detect_pump_episodes(symbol, candles)
        episodes = [
            episode
            for episode in episodes
            if episode.first_ts <= complete_event_cutoff_ts
        ]
        flow_candles: list[Candle] = []
        for episode in episodes:
            correction = correction_for_episode(episode, candles)
            if correction is not None:
                left = correction.ts + ONE_HOUR - DAY
                right = correction.ts + ONE_HOUR + FLOW_LOOKAHEAD_HOURS * 60 * 60
                flow, flow_status = client.fetch_candles(contract, "5m", left, right)
                if flow_status.status != "complete":
                    row["flow_5m_status"] = flow_status.status
                flow_candles.extend(flow)
        if episodes and row["flow_5m_status"] == "not_requested":
            row["flow_5m_status"] = "complete"
        symbol_events, symbol_coverage = scan_symbol_events(
            symbol,
            candles,
            flow_candles,
            client,
            cohort=cohort,
            size_field=size_field,
            long_size_sign=long_size_sign,
            complete_event_cutoff_ts=complete_event_cutoff_ts,
        )
        events.extend(symbol_events)
        if symbol_coverage.get("liquidation_status") not in {"not_requested", None}:
            row["liquidation_status"] = symbol_coverage["liquidation_status"]
        if symbol_coverage.get("flow_5m_status") == "incomplete":
            row["flow_5m_status"] = "incomplete"

    events.sort(key=lambda row: (row["pump_ts"], row["symbol"]))
    coverage.sort(key=lambda row: (row["cohort"], row["symbol"]))
    generated_ts = int(time.time())
    report = {
        "generated_ts": generated_ts,
        "generated_utc": utc(generated_ts),
        "production_changes": False,
        "config": {
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "lookback_days": lookback_days,
            "top_n": top_n,
            "control_symbols": [internal_symbol(item) for item in controls],
            "pump_return": PUMP_RETURN,
            "pump_lookback_bars": PUMP_LOOKBACK_BARS,
            "correction_return": CORRECTION_RETURN,
            "correction_bars": CORRECTION_BARS,
            "liquidation_min_usd": LIQUIDATION_MIN_USD,
            "liquidation_hourly_fraction": LIQUIDATION_HOURLY_FRACTION,
            "flow_multiplier": FLOW_MULTIPLIER,
            "flow_lookahead_hours": FLOW_LOOKAHEAD_HOURS,
            "outcome_hours": OUTCOME_HOURS,
            "size_field": size_field,
        },
        "preflight": preflight.as_dict(),
        "universe": {
            "top_tickers": top,
            "primary_contracts": sorted(primary_contracts),
            "control_contracts": sorted(controls),
            "event_capable_contracts": sorted(eligible),
        },
        "events": events,
        "coverage": coverage,
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "events.csv", events, EVENT_FIELDS)
    _write_csv(output_dir / "coverage.csv", coverage, COVERAGE_FIELDS)
    _write_csv(
        output_dir / "preflight.csv",
        [
            {
                "ok": preflight.ok,
                "size_field": preflight.size_field,
                "reason": preflight.reason,
                "sign_to_side": json.dumps(
                    preflight.sign_to_side, sort_keys=True
                ),
                "example_count": len(preflight.examples),
            }
        ],
        PREFLIGHT_FIELDS,
    )
    _write_csv(
        output_dir / "summary.csv",
        [
            {"metric": "event_rows", "value": len(events)},
            {
                "metric": "successful_support_retests",
                "value": sum(row.get("outcome") == "success" for row in events),
            },
            {
                "metric": "primary_events",
                "value": sum(row.get("cohort") == "primary" for row in events),
            },
            {
                "metric": "control_events",
                "value": sum(row.get("cohort") == "control" for row in events),
            },
            {"metric": "coverage_rows", "value": len(coverage)},
            {
                "metric": "excluded_top_universe_symbols",
                "value": sum(
                    item["contract"] not in primary_contracts
                    and item["contract"] not in controls
                    for item in top
                ),
            },
        ],
        SUMMARY_FIELDS,
    )
    _write_csv(
        output_dir / "universe.csv",
        [
            {
                "contract": item["contract"],
                "symbol": internal_symbol(item["contract"]),
                "ticker_rank": ranks.get(item["contract"]),
                "ticker_notional_24h": item["ticker_notional_24h"],
                "cohort": (
                    "control"
                    if item["contract"] in controls
                    else "primary"
                    if item["contract"] in primary_contracts
                    else "excluded"
                ),
                "event_capable": item["contract"] in eligible,
                "reason": (
                    "control"
                    if item["contract"] in controls
                    else "event_capable"
                    if item["contract"] in eligible
                    else "no_15m_pump_in_lookback"
                ),
            }
            for item in top
        ] + [
            {
                "contract": contract,
                "symbol": internal_symbol(contract),
                "ticker_rank": ranks.get(contract),
                "ticker_notional_24h": ticker_by_contract.get(contract, {}).get(
                    "ticker_notional_24h"
                ),
                "cohort": "control",
                "event_capable": contract in eligible,
                "reason": "control",
            }
            for contract in controls
            if contract not in {item["contract"] for item in top}
        ],
        [
            "contract",
            "symbol",
            "ticker_rank",
            "ticker_notional_24h",
            "cohort",
            "event_capable",
            "reason",
        ],
    )
    (output_dir / "report.md").write_text(
        build_markdown(report),
        encoding="utf-8",
    )
    return report


def write_failed_preflight(output_dir: Path, error: Exception) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": False,
        "error": str(error),
        "generated_utc": utc(int(time.time())),
    }
    (output_dir / "preflight.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_csv(
        output_dir / "preflight.csv",
        [
            {
                "ok": False,
                "size_field": "size",
                "reason": str(error),
                "sign_to_side": "{}",
                "example_count": 0,
            }
        ],
        PREFLIGHT_FIELDS,
    )
    (output_dir / "report.md").write_text(
        "# Historical pump → liquidation pattern scan\n\n"
        "**ABORTED before data scan: liquidation sign preflight failed.**\n\n"
        f"- Reason: {error}\n"
        "- No event counts were produced. No production behavior was changed.\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sign-examples",
        type=Path,
        required=True,
        help=(
            "JSON list of 1-2 manual examples with symbol, ts, expected_side "
            "(long/short), and independently verified expected_size_sign (-1/1)"
        ),
    )
    parser.add_argument("--out", type=Path, default=Path("outcome_historical_liquidation_pattern"))
    parser.add_argument("--start", type=parse_ts)
    parser.add_argument("--end", type=parse_ts, default=_default_end_ts())
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--size-field", default="size", choices=("size", "order_size"))
    parser.add_argument("--api-base", default=GATE_API_BASE)
    args = parser.parse_args(argv)
    end_ts = floor_ts(args.end, FIFTEEN_MINUTES)
    start_ts = (
        floor_ts(args.start, FIFTEEN_MINUTES)
        if args.start is not None
        else end_ts - args.lookback_days * DAY
    )
    try:
        examples = load_sign_examples(args.sign_examples)
        report = run_scan(
            GateClient(base_url=args.api_base),
            sign_examples=examples,
            output_dir=args.out,
            start_ts=start_ts,
            end_ts=end_ts,
            lookback_days=args.lookback_days,
            top_n=args.top_n,
            workers=args.workers,
            size_field=args.size_field,
        )
    except (ScanError, OSError, ValueError) as exc:
        write_failed_preflight(args.out, exc)
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {args.out / 'report.md'} "
        f"(events={len(report['events'])}, "
        f"primary={len(report['universe']['primary_contracts'])}, "
        f"controls={len(report['universe']['control_contracts'])})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())