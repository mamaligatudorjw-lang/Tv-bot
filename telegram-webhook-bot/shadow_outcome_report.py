#!/usr/bin/env python3
"""Reconstruct fixed-window outcomes for shadow demo positions.

This is deliberately separate from app.py and never changes trading state.
It uses Gate.io 15m futures candles, classifies first barrier touch, and
writes an auditable per-signal CSV plus Markdown/JSON summaries.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.gateio.ws/api/v4"
INTERVAL_SEC = 15 * 60
HOUR_SEC = 60 * 60
RSI_PERIOD = 14
TREND_WINDOW_H = 12
COOLDOWN_CONFLICT_SEC = 60 * 60
DEFAULT_WINDOW_H = 24
DEFAULT_RANGE_THRESHOLD = 50.0
MIN_GROUP_N = 20


def gate_symbol(symbol: str) -> str:
    return symbol[:-4] + "_USDT" if symbol.endswith("USDT") else symbol


def fetch_candles(
    session: requests.Session,
    symbol: str,
    start: int,
    end: int,
    interval: str = "15m",
    interval_sec: int = INTERVAL_SEC,
    retries: int = 3,
) -> list[dict[str, Any]]:
    # Gate.io rejects limit together with from/to and caps a time-range
    # response at roughly 1000 candles. Split long histories into safe chunks.
    chunk_seconds = 999 * interval_sec
    all_rows: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + chunk_seconds)
        params = {
            "contract": gate_symbol(symbol),
            "interval": interval,
            "from": cursor,
            "to": chunk_end,
        }
        for attempt in range(retries):
            try:
                response = session.get(
                    f"{BASE_URL}/futures/usdt/candlesticks",
                    params=params,
                    timeout=12,
                )
                if response.status_code == 400 or response.status_code == 404:
                    response.raise_for_status()
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                all_rows.extend(response.json())
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status not in (429,) and status < 500:
                    raise
                if attempt + 1 == retries:
                    raise
                time.sleep(1.0 * (attempt + 1))
            except Exception:
                if attempt + 1 == retries:
                    raise
                time.sleep(0.75 * (attempt + 1))
        cursor = chunk_end + 1
    unique = {int(row["t"]): row for row in all_rows}
    return [unique[ts] for ts in sorted(unique)]


def load_signals(db_path: Path, strategies: set[str] | None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
               status, ts_close, exit_price, alert_type, shadow_reason, rsi_at_signal
        FROM demo_positions
        WHERE is_shadow=1
          AND direction IN ('LONG', 'SHORT')
          AND entry_price > 0 AND sl_price > 0 AND tp_price > 0
    """
    args: list[Any] = []
    if strategies:
        placeholders = ",".join("?" for _ in strategies)
        query += f" AND alert_type IN ({placeholders})"
        args.extend(sorted(strategies))
    query += " ORDER BY ts_open, id"
    rows = [dict(row) for row in conn.execute(query, args)]
    conn.close()
    return rows


