#!/usr/bin/env python3
"""Exploratory trailing-stop grid search on resolved demo positions.

Read-only analysis.  It never imports app.py or mutates the trading database.
The path simulation uses completed 5m Gate.io futures candles.  The original
fixed TP/SL outcome is the baseline; a trailing stop is allowed to exit before
that baseline exit, otherwise the baseline result is retained.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shadow_outcome_report import fetch_candles

PATH_INTERVAL = "5m"
PATH_INTERVAL_SEC = 5 * 60
RANGE_INTERVAL = "5m"
RANGE_INTERVAL_SEC = 5 * 60
RANGE_SEC = 24 * 60 * 60
DEFAULT_STEPS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def price_r(direction: str, entry: float, stop: float, price: float) -> float:
    risk = abs(stop - entry)
    if risk <= 0:
        return float("nan")
    return (price - entry) / risk if direction == "LONG" else (entry - price) / risk


def load_resolved(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
                   status, ts_close, exit_price, alert_type, is_shadow
              FROM demo_positions
             WHERE direction IN ('LONG', 'SHORT')
               AND status IN ('tp', 'sl')
               AND ts_close IS NOT NULL
               AND entry_price > 0 AND sl_price > 0 AND tp_price > 0
               AND symbol <> 'CASHCATUSDT'
             ORDER BY ts_open, id
            """
        )
    ]
    conn.close()
    return rows


def strategy_ranking(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["alert_type"] or "unknown")].append(row)

    ranking: list[dict[str, Any]] = []
    selected: set[str] = set()
    for strategy, items in grouped.items():
        rs = [
            price_r(
                row["direction"],
                float(row["entry_price"]),
                float(row["sl_price"]),
                float(row["exit_price"]),
            )
            for row in items
            if row["exit_price"] is not None
        ]
        rs = [value for value in rs if math.isfinite(value)]
        tp = sum(row["status"] == "tp" for row in items)
        sl = sum(row["status"] == "sl" for row in items)
        n = tp + sl
        avg_r = sum(rs) / len(rs) if rs else float("nan")
        wr = tp / n if n else float("nan")
        keep = n >= 20 and avg_r > 0
        if keep:
            selected.add(strategy)
        ranking.append({
            "strategy": strategy,
            "n_resolved": n,
            "tp": tp,
            "sl": sl,
            "wr_pct": round(wr * 100, 4) if math.isfinite(wr) else "",
            "avg_r_observed": round(avg_r, 6) if math.isfinite(avg_r) else "",
            "selected": "yes" if keep else "no",
            "selection_rule": "n_resolved>=20 and avg_r_observed>0",
        })
    ranking.sort(
        key=lambda row: (
            -(row["avg_r_observed"] if row["avg_r_observed"] != "" else -math.inf),
            -row["n_resolved"],
            row["strategy"],
        )
    )
    return ranking, selected


def range_pct(candles: list[dict[str, Any]], ts_open: int) -> float | None:
    completed = [
        candle
        for candle in candles
        if ts_open - RANGE_SEC <= int(candle["t"])
        and int(candle["t"]) + RANGE_INTERVAL_SEC <= ts_open
    ]
    if len(completed) < 12:
        return None
    high = max(float(candle["h"]) for candle in completed)
    low = min(float(candle["l"]) for candle in completed)
    return (high - low) / low * 100.0 if low > 0 else None


def fixed_baseline(row: dict[str, Any]) -> tuple[float, str]:
    if row["status"] == "tp":
        return price_r(
            row["direction"],
            float(row["entry_price"]),
            float(row["sl_price"]),
            float(row["tp_price"]),
        ), "tp"
    return price_r(
        row["direction"],
        float(row["entry_price"]),
        float(row["sl_price"]),
        float(row["sl_price"]),
    ), "sl"


