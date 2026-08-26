#!/usr/bin/env python3
"""Exploratory trailing-SL activation-threshold grid search.

The original fixed TP remains unchanged. Before activation, the original SL is
used and the trailing stop does not move. After activation, the same trailing
distance grid as the prior 5m analysis is tested.

This script uses the same saved selected-position cohort as the prior analysis
and refetches only the read-only Gate.io 5m path data. It never imports app.py
or mutates the trading database.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from shadow_outcome_report import fetch_candles
from trailing_stop_analysis import (
    DEFAULT_STEPS,
    RANGE_SEC,
    fixed_baseline,
    path_is_usable,
    price_r,
    range_pct,
)


ACTIVATION_THRESHOLDS: tuple[float | None, ...] = (None, 0.3, 0.5, 0.75, 1.0)
PATH_INTERVAL = "5m"
PATH_INTERVAL_SEC = 5 * 60


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def threshold_label(threshold: float | None) -> str:
    return "any_profit" if threshold is None else f"+{threshold:g}R"


def load_positions(
    db_path: Path,
    selected_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    ids = sorted({int(row["id"]) for row in selected_rows})
    if not ids:
        return []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ids)
    rows = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
                   status, ts_close, exit_price, alert_type, is_shadow
              FROM demo_positions
             WHERE id IN ({placeholders})
               AND direction IN ('LONG', 'SHORT')
               AND status IN ('tp', 'sl')
               AND ts_close IS NOT NULL
               AND entry_price > 0 AND sl_price > 0 AND tp_price > 0
            """,
            ids,
        )
    ]
    connection.close()
    by_id = {int(row["id"]): row for row in rows}
    return [
        by_id[int(row["id"])]
        for row in selected_rows
        if int(row["id"]) in by_id
    ]


def build_ranges(
    positions: list[dict[str, Any]],
) -> dict[str, tuple[int, int]]:
    ranges: dict[str, tuple[int, int]] = {}
    for row in positions:
        symbol = str(row["symbol"])
        start, end = ranges.get(
            symbol, (int(row["ts_open"]), int(row["ts_close"]))
        )
        ranges[symbol] = (
            min(start, int(row["ts_open"]) - RANGE_SEC),
            max(end, int(row["ts_close"]) + PATH_INTERVAL_SEC),
        )
    return ranges


def fetch_paths(
    ranges: dict[str, tuple[int, int]],
    workers: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    paths: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}

    def fetch_one(symbol: str, bounds: tuple[int, int]):
        import requests

        session = requests.Session()
        try:
            candles = fetch_candles(
                session,
                symbol,
                bounds[0],
                bounds[1],
                interval=PATH_INTERVAL,
                interval_sec=PATH_INTERVAL_SEC,
            )
            return symbol, candles, None
        except Exception as exc:
            return symbol, [], f"{type(exc).__name__}: {exc}"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(fetch_one, symbol, bounds): symbol
            for symbol, bounds in ranges.items()
        }
        for future in as_completed(futures):
            symbol, candles, error = future.result()
            if error:
                failures[symbol] = error
            else:
                paths[symbol] = candles
    return paths, failures


