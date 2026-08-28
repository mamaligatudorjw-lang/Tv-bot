#!/usr/bin/env python3
"""Read-only TP-vs-SL analysis for the strong-signal shadow strategies.

The script reads resolved demo positions and the append-only runtime log.  It
never imports app.py, writes to SQLite, or changes signal behavior.  Runtime
log values are retained as rounded observations with explicit provenance.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Iterable

import requests

from shadow_outcome_report import fetch_candles


TARGET_STRATEGIES = (
    "ema_cross_confirmed",
    "overheated_early",
    "ema_cross",
    "overheated_confirmed",
)
MIN_GROUP_N = 20
BOOTSTRAP_ITERATIONS = 800
PERMUTATION_ITERATIONS = 800
RANDOM_SEED = 143
HOUR_SEC = 60 * 60
HISTORICAL_INTERVAL = "1h"
HISTORICAL_INTERVAL_SEC = HOUR_SEC
HISTORICAL_WARMUP_HOURS = 30
DEFAULT_HISTORY_WORKERS = 3
LOG_MATCH_WINDOWS = {
    "ema_cross": 120.0,
    "overheated_early": 120.0,
    "ema_cross_confirmed": 300.0,
    "overheated_confirmed": 300.0,
}

LOG_TIMESTAMP_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)"
)
EMA_CROSS_RE = re.compile(
    r"EMA cross shadow (?P<symbol>\S+) (?P<direction>LONG|SHORT): "
    r"price=(?P<price>[-\d.eE]+) gap=(?P<gap>[-\d.eE]+)% "
    r"sl=(?P<sl>[-\d.eE]+) tp=(?P<tp>[-\d.eE]+)"
)
OVERHEATED_EARLY_RE = re.compile(
    r"overheated_early (?:PRECHECK|EVAL) (?P<symbol>\S+): "
    r"pct24=(?P<pct24>[-\d.eE]+)%.*?rsi=(?P<rsi>[-\d.eE]+)"
)
CONFIRMED_RE = re.compile(
    r"cont_confirmed: (?P<strategy>ema_cross_confirmed|overheated_confirmed) "
    r"(?P<symbol>\S+) (?P<direction>LONG|SHORT) "
    r"confirmed(?:#(?P<number>\d+))? @(?P<entry>[-\d.eE]+) "
    r"signal=(?P<signal>[-\d.eE]+) vol=(?P<volume>[-\d.eE]+)x"
    r"(?: tp_mult=(?P<tp_mult>[-\d.eE]+)x)? age=(?P<age>\d+)min"
)

FEATURE_META = {
    "risk_pct": {
        "label": "SL distance from entry (%)",
        "provenance": "exact_persisted_derived",
        "description": "abs(entry_price - sl_price) / entry_price",
    },
    "reward_pct": {
        "label": "TP distance from entry (%)",
        "provenance": "exact_persisted_derived",
        "description": "abs(tp_price - entry_price) / entry_price",
    },
    "reward_risk": {
        "label": "TP/SL distance ratio",
        "provenance": "exact_persisted_derived",
        "description": "reward_pct / risk_pct",
    },
    "entry_vs_signal_pct": {
        "label": "Directional entry move from signal (%)",
        "provenance": "exact_persisted_derived",
        "description": "direction-adjusted entry_price vs signal_price",
    },
    "ema_gap_pct_log": {
        "label": "EMA cross gap (%)",
        "provenance": "runtime_log_rounded",
        "description": "EMA(9)-EMA(21) gap emitted by the signal path",
    },
    "overheated_pct24_log": {
        "label": "Overheated 24h move (%)",
        "provenance": "runtime_log_rounded",
        "description": "pct24 emitted by the overheated early signal path",
    },
    "overheated_rsi_log": {
        "label": "Overheated RSI",
        "provenance": "runtime_log_rounded",
        "description": "RSI emitted by the overheated early signal path",
    },
    "confirmation_volume_ratio_log": {
        "label": "Confirmation volume ratio (x)",
        "provenance": "runtime_log_rounded",
        "description": "completed-candle volume / 10-bar average",
    },
    "confirmation_number_log": {
        "label": "Confirmation number",
        "provenance": "runtime_log_exact_integer",
        "description": "confirmation count emitted by continuation telemetry",
    },
    "confirmation_age_min_log": {
        "label": "Confirmation age (minutes)",
        "provenance": "runtime_log_exact_integer",
        "description": "age of the parent signal at confirmation",
    },
    "price_return_1h_pct": {
        "label": "Directional price change, 1h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "direction-adjusted close-to-close return over the last completed 1h candle",
    },
    "price_return_2h_pct": {
        "label": "Directional price change, 2h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "direction-adjusted close-to-close return over the last two completed 1h candles",
    },
    "price_return_4h_pct": {
        "label": "Directional price change, 4h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "direction-adjusted close-to-close return over the last four completed 1h candles",
    },
    "range_1h_pct": {
        "label": "Candle range, 1h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "high-low range of the last completed 1h candle divided by its low",
    },
    "range_2h_pct": {
        "label": "Window range, 2h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "high-low range across the last two completed 1h candles",
    },
    "range_4h_pct": {
        "label": "Window range, 4h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "high-low range across the last four completed 1h candles",
    },
    "realized_vol_2h_pct": {
        "label": "Realized volatility, 2h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "population standard deviation of completed 1h log returns in the 2h window",
    },
    "realized_vol_4h_pct": {
        "label": "Realized volatility, 4h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "population standard deviation of completed 1h log returns in the 4h window",
    },
    "volume_ratio_1h_vs_24h": {
        "label": "Volume ratio, 1h vs prior 24h",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "latest completed 1h volume divided by the mean of the preceding 24 completed 1h volumes",
    },
    "volume_change_1h_pct": {
        "label": "Volume change, last 1h (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "latest completed 1h volume change versus the preceding completed 1h candle",
    },
    "volume_acceleration_pct": {
        "label": "Volume acceleration (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "change in volume growth rate across the last three completed 1h candles",
    },
    "momentum_acceleration_pct": {
        "label": "Momentum acceleration (%)",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "directional 1h return minus the average directional return per hour over 2h",
    },
    "momentum_decay_ratio": {
        "label": "Momentum decay ratio",
        "provenance": "reconstructed_historical_gateio_1h",
        "description": "absolute directional 1h return divided by absolute directional 4h return per hour",
    },
}


def fmt_ts(value: int | float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def parse_log_ts(line: str) -> float | None:
    match = LOG_TIMESTAMP_RE.search(line)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match["stamp"], "%Y-%m-%d %H:%M:%S,%f")
        return stamp.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(value)
    return parsed


def parse_runtime_log(path: Path) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    events: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return events
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            ts = parse_log_ts(line)
            if ts is None:
                continue
            match = EMA_CROSS_RE.search(line)
            if match:
                events[(
                    "ema_cross", match["symbol"], match["direction"]
                )].append({
                    "ts": ts,
                    "ema_gap_pct_log": _number(match["gap"]),
                })
                continue
            match = OVERHEATED_EARLY_RE.search(line)
            if match:
                events[(
                    "overheated_early", match["symbol"], "LONG"
                )].append({
                    "ts": ts,
                    "overheated_pct24_log": _number(match["pct24"]),
                    "overheated_rsi_log": _number(match["rsi"]),
                })
                continue
            match = CONFIRMED_RE.search(line)
            if match:
                strategy = match["strategy"]
                events[(
                    strategy, match["symbol"], match["direction"]
                )].append({
                    "ts": ts,
                    "confirmation_volume_ratio_log": _number(match["volume"]),
                    "confirmation_number_log": int(match["number"] or 1),
                    "confirmation_age_min_log": int(match["age"]),
                    "confirmation_tp_mult_log": (
                        _number(match["tp_mult"])
                        if match["tp_mult"] is not None else None
                    ),
                    "confirmed_entry_log": _number(match["entry"]),
                    "confirmed_signal_log": _number(match["signal"]),
                })
    for values in events.values():
        values.sort(key=lambda event: event["ts"])
    return events


def nearest_event(
    events: dict[tuple[str, str, str], list[dict[str, Any]]],
    strategy: str,
    symbol: str,
    direction: str,
    ts_open: int,
) -> dict[str, Any]:
    values = events.get((strategy, symbol, direction), [])
    if not values:
        return {}
    timestamps = [float(event["ts"]) for event in values]
    index = bisect_left(timestamps, float(ts_open))
    candidates = []
    if index < len(values):
        candidates.append(values[index])
    if index:
        candidates.append(values[index - 1])
    event = min(candidates, key=lambda item: abs(item["ts"] - ts_open))
    if abs(event["ts"] - ts_open) > LOG_MATCH_WINDOWS[strategy]:
        return {}
    return dict(event)


def load_positions(
    db_path: Path, *, resolved_only: bool = False
) -> list[dict[str, Any]]:
    """Load target shadow positions through a read-only SQLite connection."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    resolved_clause = (
        "AND status IN ('tp', 'sl') AND exit_price IS NOT NULL AND exit_price > 0"
        if resolved_only
        else ""
    )
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type,
                       is_shadow, signal_price
                  FROM demo_positions
                 WHERE is_shadow=1
                   AND alert_type IN (?, ?, ?, ?)
                   AND direction IN ('LONG', 'SHORT')
                   AND entry_price > 0
                   AND sl_price > 0
                   AND tp_price > 0
                    {resolved_clause}
                 ORDER BY ts_open, id
                """,
                TARGET_STRATEGIES,
            )
        ]
    finally:
        conn.close()
    return rows


def load_resolved(db_path: Path) -> list[dict[str, Any]]:
    """Backward-compatible resolved-only loader used by the prior report."""
    return load_positions(db_path, resolved_only=True)


def load_position_counts(db_path: Path) -> dict[str, dict[str, int]]:
    """Return total/status counts without changing the database."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """
            SELECT alert_type, status, COUNT(*)
              FROM demo_positions
             WHERE is_shadow=1
               AND alert_type IN (?, ?, ?, ?)
               AND direction IN ('LONG', 'SHORT')
               AND entry_price > 0 AND sl_price > 0 AND tp_price > 0
             GROUP BY alert_type, status
            """,
            TARGET_STRATEGIES,
        ).fetchall()
    finally:
        conn.close()
    counts: dict[str, dict[str, int]] = {
        strategy: {"total": 0, "resolved": 0, "tp": 0, "sl": 0}
        for strategy in TARGET_STRATEGIES
    }
    for strategy, status, count in rows:
        item = counts[strategy]
        item["total"] += int(count)
        if status in ("tp", "sl"):
            item["resolved"] += int(count)
            item[status] += int(count)
        else:
            item[str(status)] = int(count)
    return counts


def _normalise_market_candles(
    candles: Iterable[dict[str, Any]],
) -> list[dict[str, float | int]]:
    """Keep only valid Gate.io OHLCV candles in chronological order."""
    by_ts: dict[int, dict[str, float | int]] = {}
    for candle in candles:
        try:
            timestamp = int(candle["t"])
            values = {
                "t": timestamp,
                "o": float(candle["o"]),
                "h": float(candle["h"]),
                "l": float(candle["l"]),
                "c": float(candle["c"]),
                "v": float(candle["v"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if (
            values["o"] > 0 and values["h"] > 0 and values["l"] > 0
            and values["c"] > 0 and values["v"] >= 0
            and values["h"] >= values["l"]
        ):
            by_ts[timestamp] = values
    return [by_ts[timestamp] for timestamp in sorted(by_ts)]


def _contiguous_window(
    candles: list[dict[str, float | int]], bars: int
) -> list[dict[str, float | int]] | None:
    if len(candles) < bars:
        return None
    window = candles[-bars:]
    timestamps = [int(candle["t"]) for candle in window]
    if any(
        right - left != HISTORICAL_INTERVAL_SEC
        for left, right in zip(timestamps, timestamps[1:])
    ):
        return None
    return window


def _directional_return(
    direction: str, first_close: float, last_close: float
) -> float | None:
    if first_close <= 0:
        return None
    raw = (last_close - first_close) / first_close * 100.0
    return raw if direction == "LONG" else -raw


def historical_features(
    candles: Iterable[dict[str, Any]], ts_open: int, direction: str
) -> dict[str, Any]:
    """Reconstruct lookahead-safe features from completed 1h Gate.io candles."""
    normalised = _normalise_market_candles(candles)
    completed = [
        candle for candle in normalised
        if int(candle["t"]) + HISTORICAL_INTERVAL_SEC <= int(ts_open)
    ]
    feature_fields = [
        field for field in FEATURE_META
        if field.startswith((
            "price_return_", "range_", "realized_vol_", "volume_", "momentum_"
        ))
    ]
    missing = {field: None for field in feature_fields}
    missing["historical_feature_status"] = (
        "no_candles" if not normalised else "insufficient_or_gapped_history"
    )
    if not completed:
        return missing

    features: dict[str, Any] = {
        **missing,
        "historical_feature_status": "ok",
        "historical_last_candle_ts": int(completed[-1]["t"]),
        "historical_last_candle_utc": fmt_ts(int(completed[-1]["t"])),
    }
    windows: dict[int, list[dict[str, float | int]] | None] = {
        bars: _contiguous_window(completed, bars) for bars in (1, 2, 4)
    }
    returns: dict[int, float | None] = {}
    latest = float(completed[-1]["c"])
    for bars, window in windows.items():
        returns[bars] = None
        if window is not None:
            returns[bars] = _directional_return(
                direction, float(window[0]["c"]), latest
            )
            low = min(float(candle["l"]) for candle in window)
            high = max(float(candle["h"]) for candle in window)
            features[f"range_{bars}h_pct"] = (
                (high - low) / low * 100.0 if low > 0 else None
            )
    features.update({
        "price_return_1h_pct": returns[1],
        "price_return_2h_pct": returns[2],
        "price_return_4h_pct": returns[4],
    })

    for bars in (2, 4):
        window = windows[bars]
        if window is None:
            continue
        close_returns = [
            math.log(float(right["c"]) / float(left["c"])) * 100.0
            for left, right in zip(window, window[1:])
            if float(left["c"]) > 0 and float(right["c"]) > 0
        ]
        if close_returns:
            features[f"realized_vol_{bars}h_pct"] = (
                stdev(close_returns) if len(close_returns) >= 2 else 0.0
            )

    latest_volume = float(completed[-1]["v"])
    prior_volume = float(completed[-2]["v"]) if len(completed) >= 2 else None
    prior_prior_volume = (
        float(completed[-3]["v"]) if len(completed) >= 3 else None
    )
    baseline = completed[-25:-1] if len(completed) >= 25 else []
    baseline_volumes = [float(candle["v"]) for candle in baseline]
    if baseline_volumes:
        average_volume = mean(baseline_volumes)
        if average_volume > 0:
            features["volume_ratio_1h_vs_24h"] = latest_volume / average_volume
    if prior_volume is not None and prior_volume > 0:
        features["volume_change_1h_pct"] = (
            (latest_volume - prior_volume) / prior_volume * 100.0
        )
    if (
        prior_volume is not None and prior_prior_volume is not None
        and prior_volume > 0 and prior_prior_volume > 0
    ):
        latest_growth = (latest_volume - prior_volume) / prior_volume * 100.0
        prior_growth = (prior_volume - prior_prior_volume) / prior_prior_volume * 100.0
        features["volume_acceleration_pct"] = latest_growth - prior_growth
    if returns[1] is not None and returns[2] is not None:
        features["momentum_acceleration_pct"] = returns[1] - returns[2] / 2.0
    if (
        returns[1] is not None and returns[4] is not None
        and abs(returns[4]) > 1e-12
    ):
        features["momentum_decay_ratio"] = abs(returns[1]) / (abs(returns[4]) / 4.0)
    return features


def fetch_historical_histories(
    rows: Iterable[dict[str, Any]],
    output_dir: Path,
    *,
    workers: int = DEFAULT_HISTORY_WORKERS,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Fetch one cached 1h history per symbol, with explicit failure coverage."""
    rows = list(rows)
    if not rows:
        return {}, {"status": "no_signals", "symbols_requested": 0}
    symbols = sorted({str(row["symbol"]) for row in rows})
    min_ts = min(int(row["ts_open"]) for row in rows)
    max_ts = max(int(row["ts_open"]) for row in rows)
    start = min_ts - HISTORICAL_WARMUP_HOURS * HOUR_SEC
    required_last_candle_ts = (max_ts // HOUR_SEC) * HOUR_SEC - HOUR_SEC
    cache_dir = output_dir / "candle_cache_1h"
    cache_dir.mkdir(parents=True, exist_ok=True)
    histories: dict[str, list[dict[str, Any]]] = {}
    cached = 0
    pending: list[str] = []
    for symbol in symbols:
        cache_path = cache_dir / f"{symbol}.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            candles = _normalise_market_candles(payload)
            if (
                candles
                and int(candles[0]["t"]) <= start + HOUR_SEC
                and int(candles[-1]["t"]) >= required_last_candle_ts
            ):
                histories[symbol] = candles
                cached += 1
                continue
        except (OSError, ValueError, TypeError):
            pass
        pending.append(symbol)

    failures: dict[str, str] = {}

    def fetch_one(symbol: str) -> tuple[str, list[dict[str, Any]]]:
        with requests.Session() as session:
            candles = fetch_candles(
                session,
                symbol,
                start,
                max_ts,
                interval=HISTORICAL_INTERVAL,
                interval_sec=HISTORICAL_INTERVAL_SEC,
            )
        return symbol, _normalise_market_candles(candles)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_one, symbol): symbol for symbol in pending}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                fetched_symbol, candles = future.result()
                if candles:
                    histories[fetched_symbol] = candles
                    (cache_dir / f"{fetched_symbol}.json").write_text(
                        json.dumps(candles, ensure_ascii=False),
                        encoding="utf-8",
                    )
                else:
                    failures[symbol] = "empty_response"
            except Exception as exc:
                failures[symbol] = f"{type(exc).__name__}: {exc}"
    return histories, {
        "status": "ok" if not failures else "partial",
        "interval": HISTORICAL_INTERVAL,
        "requested_start_ts": start,
        "requested_end_ts": max_ts,
        "requested_start_utc": fmt_ts(start),
        "requested_end_utc": fmt_ts(max_ts),
        "symbols_requested": len(symbols),
        "symbols_cached": cached,
        "symbols_fetched": len(pending) - len(failures),
        "symbols_with_history": len(histories),
        "symbols_failed": len(failures),
        "failed_symbols": failures,
    }