def path_is_usable(
    candles: list[dict[str, Any]], ts_open: int, ts_close: int
) -> tuple[list[dict[str, Any]], str | None]:
    first = (
        (ts_open + PATH_INTERVAL_SEC - 1) // PATH_INTERVAL_SEC
    ) * PATH_INTERVAL_SEC
    relevant = [
        candle
        for candle in candles
        if first <= int(candle["t"]) < ts_close
    ]
    if not relevant and ts_close - ts_open <= PATH_INTERVAL_SEC:
        # The position ended before a completed path candle could exist. No
        # completed-candle trailing update was possible; retain the baseline.
        return [], None
    if not relevant:
        return [], "no_5m_candles_in_position_window"
    starts = [int(candle["t"]) for candle in relevant]
    if any(
        right - left > PATH_INTERVAL_SEC
        for left, right in zip(starts, starts[1:])
    ):
        return relevant, "5m_candle_gap_in_position_window"
    return relevant, None


def simulate(
    row: dict[str, Any],
    path_candles: list[dict[str, Any]],
    range_candles: list[dict[str, Any]],
    step_pct: float,
) -> dict[str, Any]:
    entry = float(row["entry_price"])
    original_sl = float(row["sl_price"])
    target = float(row["tp_price"])
    direction = row["direction"]
    baseline_r, baseline_outcome = fixed_baseline(row)
    relevant, coverage_error = path_is_usable(
        path_candles, int(row["ts_open"]), int(row["ts_close"])
    )
    result = {
        "id": row["id"],
        "strategy": row["alert_type"] or "unknown",
        "symbol": row["symbol"],
        "direction": direction,
        "is_shadow": row["is_shadow"],
        "step_pct": step_pct,
        "range_pct": range_pct(range_candles, int(row["ts_open"])),
        "baseline_r": baseline_r,
        "baseline_outcome": baseline_outcome,
        "alt_r": baseline_r,
        "alt_outcome": f"baseline_{baseline_outcome}",
        "trail_ts": "",
        "trail_price": "",
        "coverage_error": coverage_error or "",
    }
    if coverage_error:
        return result

    stop = original_sl
    favorable_extreme = entry
    activated = False
    for candle in relevant:
        high = float(candle["h"])
        low = float(candle["l"])

        # Conservative OHLC convention: an existing stop wins when both
        # barriers appear in one candle.  A stop newly computed from this
        # candle's extreme is only active on the next candle.
        hit_stop = low <= stop if direction == "LONG" else high >= stop
        hit_target = high >= target if direction == "LONG" else low <= target
        if hit_stop or hit_target:
            if hit_stop:
                exit_price = stop
                outcome = "sl" if stop == original_sl else "trail_stop"
            else:
                exit_price = target
                outcome = "tp"
            result.update({
                "alt_r": price_r(direction, entry, original_sl, exit_price),
                "alt_outcome": outcome,
                "trail_ts": int(candle["t"]),
                "trail_price": exit_price,
            })
            return result

        if direction == "LONG" and high > entry:
            activated = True
            favorable_extreme = max(favorable_extreme, high)
            stop = max(stop, favorable_extreme * (1.0 - step_pct / 100.0))
        elif direction == "SHORT" and low < entry:
            activated = True
            favorable_extreme = min(favorable_extreme, low)
            stop = min(stop, favorable_extreme * (1.0 + step_pct / 100.0))

    # No trailing exit before the already-observed fixed TP/SL exit.
    result["trail_price"] = stop if activated else ""
    return result


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def add_range_buckets(rows: list[dict[str, Any]]) -> tuple[float, float]:
    values = sorted(
        float(row["range_pct"])
        for row in rows
        if row["range_pct"] is not None
    )
    q33 = quantile(values, 1 / 3)
    q67 = quantile(values, 2 / 3)
    for row in rows:
        value = row["range_pct"]
        if value is None:
            row["range_bucket"] = "missing"
        elif value <= q33:
            row["range_bucket"] = "low"
        elif value <= q67:
            row["range_bucket"] = "mid"
        else:
            row["range_bucket"] = "high"
    return q33, q67


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, float, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["coverage_error"]:
            continue
        groups[(row["strategy"], row["step_pct"], "all")].append(row)
        if row["range_bucket"] != "missing":
            groups[(row["strategy"], row["step_pct"], row["range_bucket"])].append(row)

    output: list[dict[str, Any]] = []
    for (strategy, step_pct, bucket), items in sorted(groups.items()):
        baseline = [float(row["baseline_r"]) for row in items]
        alt = [float(row["alt_r"]) for row in items]
        delta = [a - b for a, b in zip(alt, baseline)]
        output.append({
            "strategy": strategy,
            "step_pct": step_pct,
            "range_bucket": bucket,
            "n": len(items),
            "baseline_total_r": round(sum(baseline), 6),
            "alt_total_r": round(sum(alt), 6),
            "delta_total_r": round(sum(delta), 6),
            "baseline_avg_r": round(sum(baseline) / len(baseline), 6),
            "alt_avg_r": round(sum(alt) / len(alt), 6),
            "delta_avg_r": round(sum(delta) / len(delta), 6),
            "baseline_wr_pct": round(
                100 * sum(row["baseline_outcome"] == "tp" for row in items) / len(items), 4
            ),
            "alt_wr_pct": round(100 * sum(value > 0 for value in alt) / len(alt), 4),
            "alt_positive_n": sum(value > 0 for value in alt),
            "alt_negative_n": sum(value <= 0 for value in alt),
            "trail_exit_n": sum(
                row["alt_outcome"] == "trail_stop" for row in items
            ),
        })
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    output_dir: Path,
    ranking: list[dict[str, Any]],
    selected: set[str],
    summaries: list[dict[str, Any]],
    coverage: dict[str, Any],
    q33: float,
    q67: float,
) -> None:
    lines = [
        "# Exploratory trailing-stop grid search",
        "",
        "**Read-only analysis. Production logic and database were not changed.**",
        "",
        "The grid uses completed Gate.io 5m futures candles. The initial stop is the",
        "recorded original SL. Once price trades in the profitable direction, the",
        "stop follows the favorable high/low by the tested percentage. If no trailing",
        "exit occurs before the observed fixed TP/SL exit, the baseline result is kept.",
        "When one OHLC candle contains both barriers, stop-first is used conservatively;",
        "a newly calculated trailing stop starts on the next candle.",
        "",
        "This is an **exploratory/grid-search result**, not an out-of-sample test and",
        "not a basis for production changes.",
        "",
        f"Range buckets use 5m-derived preceding 24h range: low ≤ {q33:.4f}%, "
        f"mid ≤ {q67:.4f}%, high > {q67:.4f}%.",
        "",
        "## Strategy selection",
        "",
        "Selected strategies: " + ", ".join(sorted(selected)) if selected else "None selected.",
        "",
        "| Strategy | n resolved | WR | avg R | Selected |",
        "|---|---:|---:|---:|---|",
    ]
    for row in ranking:
        lines.append(
            f"| {row['strategy']} | {row['n_resolved']} | "
            f"{row['wr_pct']}% | {row['avg_r_observed']} | {row['selected']} |"
        )
    lines += [
        "",
        "## Grid summary",
        "",
        "| Strategy | Step | Range | n | Baseline total R | Alt total R | Δ total R | "
        "Baseline avg R | Alt avg R | Baseline WR | Alt WR | Trail exits |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['strategy']} | {row['step_pct']}% | {row['range_bucket']} | "
            f"{row['n']} | {row['baseline_total_r']} | {row['alt_total_r']} | "
            f"{row['delta_total_r']} | {row['baseline_avg_r']} | {row['alt_avg_r']} | "
            f"{row['baseline_wr_pct']}% | {row['alt_wr_pct']}% | {row['trail_exit_n']} |"
        )
    lines += [
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(coverage, indent=2, sort_keys=True),
        "```",
        "",
        "Interpretation must account for selection bias, path ambiguity within 5m",
        "candles, fees/slippage, and the lack of a separate out-of-sample period.",
    ]
    (output_dir / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--out", type=Path, default=Path("outcome_trailing_stop"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--steps",
        type=float,
        nargs="+",
        default=list(DEFAULT_STEPS),
        help="Trailing distance percentages.",
    )
    args = parser.parse_args()

    rows = load_resolved(args.db)
    ranking, selected = strategy_ranking(rows)
    selected_rows = [row for row in rows if (row["alert_type"] or "unknown") in selected]
    if not selected_rows:
        raise SystemExit("No strategies met n_resolved>=20 and positive observed avg R")

    path_ranges: dict[str, tuple[int, int]] = {}
    range_ranges: dict[str, tuple[int, int]] = {}
    for row in selected_rows:
        symbol = str(row["symbol"])
        path_start, path_end = path_ranges.get(
            symbol, (int(row["ts_open"]), int(row["ts_close"]))
        )
        range_start, range_end = range_ranges.get(
            symbol, (int(row["ts_open"]), int(row["ts_open"]))
        )
        path_ranges[symbol] = (
            min(path_start, int(row["ts_open"]) - RANGE_SEC),
            max(path_end, int(row["ts_close"]) + PATH_INTERVAL_SEC),
        )
        range_ranges[symbol] = (
            min(range_start, int(row["ts_open"]) - RANGE_SEC),
            max(range_end, int(row["ts_open"]) + RANGE_INTERVAL_SEC),
        )

    path_candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    range_candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}

    def fetch_one(
        symbol: str,
        path_bounds: tuple[int, int],
        range_bounds: tuple[int, int],
    ):
        import requests

        session = requests.Session()
        try:
            path_candles = fetch_candles(
                session, symbol, path_bounds[0], path_bounds[1],
                interval=PATH_INTERVAL, interval_sec=PATH_INTERVAL_SEC,
            )
            # The default path and range intervals are both 5m, and the path
            # request deliberately includes the preceding 24h. Reuse it to
            # avoid a second identical historical request per symbol.
            range_candles = (
                path_candles
                if PATH_INTERVAL == RANGE_INTERVAL
                and path_bounds[0] <= range_bounds[0]
                and path_bounds[1] >= range_bounds[1]
                else fetch_candles(
                    session, symbol, range_bounds[0], range_bounds[1],
                    interval=RANGE_INTERVAL, interval_sec=RANGE_INTERVAL_SEC,
                )
            )
            return symbol, path_candles, range_candles, None
        except Exception as exc:  # keep one symbol failure from hiding coverage
            return symbol, [], [], f"{type(exc).__name__}: {exc}"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                fetch_one, symbol, path_ranges[symbol], range_ranges[symbol]
            ): symbol
            for symbol in path_ranges
        }
        for future in as_completed(futures):
            symbol, path_candles, range_candles, error = future.result()
            if error:
                failures[symbol] = error
            else:
                path_candles_by_symbol[symbol] = path_candles
                range_candles_by_symbol[symbol] = range_candles
            time.sleep(0.05)

    simulated: list[dict[str, Any]] = []
    for row in selected_rows:
        path_candles = path_candles_by_symbol.get(str(row["symbol"]), [])
        range_candles = range_candles_by_symbol.get(str(row["symbol"]), [])
        for step_pct in args.steps:
            simulated.append(
                simulate(row, path_candles, range_candles, float(step_pct))
            )
    q33, q67 = add_range_buckets(simulated)
    summaries = summarize(simulated)

    output_dir: Path = args.out
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "strategy_ranking.csv", ranking)
    write_csv(output_dir / "trailing_rows.csv", simulated)
    write_csv(output_dir / "summary.csv", summaries)
    coverage = {
        "resolved_rows_all_strategies": len(rows),
        "selected_strategies": sorted(selected),
        "selected_resolved_rows": len(selected_rows),
        "steps_pct": [float(step) for step in args.steps],
        "path_candle_interval": PATH_INTERVAL,
        "range_candle_interval": RANGE_INTERVAL,
        "symbols_requested": len(path_ranges),
        "symbols_loaded": len(path_candles_by_symbol),
        "symbol_fetch_failures": failures,
        "rows_simulated": len(simulated),
        "rows_with_path_coverage_error": sum(
            bool(row["coverage_error"]) for row in simulated
        ),
        "range_missing_rows": sum(row["range_pct"] is None for row in simulated),
        "range_q33_pct": q33,
        "range_q67_pct": q67,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir, ranking, selected, summaries, coverage, q33, q67)
    print(json.dumps({
        "selected_strategies": sorted(selected),
        "selected_resolved_rows": len(selected_rows),
        "symbols_loaded": len(path_candles_by_symbol),
        "symbols_failed": len(failures),
        "rows_with_path_coverage_error": coverage["rows_with_path_coverage_error"],
        "output": str(output_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())