def simulate_activation(
    row: dict[str, Any],
    candles: list[dict[str, Any]],
    step_pct: float,
    activation_r: float | None,
) -> dict[str, Any]:
    entry = float(row["entry_price"])
    original_sl = float(row["sl_price"])
    target = float(row["tp_price"])
    direction = str(row["direction"])
    baseline_r, baseline_outcome = fixed_baseline(row)
    relevant, coverage_error = path_is_usable(
        candles, int(row["ts_open"]), int(row["ts_close"])
    )
    result: dict[str, Any] = {
        "id": row["id"],
        "strategy": row["alert_type"] or "unknown",
        "symbol": row["symbol"],
        "direction": direction,
        "is_shadow": row["is_shadow"],
        "activation_threshold": threshold_label(activation_r),
        "activation_r": "" if activation_r is None else activation_r,
        "step_pct": step_pct,
        "range_pct": range_pct(candles, int(row["ts_open"])),
        "baseline_r": baseline_r,
        "baseline_outcome": baseline_outcome,
        "alt_r": baseline_r,
        "alt_outcome": f"baseline_{baseline_outcome}",
        "trail_ts": "",
        "trail_price": "",
        "activated": False,
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
        favorable_move_r = (
            (high - entry) / abs(original_sl - entry)
            if direction == "LONG"
            else (entry - low) / abs(original_sl - entry)
        )

        # Existing barriers are evaluated before this candle can update the
        # stop. This preserves the prior conservative 5m convention.
        hit_stop = low <= stop if direction == "LONG" else high >= stop
        hit_target = high >= target if direction == "LONG" else low <= target
        if hit_stop or hit_target:
            if hit_stop:
                exit_price = stop
                outcome = "sl" if not activated else "trail_stop"
            else:
                exit_price = target
                outcome = "tp"
            result.update({
                "alt_r": price_r(direction, entry, original_sl, exit_price),
                "alt_outcome": outcome,
                "trail_ts": int(candle["t"]),
                "trail_price": exit_price,
                "activated": activated,
            })
            return result

        can_activate = (
            favorable_move_r > 0
            if activation_r is None
            else favorable_move_r >= activation_r
        )
        if can_activate:
            activated = True
            if direction == "LONG":
                favorable_extreme = max(favorable_extreme, high)
                stop = max(
                    stop, favorable_extreme * (1.0 - step_pct / 100.0)
                )
            else:
                favorable_extreme = min(favorable_extreme, low)
                stop = min(
                    stop, favorable_extreme * (1.0 + step_pct / 100.0)
                )

    result["trail_price"] = stop if activated else ""
    result["activated"] = activated
    return result


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row["coverage_error"]:
            groups[
                (row["strategy"], row["activation_threshold"], row["step_pct"])
            ].append(row)

    output = []
    for (strategy, threshold, step), items in sorted(groups.items()):
        baseline = [float(row["baseline_r"]) for row in items]
        alt = [float(row["alt_r"]) for row in items]
        baseline_tp_to_alt_loss = sum(
            float(row["baseline_r"]) >= 2.0 and float(row["alt_r"]) < 0
            for row in items
        )
        output.append({
            "strategy": strategy,
            "activation_threshold": threshold,
            "step_pct": step,
            "n": len(items),
            "baseline_total_r": round(sum(baseline), 6),
            "alt_total_r": round(sum(alt), 6),
            "delta_total_r": round(sum(alt) - sum(baseline), 6),
            "baseline_avg_r": round(mean(baseline), 6),
            "alt_avg_r": round(mean(alt), 6),
            "delta_avg_r": round(mean(alt) - mean(baseline), 6),
            "baseline_wr_pct": round(
                100 * sum(row["baseline_outcome"] == "tp" for row in items)
                / len(items),
                4,
            ),
            "alt_wr_pct": round(100 * sum(value > 0 for value in alt) / len(alt), 4),
            "alt_positive_n": sum(value > 0 for value in alt),
            "alt_negative_n": sum(value <= 0 for value in alt),
            "activated_n": sum(row["activated"] for row in items),
            "trail_exit_n": sum(
                row["alt_outcome"] == "trail_stop" for row in items
            ),
            "baseline_2r_to_alt_loss_n": baseline_tp_to_alt_loss,
        })
    return output


def previous_reference(
    previous_rows: list[dict[str, str]],
    previous_summary: list[dict[str, str]],
) -> list[dict[str, Any]]:
    best_steps = {}
    for strategy in sorted({row["strategy"] for row in previous_summary}):
        candidates = [
            row
            for row in previous_summary
            if row["strategy"] == strategy and row["range_bucket"] == "all"
        ]
        if candidates:
            best_steps[strategy] = max(
                candidates, key=lambda row: float(row["delta_total_r"])
            )["step_pct"]

    output = []
    for strategy, step in best_steps.items():
        items = [
            row
            for row in previous_rows
            if row["strategy"] == strategy
            and row["step_pct"] == step
            and not row["coverage_error"]
        ]
        output.append({
            "strategy": strategy,
            "previous_best_step_pct": step,
            "previous_n": len(items),
            "previous_baseline_2r_to_alt_loss_n": sum(
                float(row["baseline_r"]) >= 2.0 and float(row["alt_r"]) < 0
                for row in items
            ),
        })
    return output


def write_report(
    output_dir: Path,
    summaries: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> None:
    priority = {"overheated_24h", "ema_cross_confirmed"}
    ordered = sorted(
        summaries,
        key=lambda row: (
            0 if row["strategy"] in priority else 1,
            row["strategy"],
            row["activation_threshold"],
            float(row["step_pct"]),
        ),
    )
    lines = [
        "# Exploratory trailing-SL activation-threshold grid",
        "",
        "**Read-only. Production logic and the trading database were not changed.**",
        "",
        "TP remains fixed. Before activation, the original SL remains in force and "
        "the trailing stop does not move. After activation, the favorable high/low "
        "is trailed by the tested step. The `any_profit` row is the prior model "
        "without a positive-R activation threshold.",
        "",
        "## Summary",
        "",
        "| Strategy | Activation | Step | n | Baseline total R | Alt total R | Δ total R | Baseline avg R | Alt avg R | Baseline WR | Alt WR | Activated | Trail exits | +2R baseline → alt loss |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in ordered:
        lines.append(
            f"| {row['strategy']} | {row['activation_threshold']} | "
            f"{row['step_pct']}% | {row['n']} | {row['baseline_total_r']} | "
            f"{row['alt_total_r']} | {row['delta_total_r']} | "
            f"{row['baseline_avg_r']} | {row['alt_avg_r']} | "
            f"{row['baseline_wr_pct']}% | {row['alt_wr_pct']}% | "
            f"{row['activated_n']} | {row['trail_exit_n']} | "
            f"{row['baseline_2r_to_alt_loss_n']} |"
        )
    lines += [
        "",
        "## Previous-test reference",
        "",
        "The prior comparison count is computed from the saved 5m output at each "
        "strategy's prior best step. It is not reused as a new outcome.",
        "",
        "| Strategy | Prior best step | n | Prior +2R baseline → alt loss |",
        "|---|---:|---:|---:|",
    ]
    for row in previous:
        lines.append(
            f"| {row['strategy']} | {row['previous_best_step_pct']}% | "
            f"{row['previous_n']} | "
            f"{row['previous_baseline_2r_to_alt_loss_n']} |"
        )
    lines += [
        "",
        "Across the two priority strategies, the saved prior reference count is "
        f"{sum(row['previous_baseline_2r_to_alt_loss_n'] for row in previous if row['strategy'] in priority)}; "
        "across all selected strategies it is "
        f"{sum(row['previous_baseline_2r_to_alt_loss_n'] for row in previous)}.",
        "",
        "## Interpretation",
        "",
        "The activation threshold is useful only if it reduces the "
        "`+2R baseline → alt loss` failures without removing too many cases where "
        "trailing rescues a baseline SL. This table reports both the failure count "
        "and total/average R so the trade-off is explicit.",
        "",
        "This remains an **exploratory in-sample/grid-search result**. Testing five "
        "activation thresholds and seven trailing steps creates selection bias. "
        "Even a favorable combination must be frozen and checked on a separate "
        "out-of-sample time period before any production decision.",
        "",
        "```json",
        json.dumps(coverage, indent=2, sort_keys=True),
        "```",
    ]
    (output_dir / "activation_analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--input", type=Path, default=Path("outcome_trailing_stop"))
    parser.add_argument("--out", type=Path, default=Path("outcome_trailing_activation"))
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    previous_rows = read_csv(args.input / "trailing_rows.csv")
    previous_summary = read_csv(args.input / "summary.csv")
    selected_ids = {}
    for row in previous_rows:
        if row["step_pct"] == "2.0":
            selected_ids[int(row["id"])] = row
    positions = load_positions(args.db, list(selected_ids.values()))
    ranges = build_ranges(positions)
    paths, failures = fetch_paths(ranges, args.workers)

    simulated: list[dict[str, Any]] = []
    for position in positions:
        symbol = str(position["symbol"])
        for threshold in ACTIVATION_THRESHOLDS:
            for step in DEFAULT_STEPS:
                result = simulate_activation(
                    position,
                    paths.get(symbol, []),
                    float(step),
                    threshold,
                )
                if symbol in failures:
                    result["coverage_error"] = failures[symbol]
                simulated.append(result)

    summaries = summarize(simulated)
    previous = previous_reference(previous_rows, previous_summary)
    coverage = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "path_interval": PATH_INTERVAL,
        "activation_thresholds_r": [
            "any_profit", 0.3, 0.5, 0.75, 1.0
        ],
        "steps_pct": list(DEFAULT_STEPS),
        "positions_from_previous_cohort": len(positions),
        "rows_simulated": len(simulated),
        "rows_with_coverage_error": sum(
            bool(row["coverage_error"]) for row in simulated
        ),
        "symbols_requested": len(ranges),
        "symbols_loaded": len(paths),
        "symbol_fetch_failures": failures,
        "previous_grid_generated_utc": json.loads(
            (args.input / "coverage.json").read_text(encoding="utf-8")
        ).get("generated_utc"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "activation_rows.csv", simulated)
    write_csv(args.out / "activation_summary.csv", summaries)
    write_csv(args.out / "previous_reference.csv", previous)
    write_report(args.out, summaries, previous, coverage)
    print(json.dumps({
        "positions": len(positions),
        "symbols_loaded": len(paths),
        "rows_with_coverage_error": coverage["rows_with_coverage_error"],
        "output": str(args.out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())