def load_direction_events(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load shadow and real signal timestamps for the conflict lookback."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT id, ts_open AS ts, symbol, direction, alert_type
          FROM demo_positions
         WHERE is_shadow=1 AND direction IN ('LONG', 'SHORT')
        UNION ALL
        SELECT -id AS id, ts, symbol, recommendation AS direction, alert_type
          FROM alerts
         WHERE recommendation IN ('LONG', 'SHORT')
        ORDER BY ts
        """
    ).fetchall()
    conn.close()
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event_id, ts, symbol, direction, alert_type in rows:
        events[symbol].append({
            "id": event_id,
            "ts": int(ts),
            "direction": direction,
            "alert_type": alert_type,
        })
    return events


def price_r(direction: str, entry: float, stop: float, price: float) -> float:
    """Economic R: +1R is one risk gained, -1R is the stop loss."""
    risk = abs(stop - entry)
    if risk <= 0:
        return float("nan")
    return (price - entry) / risk if direction == "LONG" else (entry - price) / risk


def simple_rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Match app.py's simple RSI calculation on the latest completed window."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(delta, 0.0) for delta in deltas[-period:]]
    losses = [max(-delta, 0.0) for delta in deltas[-period:]]
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    avg_gain = sum(gains) / period
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def hourly_features(
    candles: list[dict[str, Any]],
    ts_open: int,
) -> dict[str, Any]:
    """Reconstruct signal-time RSI and the latest hourly uptrend diagnostics."""
    completed = [
        candle for candle in candles
        if int(candle["t"]) + HOUR_SEC <= ts_open
    ]
    closes = [float(candle["c"]) for candle in completed]
    rsi = simple_rsi(closes[-(RSI_PERIOD + 1):])
    recent = completed[-(TREND_WINDOW_H + 1):]
    recent_closes = [float(candle["c"]) for candle in recent]
    up_count = (
        sum(recent_closes[i] > recent_closes[i - 1] for i in range(1, len(recent_closes)))
        if len(recent_closes) == TREND_WINDOW_H + 1 else None
    )
    streak = 0
    if len(closes) >= 2:
        for index in range(len(closes) - 1, 0, -1):
            if closes[index] > closes[index - 1]:
                streak += 1
            else:
                break
    if streak:
        start_index = len(completed) - 1 - streak
        trend_start_ts = int(completed[start_index]["t"])
        delay_hours = (ts_open - trend_start_ts) / HOUR_SEC
        if delay_hours < 4:
            delay_bucket = "0-3.9h"
        elif delay_hours < 8:
            delay_bucket = "4-7.9h"
        elif delay_hours < 12:
            delay_bucket = "8-11.9h"
        else:
            delay_bucket = "12h+"
    else:
        trend_start_ts = None
        delay_hours = None
        delay_bucket = "no_consecutive_trend"
    return {
        "rsi_1h": rsi,
        "rsi_bucket": (
            "missing" if rsi is None else "rsi_ge_80" if rsi >= 80 else "rsi_lt_80"
        ),
        "up_count_12": up_count,
        "trend_streak_candles": streak,
        "trend_start_ts": trend_start_ts,
        "trend_delay_hours": delay_hours,
        "trend_delay_bucket": delay_bucket,
    }


def apply_recorded_rsi(
    features: dict[str, Any],
    recorded_rsi: Any,
) -> dict[str, Any]:
    """Prefer the engine snapshot; retain reconstruction for legacy rows."""
    if recorded_rsi is None:
        features["rsi_source"] = "reconstructed"
        return features
    rsi = float(recorded_rsi)
    features["rsi_1h"] = rsi
    features["rsi_bucket"] = "rsi_ge_80" if rsi >= 80 else "rsi_lt_80"
    features["rsi_source"] = "engine_snapshot"
    return features


def first_touch(
    signal: dict[str, Any],
    candles: list[dict[str, Any]],
    end_ts: int,
) -> tuple[str, int | None, float, str]:
    direction = signal["direction"]
    entry = float(signal["entry_price"])
    stop = float(signal["sl_price"])
    target = float(signal["tp_price"])
    relevant = [
        c for c in candles
        if int(c["t"]) + INTERVAL_SEC > int(signal["ts_open"])
        and int(c["t"]) < end_ts
    ]
    last_close: float | None = None
    last_ts: int | None = None
    for candle in relevant:
        high = float(candle["h"])
        low = float(candle["l"])
        close = float(candle["c"])
        last_close, last_ts = close, int(candle["t"]) + INTERVAL_SEC
        if direction == "LONG":
            hit_tp = high >= target
            hit_sl = low <= stop
        else:
            hit_tp = low <= target
            hit_sl = high >= stop
        if hit_tp and hit_sl:
            # 15m OHLC cannot tell which barrier was touched first.
            return "ambiguous", last_ts, float("nan"), "both_barriers_same_candle"
        if hit_tp:
            return "tp_first", last_ts, price_r(direction, entry, stop, target), "target_touch"
        if hit_sl:
            return "sl_first", last_ts, price_r(direction, entry, stop, stop), "stop_touch"
    if last_close is None:
        return "missing_price", None, float("nan"), "no_candle_in_window"
    return "unresolved", last_ts, price_r(direction, entry, stop, last_close), "window_close"


def range_at_entry(candles: list[dict[str, Any]], ts_open: int) -> float | None:
    end = ts_open - (ts_open % INTERVAL_SEC)
    window_start = end - 24 * 3600
    completed = [
        c for c in candles
        if window_start <= int(c["t"]) and int(c["t"]) + INTERVAL_SEC <= ts_open
    ]
    if len(completed) < 60:
        return None
    high = max(float(c["h"]) for c in completed)
    low = min(float(c["l"]) for c in completed)
    return (high - low) / low * 100.0 if low > 0 else None


def has_opposite_signal(
    signal: dict[str, Any],
    by_symbol: dict[str, list[dict[str, Any]]],
) -> tuple[bool, int | None, str | None]:
    opposite = "SHORT" if signal["direction"] == "LONG" else "LONG"
    cutoff = int(signal["ts_open"]) - COOLDOWN_CONFLICT_SEC
    candidates = [
        other for other in by_symbol.get(signal["symbol"], [])
        if other["id"] != signal["id"]
        and other["direction"] == opposite
        and cutoff <= int(other["ts"]) < int(signal["ts_open"])
    ]
    if not candidates:
        return False, None, None
    other = max(candidates, key=lambda row: int(row["ts"]))
    return True, int(other["ts"]), other["alert_type"]


def fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in rows if r["outcome"] in ("tp_first", "sl_first")]
    tp = sum(r["outcome"] == "tp_first" for r in rows)
    sl = sum(r["outcome"] == "sl_first" for r in rows)
    unresolved = sum(r["outcome"] == "unresolved" for r in rows)
    ambiguous = sum(r["outcome"] == "ambiguous" for r in rows)
    rs = [float(r["r"]) for r in rows if math.isfinite(float(r["r"]))]
    return {
        "n": len(rows),
        "tp_first": tp,
        "sl_first": sl,
        "resolved": len(completed),
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "missing_price": sum(r["outcome"] == "missing_price" for r in rows),
        "win_rate_resolved_pct": round(100 * tp / len(completed), 2) if completed else None,
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
        "preliminary": len(rows) < MIN_GROUP_N,
    }


def split_report(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(row)
    return {name: metrics(items) for name, items in sorted(groups.items())}


def write_report(
    rows: list[dict[str, Any]],
    output_dir: Path,
    window_h: int,
    threshold: float,
    coverage: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "shadow_outcomes.csv"
    fields = [
        "id", "ts_open", "ts_open_utc", "symbol", "direction", "alert_type",
        "entry_price", "sl_price", "tp_price", "outcome", "exit_ts",
        "exit_ts_utc", "r", "reason", "opposite_within_60m",
        "opposite_ts", "opposite_alert_type", "range_24h_pct", "range_bucket",
        "status_existing", "ts_close_existing", "exit_price_existing",
        "rsi_1h", "rsi_bucket", "up_count_12", "trend_streak_candles",
        "trend_start_ts", "trend_delay_hours", "trend_delay_bucket", "rsi_source",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})

    eligible = [
        r for r in rows
        if r["outcome"] not in ("missing_price", "window_not_elapsed")
    ]
    short = [r for r in eligible if r["direction"] == "SHORT"]
    summary = {
        "config": {
            "strategy": "overheated_24h",
            "strategy_24h_threshold_pct": 15.0,
            "strategy_rsi_min": 70.0,
            "strategy_duration_window_hours": TREND_WINDOW_H,
            "strategy_duration_min_up": 0,
            "window_hours": window_h,
            "candle_interval": "15m",
            "opposite_window_minutes": 60,
            "range_threshold_pct": threshold,
            "min_group_n_preliminary": MIN_GROUP_N,
            "r_definition": "directional pnl divided by absolute entry-to-SL risk; TP=+2R target and SL=-1R where geometry is 2:1",
            "same_candle_policy": "ambiguous, excluded from resolved win rate and avg R",
        },
        "coverage": coverage,
        "overall": metrics(eligible),
        "by_strategy": split_report(eligible, "alert_type"),
        "short_only": {
            "overall": metrics(short),
            "opposite_within_60m": split_report(short, "opposite_group"),
            "range_24h": split_report(short, "range_bucket"),
        },
        "long_only": {
            "overall": metrics([r for r in eligible if r["direction"] == "LONG"]),
            "rsi_1h": split_report(
                [r for r in eligible if r["direction"] == "LONG" and r["rsi_bucket"] != "missing"],
                "rsi_bucket",
            ),
            "trend_delay": split_report(
                [r for r in eligible if r["direction"] == "LONG"],
                "trend_delay_bucket",
            ),
            "sustainability_up_count_12": split_report(
                [r for r in eligible if r["direction"] == "LONG" and r["up_count_12"] is not None],
                "up_count_12",
            ),
        },
    }
    (output_dir / "shadow_outcomes.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    def table(data: dict[str, dict[str, Any]]) -> str:
        lines = ["| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |",
                 "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for name, item in data.items():
            mark = " — preliminary (<20)" if item["preliminary"] else ""
            wr = "—" if item["win_rate_resolved_pct"] is None else f'{item["win_rate_resolved_pct"]:.1f}%'
            avg = "—" if item["avg_r"] is None else f'{item["avg_r"]:.3f}'
            lines.append(
                f'| {name}{mark} | {item["n"]} | {item["tp_first"]} | '
                f'{item["sl_first"]} | {item["unresolved"]} | {item["ambiguous"]} | {wr} | {avg} |'
            )
        return "\n".join(lines)

    md = [
        "# Shadow outcome report",
        "",
        f"Fixed window: **{window_h}h** after entry; source candles: **Gate.io futures 15m**.",
        "Only `demo_positions.is_shadow=1` signals with valid entry/SL/TP are included.",
        "Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.",
        "If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.",
        "`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.",
        "`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(coverage, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Overall",
        "",
        table({"all eligible": summary["overall"]}),
        "",
        "## By strategy",
        "",
        table(summary["by_strategy"]),
        "",
        "## SHORT: opposite-direction signal within previous 60 minutes",
        "",
        table(summary["short_only"]["opposite_within_60m"]),
        "",
        f"## SHORT: 24h range threshold {threshold:.1f}%",
        "",
        table(summary["short_only"]["range_24h"]),
        "",
        "## LONG: RSI at signal time",
        "",
        table(summary["long_only"]["rsi_1h"]),
        "",
        "## LONG: delay from detected consecutive hourly uptrend",
        "",
        table(summary["long_only"]["trend_delay"]),
        "",
        "## LONG: number of upward closes in the strategy's 12h window",
        "",
        table(summary["long_only"]["sustainability_up_count_12"]),
        "",
        "## Interpretation guardrails",
        "",
        "- Win rate is TP-first among resolved TP/SL outcomes only; unresolved and ambiguous are shown separately.",
        "- `avg R` includes unresolved signals at the last available price in the fixed window.",
        "- Groups with fewer than 20 signals are preliminary and are not a basis for changing filters.",
        "- For subgroups with n=5–6, one signal moves resolved WR by roughly 15–20 percentage points; these comparisons are directional only and are not a basis for setting a filter threshold.",
        "- This report does not change trading behavior or add filters.",
        "- RSI is reconstructed from the 14 latest completed 1h candles before the signal; the live/incomplete candle is excluded.",
        "- For rows created after the RSI snapshot migration, `rsi_source=engine_snapshot` is the exact RSI used by the gate. Legacy rows remain `rsi_source=reconstructed` and may differ near a threshold boundary.",
        "- Trend delay uses the consecutive up-close run ending at the last completed 1h candle. A missing run is reported as `no_consecutive_trend`, not assigned an artificial delay.",
        "- The current bot configuration has a 12h duration window but `min_up=0`; this report measures the observed up-close count and does not reinterpret it as an active gate.",
    ]
    (output_dir / "shadow_outcomes.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {csv_path}, {output_dir / 'shadow_outcomes.md'}, {output_dir / 'shadow_outcomes.json'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("alerts.db"))
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("shadow_outcome_report"))
    parser.add_argument("--window-hours", type=int, choices=(24, 48), default=DEFAULT_WINDOW_H)
    parser.add_argument("--range-threshold", type=float, default=DEFAULT_RANGE_THRESHOLD)
    parser.add_argument("--workers", type=int, default=4, help="bounded Gate.io fetch concurrency")
    parser.add_argument("--strategy", action="append", dest="strategies")
    args = parser.parse_args()

    signals = load_signals(args.db, set(args.strategies) if args.strategies else None)
    if not signals:
        print("No eligible shadow signals found.", file=sys.stderr)
        return 1
    now = int(time.time())
    max_end = min(now, max(int(row["ts_open"]) for row in signals) + args.window_hours * 3600)
    min_start = min(int(row["ts_open"]) for row in signals) - 24 * 3600
    max_signal_end = min(now, max(int(row["ts_open"]) for row in signals) + args.window_hours * 3600)
    signals_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        signals_by_symbol[signal["symbol"]].append(signal)
    by_symbol = load_direction_events(args.db)

    session = requests.Session()
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    hourly_by_symbol: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}
    need_hourly = any(signal["alert_type"] == "overheated_24h" for signal in signals)

    def fetch_one(
        symbol: str,
    ) -> tuple[
        str,
        list[dict[str, Any]] | None,
        list[dict[str, Any]] | None,
        str | None,
    ]:
        symbol_signals = signals_by_symbol[symbol]
        start = min(int(row["ts_open"]) for row in symbol_signals) - 24 * 3600
        end = min(now, max(int(row["ts_open"]) for row in symbol_signals) + args.window_hours * 3600)
        hourly_start = min(int(row["ts_open"]) for row in symbol_signals) - (RSI_PERIOD + TREND_WINDOW_H + 2) * HOUR_SEC
        try:
            fifteen = fetch_candles(session, symbol, start, end)
            hourly = (
                fetch_candles(
                    session, symbol, hourly_start, min(now, max(int(row["ts_open"]) for row in symbol_signals)),
                    interval="1h", interval_sec=HOUR_SEC,
                )
                if need_hourly else []
            )
            return symbol, fifteen, hourly, None
        except Exception as exc:
            return symbol, None, None, str(exc)

    symbols = sorted(signals_by_symbol)
    # Gate.io is slow for a few delisted contracts; bounded concurrency keeps
    # those from serially consuming the whole report run.
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(fetch_one, symbol) for symbol in symbols]
        for index, future in enumerate(as_completed(futures), 1):
            symbol, candles, hourly, error = future.result()
            if candles is not None:
                candles_by_symbol[symbol] = candles
            if hourly is not None:
                hourly_by_symbol[symbol] = hourly
            else:
                failures[symbol] = error or "unknown fetch error"
            if index % 25 == 0 or index == len(symbols):
                print(f"Fetched candles for {index}/{len(symbols)} symbols...", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for signal in signals:
        ts_open = int(signal["ts_open"])
        end_ts = ts_open + args.window_hours * 3600
        candles = candles_by_symbol.get(signal["symbol"], [])
        outcome, exit_ts, r_value, reason = first_touch(signal, candles, min(end_ts, now))
        # A window that has not elapsed is not silently called unresolved.
        if end_ts > now and outcome == "unresolved":
            outcome, reason = "window_not_elapsed", "analysis_run_before_window_end"
        opposite, opposite_ts, opposite_type = has_opposite_signal(signal, by_symbol)
        range_pct = range_at_entry(candles, ts_open)
        features = apply_recorded_rsi(
            hourly_features(hourly_by_symbol.get(signal["symbol"], []), ts_open),
            signal.get("rsi_at_signal"),
        )
        range_bucket = (
            "missing"
            if range_pct is None else
            f"below_{args.range_threshold:g}%"
            if range_pct < args.range_threshold else
            f"at_or_above_{args.range_threshold:g}%"
        )
        output = dict(signal)
        output.update({
            "ts_open_utc": fmt_ts(ts_open),
            "exit_ts": exit_ts,
            "exit_ts_utc": fmt_ts(exit_ts),
            "outcome": outcome,
            "r": r_value,
            "reason": reason,
            "opposite_within_60m": "yes" if opposite else "no",
            "opposite_group": "yes" if opposite else "no",
            "opposite_ts": opposite_ts or "",
            "opposite_alert_type": opposite_type or "",
            "range_24h_pct": "" if range_pct is None else round(range_pct, 4),
            "range_bucket": range_bucket,
            "status_existing": signal["status"],
            "ts_close_existing": signal["ts_close"] or "",
            "exit_price_existing": signal["exit_price"] or "",
            **features,
        })
        rows.append(output)

    eligible = [
        row for row in rows
        if row["outcome"] not in ("window_not_elapsed", "missing_price")
    ]
    coverage = {
        "signals_loaded": len(signals),
        "signals_reported": len(rows),
        "signals_eligible_for_fixed_window_metrics": len(eligible),
        "symbols_loaded": len(candles_by_symbol),
        "hourly_symbols_loaded": len(hourly_by_symbol) if need_hourly else None,
        "symbol_fetch_failures": failures,
        "candle_interval": "15m",
        "signal_min_utc": fmt_ts(min(int(row["ts_open"]) for row in signals)),
        "signal_max_utc": fmt_ts(max(int(row["ts_open"]) for row in signals)),
        "analysis_run_utc": datetime.now(timezone.utc).isoformat(),
        "window_not_elapsed": sum(row["outcome"] == "window_not_elapsed" for row in rows),
        "missing_price": sum(row["outcome"] == "missing_price" for row in rows),
        "range_missing": sum(row["range_24h_pct"] == "" for row in rows),
        "rsi_missing": sum(row["rsi_1h"] is None for row in rows),
        "rsi_engine_snapshot": sum(row["rsi_source"] == "engine_snapshot" for row in rows),
        "rsi_reconstructed_legacy": sum(row["rsi_source"] == "reconstructed" for row in rows),
        "trend_delay_missing": sum(row["trend_delay_hours"] is None for row in rows),
    }
    write_report(rows, args.out, args.window_hours, args.range_threshold, coverage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())