def enrich_rows(
    rows: Iterable[dict[str, Any]],
    events: dict[tuple[str, str, str], list[dict[str, Any]]],
    candles_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    candles_by_symbol = candles_by_symbol or {}
    enriched = []
    for source in rows:
        row = dict(source)
        entry = float(row["entry_price"])
        stop = float(row["sl_price"])
        target = float(row["tp_price"])
        exit_value = row.get("exit_price")
        exit_price = float(exit_value) if exit_value not in (None, "") else None
        direction = str(row["direction"])
        risk = abs(stop - entry)
        reward = abs(target - entry)
        result_r = None
        if exit_price is not None and risk > 0:
            result_r = (
                (exit_price - entry) / risk
                if direction == "LONG"
                else (entry - exit_price) / risk
            )
        signal_price = row.get("signal_price")
        directional_entry_move = None
        if signal_price is not None and float(signal_price) > 0:
            signal = float(signal_price)
            directional_entry_move = (
                (entry - signal) / signal * 100.0
                if direction == "LONG"
                else (signal - entry) / signal * 100.0
            )
        event = nearest_event(
            events,
            str(row["alert_type"]),
            str(row["symbol"]),
            direction,
            int(row["ts_open"]),
        )
        row.update({
            "ts_open_utc": fmt_ts(int(row["ts_open"])),
            "ts_close_utc": fmt_ts(row.get("ts_close")),
            "outcome": (
                "tp" if row.get("status") == "tp"
                else "sl" if row.get("status") == "sl"
                else None
            ),
            "result_r": result_r if result_r is not None and math.isfinite(result_r) else None,
            "risk_pct": risk / entry * 100.0,
            "reward_pct": reward / entry * 100.0,
            "reward_risk": reward / risk if risk > 0 else None,
            "entry_vs_signal_pct": directional_entry_move,
            "log_match_ts": event.get("ts"),
            "log_match_delta_sec": (
                abs(float(event["ts"]) - int(row["ts_open"]))
                if event else None
            ),
        })
        row.update(event)
        row["outcome"] = (
            "tp" if row.get("status") == "tp"
            else "sl" if row.get("status") == "sl"
            else None
        )
        row.update(
            historical_features(
                candles_by_symbol.get(str(row["symbol"]), []),
                int(row["ts_open"]),
                direction,
            )
        )
        enriched.append(row)
    return enriched


def _finite_values(rows: Iterable[dict[str, Any]], field: str) -> list[float]:
    values = []
    for row in rows:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def describe(rows: Iterable[dict[str, Any]], field: str) -> dict[str, Any]:
    values = _finite_values(rows, field)
    if not values:
        return {
            "n": 0, "coverage_pct": 0.0, "mean": None, "median": None,
            "stdev": None, "min": None, "max": None,
        }
    return {
        "n": len(values),
        "mean": round(mean(values), 6),
        "median": round(median(values), 6),
        "stdev": round(stdev(values), 6) if len(values) > 1 else 0.0,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(row.get("outcome") == "tp" for row in rows)
    sl = sum(row.get("outcome") == "sl" for row in rows)
    rs = [
        float(row["result_r"])
        for row in rows
        if row.get("result_r") is not None
        and math.isfinite(float(row["result_r"]))
    ]
    n = tp + sl
    return {
        "n": n,
        "total_n": len(rows),
        "tp": tp,
        "sl": sl,
        "unresolved_n": len(rows) - n,
        "resolved_wr_pct": round(100.0 * tp / n, 2) if n else None,
        "avg_r": round(mean(rs), 6) if rs else None,
        "sample_status": "ready" if n >= MIN_GROUP_N else "insufficient_sample",
    }


def _rank_auc(higher_values: list[float], lower_values: list[float]) -> float:
    if not higher_values or not lower_values:
        return float("nan")
    wins = ties = 0.0
    for higher in higher_values:
        for lower in lower_values:
            if higher > lower:
                wins += 1.0
            elif higher == lower:
                ties += 1.0
    return (wins + ties * 0.5) / (len(higher_values) * len(lower_values))


def _cliffs_delta(tp_values: list[float], sl_values: list[float]) -> float:
    auc = _rank_auc(tp_values, sl_values)
    return 2.0 * auc - 1.0 if math.isfinite(auc) else float("nan")


def _bootstrap_delta_ci(
    tp_values: list[float], sl_values: list[float], rng: random.Random
) -> tuple[float | None, float | None]:
    if not tp_values or not sl_values:
        return None, None
    sampled = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        tp = [rng.choice(tp_values) for _ in tp_values]
        sl = [rng.choice(sl_values) for _ in sl_values]
        sampled.append(_cliffs_delta(tp, sl))
    sampled.sort()
    return (
        round(sampled[int(0.025 * len(sampled))], 6),
        round(sampled[int(0.975 * len(sampled))], 6),
    )


def _permutation_p_value(
    tp_values: list[float], sl_values: list[float], rng: random.Random
) -> float | None:
    observed = abs(_cliffs_delta(tp_values, sl_values))
    if not math.isfinite(observed):
        return None
    combined = tp_values + sl_values
    tp_n = len(tp_values)
    exceed = 0
    for _ in range(PERMUTATION_ITERATIONS):
        shuffled = list(combined)
        rng.shuffle(shuffled)
        value = abs(_cliffs_delta(shuffled[:tp_n], shuffled[tp_n:]))
        if value >= observed - 1e-12:
            exceed += 1
    return round((exceed + 1) / (PERMUTATION_ITERATIONS + 1), 6)


def compare_feature(
    tp_rows: list[dict[str, Any]],
    sl_rows: list[dict[str, Any]],
    field: str,
    rng: random.Random,
) -> dict[str, Any]:
    tp_values = _finite_values(tp_rows, field)
    sl_values = _finite_values(sl_rows, field)
    tp_desc = describe(tp_rows, field)
    sl_desc = describe(sl_rows, field)
    result = {
        "feature": field,
        "label": FEATURE_META[field]["label"],
        "provenance": FEATURE_META[field]["provenance"],
        "description": FEATURE_META[field]["description"],
        "tp_first": tp_desc,
        "sl_first": sl_desc,
        "comparison_allowed": (
            len(tp_values) >= MIN_GROUP_N and len(sl_values) >= MIN_GROUP_N
        ),
        "median_diff_tp_minus_sl": (
            round(tp_desc["median"] - sl_desc["median"], 6)
            if tp_desc["median"] is not None and sl_desc["median"] is not None
            else None
        ),
        "cliffs_delta_tp_higher": None,
        "bootstrap_95ci": [None, None],
        "permutation_p_two_sided": None,
    }
    if result["comparison_allowed"]:
        result["cliffs_delta_tp_higher"] = round(
            _cliffs_delta(tp_values, sl_values), 6
        )
        low, high = _bootstrap_delta_ci(tp_values, sl_values, rng)
        result["bootstrap_95ci"] = [low, high]
        result["permutation_p_two_sided"] = _permutation_p_value(
            tp_values, sl_values, rng
        )
    return result


def _classification(
    rows: list[dict[str, Any]], field: str, threshold: float, direction: str
) -> dict[str, Any]:
    predicted_tp = lambda value: value >= threshold if direction == "gte" else value <= threshold
    matrix = {"tp_pred_tp": 0, "tp_pred_sl": 0, "sl_pred_tp": 0, "sl_pred_sl": 0}
    used = 0
    for row in rows:
        if row.get("outcome") not in ("tp", "sl"):
            continue
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        used += 1
        is_pred_tp = predicted_tp(value)
        if row["outcome"] == "tp":
            matrix["tp_pred_tp" if is_pred_tp else "tp_pred_sl"] += 1
        else:
            matrix["sl_pred_tp" if is_pred_tp else "sl_pred_sl"] += 1
    total = sum(matrix.values())
    correct = matrix["tp_pred_tp"] + matrix["sl_pred_sl"]
    tp_pred_total = matrix["tp_pred_tp"] + matrix["sl_pred_tp"]
    sl_pred_total = matrix["tp_pred_sl"] + matrix["sl_pred_sl"]
    tp_total = matrix["tp_pred_tp"] + matrix["tp_pred_sl"]
    sl_total = matrix["sl_pred_tp"] + matrix["sl_pred_sl"]
    accuracy = correct / total if total else None
    tp_precision = (
        matrix["tp_pred_tp"] / tp_pred_total if tp_pred_total else None
    )
    sl_precision = (
        matrix["sl_pred_sl"] / sl_pred_total if sl_pred_total else None
    )
    tp_recall = matrix["tp_pred_tp"] / tp_total if tp_total else None
    sl_recall = matrix["sl_pred_sl"] / sl_total if sl_total else None
    balanced_accuracy = (
        (
            matrix["tp_pred_tp"] / tp_total
            + matrix["sl_pred_sl"] / sl_total
        ) / 2.0
        if tp_total and sl_total else None
    )
    return {
        **matrix,
        "used_n": used,
        "accuracy": round(accuracy, 6) if accuracy is not None else None,
        "balanced_accuracy": (
            round(balanced_accuracy, 6)
            if balanced_accuracy is not None else None
        ),
        "precision_tp": round(tp_precision, 6) if tp_precision is not None else None,
        "precision_sl": round(sl_precision, 6) if sl_precision is not None else None,
        "tp_recall": round(tp_recall, 6) if tp_recall is not None else None,
        "sl_recall": round(sl_recall, 6) if sl_recall is not None else None,
    }


def find_candidate(
    rows: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    resolved_rows = [row for row in rows if row.get("outcome") in ("tp", "sl")]
    tp_total = sum(row["outcome"] == "tp" for row in resolved_rows)
    sl_total = sum(row["outcome"] == "sl" for row in resolved_rows)
    eligible = [
        item for item in comparisons
        if item["comparison_allowed"]
        and item["tp_first"]["n"] / tp_total >= 0.8
        and item["sl_first"]["n"] / sl_total >= 0.8
        and item["permutation_p_two_sided"] is not None
        and item["permutation_p_two_sided"] <= 0.05
        and item["bootstrap_95ci"][0] is not None
        and (
            item["bootstrap_95ci"][0] > 0.0
            or item["bootstrap_95ci"][1] < 0.0
        )
        and abs(item["cliffs_delta_tp_higher"] or 0.0) >= 0.33
    ]
    candidates = []
    for item in eligible:
        field = item["feature"]
        values = sorted({
            float(row[field]) for row in rows
            if row.get(field) not in (None, "")
            and math.isfinite(float(row[field]))
        })
        if len(values) < 2:
            continue
        thresholds = [
            (left + right) / 2.0
            for left, right in zip(values, values[1:])
        ]
        for threshold in thresholds:
            for direction in ("gte", "lte"):
                audit = _classification(rows, field, threshold, direction)
                if audit["used_n"] < MIN_GROUP_N * 2:
                    continue
                if audit["balanced_accuracy"] is None:
                    continue
                candidates.append({
                    "feature": field,
                    "label": item["label"],
                    "provenance": item["provenance"],
                    "rule": (
                        f"{item['label']} {'≥' if direction == 'gte' else '≤'} "
                        f"{threshold:.6g} predicts TP"
                    ),
                    "threshold": round(threshold, 6),
                    "operator": direction,
                    "criterion": {
                        "minimum_group_n": MIN_GROUP_N,
                        "minimum_abs_cliffs_delta": 0.33,
                        "maximum_permutation_p": 0.05,
                        "bootstrap_95ci_excludes_zero": True,
                        "minimum_balanced_accuracy": 0.60,
                        "minimum_feature_coverage_per_outcome": 0.80,
                    },
                    "feature_effect": item,
                    "classification_in_sample": audit,
                })
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item["classification_in_sample"]["balanced_accuracy"],
            item["classification_in_sample"]["accuracy"] or 0.0,
            abs(item["feature_effect"]["cliffs_delta_tp_higher"] or 0.0),
        ),
        reverse=True,
    )
    best = candidates[0]
    if best["classification_in_sample"]["balanced_accuracy"] < 0.60:
        return None
    return best


def _rule_matches(row: dict[str, Any], candidate: dict[str, Any]) -> bool:
    value = row.get(candidate["feature"])
    if value in (None, ""):
        return False
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    return (
        value >= candidate["threshold"]
        if candidate["operator"] == "gte"
        else value <= candidate["threshold"]
    )


def retrospective_candidate_audit(
    rows: list[dict[str, Any]], candidate: dict[str, Any] | None
) -> dict[str, Any]:
    """Measure selection volume on all signals and quality on resolved rows."""
    days = sorted({
        datetime.fromtimestamp(int(row["ts_open"]), timezone.utc).date().isoformat()
        for row in rows
    })
    selected = [row for row in rows if candidate and _rule_matches(row, candidate)]
    selected_resolved = [
        row for row in selected if row.get("outcome") in ("tp", "sl")
    ]
    baseline_resolved = [
        row for row in rows if row.get("outcome") in ("tp", "sl")
    ]
    selected_by_day = defaultdict(int)
    baseline_by_day = defaultdict(int)
    for row in rows:
        day = datetime.fromtimestamp(int(row["ts_open"]), timezone.utc).date().isoformat()
        baseline_by_day[day] += 1
    for row in selected:
        day = datetime.fromtimestamp(int(row["ts_open"]), timezone.utc).date().isoformat()
        selected_by_day[day] += 1
    day_count = len(days)
    return {
        "candidate_present": candidate is not None,
        "baseline": {
            "signals": len(rows),
            "resolved": len(baseline_resolved),
            "unresolved": len(rows) - len(baseline_resolved),
            "average_signals_per_day": round(len(rows) / day_count, 4) if day_count else None,
            "max_signals_per_day": max(baseline_by_day.values(), default=0),
        },
        "selected": {
            "signals": len(selected),
            "resolved": len(selected_resolved),
            "unresolved": len(selected) - len(selected_resolved),
            "coverage_pct": round(100.0 * len(selected) / len(rows), 4) if rows else 0.0,
            "average_signals_per_day": round(len(selected) / day_count, 4) if day_count else None,
            "max_signals_per_day": max(selected_by_day.values(), default=0),
            "daily_signal_counts": dict(sorted(selected_by_day.items())),
            "resolved_metrics": metrics(selected_resolved),
        },
        "days_observed": day_count,
        "daily_baseline_counts": dict(sorted(baseline_by_day.items())),
        "precision_recall": (
            candidate["classification_in_sample"] if candidate else None
        ),
    }


def feature_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report availability overall and by strategy/outcome, never imputing data."""
    result: dict[str, Any] = {}
    for field in FEATURE_META:
        if not (
            field.startswith((
                "price_return_", "range_", "realized_vol_", "volume_", "momentum_"
            ))
        ):
            continue
        available = [
            row for row in rows
            if row.get(field) not in (None, "")
        ]
        by_strategy: dict[str, Any] = {}
        for strategy in TARGET_STRATEGIES:
            strategy_rows = [
                row for row in rows if row.get("alert_type") == strategy
            ]
            by_outcome = {}
            for outcome in ("tp", "sl", "unresolved"):
                cohort = [
                    row for row in strategy_rows
                    if (
                        row.get("outcome") == outcome
                        if outcome in ("tp", "sl")
                        else row.get("outcome") not in ("tp", "sl")
                    )
                ]
                count = sum(row.get(field) not in (None, "") for row in cohort)
                by_outcome[outcome] = {
                    "n": len(cohort),
                    "available_n": count,
                    "coverage_pct": round(100.0 * count / len(cohort), 2)
                    if cohort else 0.0,
                }
            by_strategy[strategy] = by_outcome
        result[field] = {
            "provenance": FEATURE_META[field]["provenance"],
            "n": len(rows),
            "available_n": len(available),
            "coverage_pct": round(100.0 * len(available) / len(rows), 2)
            if rows else 0.0,
            "by_strategy_and_outcome": by_strategy,
        }
    return result


def build_report(
    rows: list[dict[str, Any]],
    events: dict,
    candles_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    position_counts: dict[str, dict[str, int]] | None = None,
    history_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enriched = enrich_rows(rows, events, candles_by_symbol)
    report: dict[str, Any] = {
        "config": {
            "strategies": list(TARGET_STRATEGIES),
            "scope": "all valid target shadow demo_positions; outcomes use recorded status",
            "outcomes": "tp/sl are resolved; open and ttl_expired remain unresolved",
            "r_definition": "directional exit-vs-entry divided by absolute entry-to-SL risk",
            "minimum_group_n": MIN_GROUP_N,
            "read_only": True,
            "historical_candle_interval": HISTORICAL_INTERVAL,
            "historical_candle_rule": "candle_open + 1h <= ts_open; gapped windows are unavailable",
            "in_sample_rule_warning": (
                "Any candidate is selected and evaluated on the same history; "
                "it is not forward evidence."
            ),
        },
        "coverage": {
            "rows_loaded": len(enriched),
            "log_exists": bool(events),
            "log_event_keys": len(events),
            "log_matches": sum(row.get("log_match_ts") is not None for row in enriched),
            "log_match_rate_pct": round(
                100.0 * sum(row.get("log_match_ts") is not None for row in enriched)
                / len(enriched), 2
            ) if enriched else 0.0,
            "last_signal_ts": max(
                (row["ts_open"] for row in enriched), default=None
            ),
            "last_signal_utc": fmt_ts(
                max((row["ts_open"] for row in enriched), default=None)
            ),
            "historical_candles": history_coverage or {
                "status": "not_requested",
                "symbols_with_history": len(candles_by_symbol or {}),
            },
        },
        "feature_provenance": FEATURE_META,
        "feature_coverage": feature_coverage(enriched),
        "position_counts": position_counts or {},
        "strategies": {},
        "rows": enriched,
    }
    numeric_fields = list(FEATURE_META)
    for strategy in TARGET_STRATEGIES:
        strategy_rows = [row for row in enriched if row["alert_type"] == strategy]
        directions = ("LONG", "SHORT")
        cohorts = {"overall": strategy_rows}
        cohorts.update({
            direction: [row for row in strategy_rows if row["direction"] == direction]
            for direction in directions
        })
        strategy_report = {}
        for cohort_name, cohort_rows in cohorts.items():
            tp_rows = [row for row in cohort_rows if row["outcome"] == "tp"]
            sl_rows = [row for row in cohort_rows if row["outcome"] == "sl"]
            stable_offset = sum(
                (index + 1) * ord(char)
                for index, char in enumerate(f"{strategy}:{cohort_name}")
            )
            rng = random.Random(RANDOM_SEED + stable_offset)
            comparisons = [
                compare_feature(tp_rows, sl_rows, field, rng)
                for field in numeric_fields
            ]
            strategy_report[cohort_name] = {
                "metrics": metrics(cohort_rows),
                "tp_first": len(tp_rows),
                "sl_first": len(sl_rows),
                "comparison_allowed": (
                    len(tp_rows) >= MIN_GROUP_N and len(sl_rows) >= MIN_GROUP_N
                ),
                "feature_comparisons": comparisons,
                "candidate": (
                    find_candidate(cohort_rows, comparisons)
                    if len(tp_rows) >= MIN_GROUP_N and len(sl_rows) >= MIN_GROUP_N
                    else None
                ),
            }
            strategy_report[cohort_name]["comparison_status"] = (
                "READY"
                if strategy_report[cohort_name]["comparison_allowed"]
                else "INSUFFICIENT_TP_OR_SL"
            )
            strategy_report[cohort_name]["comparison_reason"] = (
                "Both resolved outcome cohorts meet n>=minimum_group_n."
                if strategy_report[cohort_name]["comparison_allowed"]
                else (
                    f"Requires TP>= {MIN_GROUP_N} and SL>= {MIN_GROUP_N}; "
                    f"observed TP={len(tp_rows)}, SL={len(sl_rows)}."
                )
            )
            strategy_report[cohort_name]["retrospective"] = retrospective_candidate_audit(
                cohort_rows, strategy_report[cohort_name]["candidate"]
            )
        report["strategies"][strategy] = strategy_report
    return report


def _md_value(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_outputs(
    report: dict[str, Any], output_dir: Path, rows: list[dict[str, Any]]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    fields = [
        "id", "ts_open", "ts_open_utc", "symbol", "direction", "alert_type",
        "status", "outcome", "entry_price", "sl_price", "tp_price", "exit_price",
        "result_r", "risk_pct", "reward_pct", "reward_risk", "signal_price",
        "entry_vs_signal_pct", "log_match_ts", "log_match_delta_sec",
        "ema_gap_pct_log", "overheated_pct24_log", "overheated_rsi_log",
        "confirmation_volume_ratio_log", "confirmation_number_log",
        "confirmation_age_min_log", "confirmation_tp_mult_log",
        "historical_feature_status", "historical_last_candle_ts",
        "historical_last_candle_utc",
        *[
            field for field in FEATURE_META
            if field.startswith((
                "price_return_", "range_", "realized_vol_", "volume_", "momentum_"
            ))
        ],
    ]
    with (output_dir / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# TP vs SL — strong-signal experimental analysis",
        "",
        "**Read-only report. No production logic, filters, score, SL/TP, or SQLite rows were changed.**",
        "",
        f"- Scope: all valid target shadow `demo_positions` rows; loaded **{report['coverage']['rows_loaded']}**, "
        f"with **{sum(row.get('outcome') in ('tp', 'sl') for row in rows)}** resolved.",
        f"- Runtime log matches: **{report['coverage']['log_matches']}** "
        f"({report['coverage']['log_match_rate_pct']}%).",
        f"- Historical 1h candle coverage: **{report['coverage']['historical_candles'].get('symbols_with_history', 0)}** "
        f"of **{report['coverage']['historical_candles'].get('symbols_requested', 0)}** symbols.",
        f"- Minimum comparison cohort: **{MIN_GROUP_N} TP-first and {MIN_GROUP_N} SL-first**.",
        "- `WR` is TP / (TP + SL); `avg R` uses recorded exit price and original entry-to-SL risk.",
        "- Any rule below is in-sample: the threshold was selected and scored on the same rows.",
        "",
        "## Feature provenance",
        "",
        "| Field | Provenance | Coverage | Meaning |",
        "|---|---|---:|---|",
    ]
    for field, meta in FEATURE_META.items():
        count = sum(
            row.get(field) not in (None, "")
            for row in rows
        )
        coverage = 100.0 * count / len(rows) if rows else 0.0
        lines.append(
            f"| {meta['label']} (`{field}`) | {meta['provenance']} | "
            f"{coverage:.1f}% | {meta['description']} |"
        )
    lines += ["", "## Current strategy performance", ""]
    lines += [
        "| Strategy | Cohort | total | resolved | TP | SL | WR resolved | avg R | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for strategy, cohorts in report["strategies"].items():
        for cohort_name, item in cohorts.items():
            metric = item["metrics"]
            status = item["comparison_status"]
            lines.append(
                f"| {strategy} | {cohort_name} | {metric['total_n']} | {metric['n']} | "
                f"{metric['tp']} | "
                f"{metric['sl']} | {_md_value(metric['resolved_wr_pct'], 2)}% | "
                f"{_md_value(metric['avg_r'], 4)} | {status} |"
            )
    lines += ["", "## Direction summary across all strategies", ""]
    lines += [
        "| Strategy | Direction | total | resolved | TP | SL | WR resolved | avg R | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for strategy, cohorts in report["strategies"].items():
        for direction in ("LONG", "SHORT"):
            item = cohorts[direction]
            metric = item["metrics"]
            lines.append(
                f"| {strategy} | {direction} | {metric['total_n']} | {metric['n']} | "
                f"{metric['tp']} | {metric['sl']} | "
                f"{_md_value(metric['resolved_wr_pct'], 2)}% | "
                f"{_md_value(metric['avg_r'], 4)} | {item['comparison_status']} |"
            )
    lines += ["", "## Retrospective candidate volume and precision", ""]
    lines += [
        "| Strategy | Cohort | Candidate | Baseline/day | Selected/day | Selected signals | "
        "TP precision | TP recall | Selected WR |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy, cohorts in report["strategies"].items():
        for cohort_name, item in cohorts.items():
            audit = item["retrospective"]
            selected = audit["selected"]
            classification = audit["precision_recall"]
            candidate_label = (
                item["candidate"]["feature"] if item["candidate"] else "NO CANDIDATE"
            )
            lines.append(
                f"| {strategy} | {cohort_name} | {candidate_label} | "
                f"{_md_value(audit['baseline']['average_signals_per_day'], 2)} | "
                f"{_md_value(selected['average_signals_per_day'], 2)} | "
                f"{selected['signals']} | "
                f"{_md_value(classification.get('precision_tp') if classification else None, 3)} | "
                f"{_md_value(classification.get('tp_recall') if classification else None, 3)} | "
                f"{_md_value(selected['resolved_metrics']['resolved_wr_pct'], 2)}% |"
            )
    lines += ["", "## TP-first vs SL-first comparisons", ""]
    for strategy, cohorts in report["strategies"].items():
        lines += [f"### {strategy}", ""]
        for cohort_name, item in cohorts.items():
            lines += [f"#### {cohort_name}", ""]
            if not item["comparison_allowed"]:
                lines.append(
                    f"**{item['comparison_status']}:** {item['comparison_reason']} "
                    "No feature conclusion or candidate is allowed."
                )
                lines.append("")
                continue
            lines += [
                "| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |",
                "|---|---:|---:|---:|---:|---|---:|",
            ]
            for comparison in item["feature_comparisons"]:
                tp = comparison["tp_first"]
                sl = comparison["sl_first"]
                ci = comparison["bootstrap_95ci"]
                lines.append(
                    f"| {comparison['label']} [{comparison['provenance']}] | "
                    f"{_md_value(tp['median'])} ({tp['n']}) | "
                    f"{_md_value(sl['median'])} ({sl['n']}) | "
                    f"{_md_value(comparison['median_diff_tp_minus_sl'])} | "
                    f"{_md_value(comparison['cliffs_delta_tp_higher'])} | "
                    f"[{_md_value(ci[0])}, {_md_value(ci[1])}] | "
                    f"{_md_value(comparison['permutation_p_two_sided'], 4)} |"
                )
            candidate = item["candidate"]
            lines += ["", "#### Experimental candidate", ""]
            if candidate is None:
                lines.append(
                    "**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, "
                    "but no feature met the predeclared effect, permutation, "
                    "confidence-interval, coverage, and balanced-accuracy criteria."
                )
            else:
                audit = candidate["classification_in_sample"]
                lines += [
                    f"- **{candidate['rule']}**",
                    f"- Provenance: **{candidate['provenance']}**",
                    f"- In-sample accuracy: **{audit['accuracy']}**, balanced accuracy: "
                    f"**{audit['balanced_accuracy']}**",
                    f"- Precision TP: **{audit['precision_tp']}**; precision SL: "
                    f"**{audit['precision_sl']}**",
                    f"- TP recall: **{audit.get('tp_recall')}**; SL recall: "
                    f"**{audit.get('sl_recall')}**",
                    f"- Retrospective selected volume: **{item['retrospective']['selected']['signals']}** "
                    f"signals, **{item['retrospective']['selected']['average_signals_per_day']}**/day.",
                    "- This is experimental and requires forward-shadow validation; it is not a production rule.",
                ]
            lines.append("")
    lines += [
        "## Telegram marker decision",
        "",
        "No Telegram marker is enabled by this analysis. A marker may only be added "
        "behind an explicit default-off control after a candidate is deliberately accepted "
        "for forward-shadow testing; it must remain informational and cannot affect signal generation.",
        "",
        "## Guardrails",
        "",
        "- Runtime-log values are rounded at emission time and are not exact raw market snapshots.",
        "- Missing fields are left missing; no current ticker is substituted for historical signal-time data.",
        "- Statistical summaries are exploratory and subject to multiple-comparison bias.",
        "- No candidate is forward-validated by this report.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--log", type=Path, default=Path("bot_debug.log"))
    parser.add_argument(
        "--out", type=Path, default=Path("outcome_tp_vs_sl_experimental")
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_HISTORY_WORKERS,
        help="parallel historical candle requests (default: 3)",
    )
    args = parser.parse_args()
    rows = load_positions(args.db)
    events = parse_runtime_log(args.log)
    histories, history_coverage = fetch_historical_histories(
        rows, args.out, workers=args.workers
    )
    report = build_report(
        rows,
        events,
        histories,
        load_position_counts(args.db),
        history_coverage,
    )
    enriched = report.pop("rows")
    write_outputs(report, args.out, enriched)
    print(
        f"Wrote {args.out / 'report.md'}, {args.out / 'report.json'}, "
        f"and {args.out / 'rows.csv'} ({len(enriched)} rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())