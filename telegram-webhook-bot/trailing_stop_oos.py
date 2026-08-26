#!/usr/bin/env python3
"""Fixed-step forward validation for the exploratory trailing-stop analysis.

The steps are intentionally hard-coded from the prior in-sample selection:
overheated_24h=8% and ema_cross_confirmed=6%. No grid search or re-selection is
performed here. The script also exports the in-sample outlier trades.

This is read-only: it does not import app.py or mutate the trading database.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from shadow_outcome_report import fetch_candles
from trailing_stop_analysis import RANGE_SEC, simulate


FIXED_STEPS = {
    "overheated_24h": 8.0,
    "ema_cross_confirmed": 6.0,
}
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


def parse_cutoff(coverage: dict[str, Any], explicit: str | None) -> int:
    if explicit:
        return int(datetime.fromisoformat(explicit.replace("Z", "+00:00")).timestamp())
    generated = coverage.get("generated_utc")
    if not generated:
        raise SystemExit("coverage.json has no generated_utc; pass --cutoff explicitly")
    return int(datetime.fromisoformat(generated).timestamp())


def fmt_ts(timestamp: int | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def load_forward_positions(db_path: Path, cutoff: int) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
                   status, ts_close, exit_price, alert_type, is_shadow
              FROM demo_positions
             WHERE alert_type IN (?, ?)
               AND direction IN ('LONG', 'SHORT')
               AND ts_open > ?
               AND status IN ('tp', 'sl')
               AND ts_close IS NOT NULL
               AND entry_price > 0 AND sl_price > 0 AND tp_price > 0
               AND symbol <> 'CASHCATUSDT'
             ORDER BY ts_open, id
            """,
            (*FIXED_STEPS, cutoff),
        )
    ]
    connection.close()
    return rows


def outliers(
    path_rows: list[dict[str, str]],
    strategy: str,
    step: float,
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in path_rows
        if row["strategy"] == strategy
        and float(row["step_pct"]) == step
        and not row["coverage_error"]
    ]
    enriched = []
    for row in rows:
        baseline = float(row["baseline_r"])
        alt = float(row["alt_r"])
        enriched.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "direction": row["direction"],
            "step_pct": row["step_pct"],
            "baseline_r": round(baseline, 6),
            "alt_r": round(alt, 6),
            "delta_r": round(alt - baseline, 6),
            "alt_outcome": row["alt_outcome"],
            "trail_ts": row["trail_ts"],
        })
    return sorted(enriched, key=lambda row: row["delta_r"], reverse=True)


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


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for strategy, step in FIXED_STEPS.items():
        selected = [row for row in rows if row["strategy"] == strategy]
        usable = [row for row in selected if not row["coverage_error"]]
        baseline = [float(row["baseline_r"]) for row in usable]
        alt = [float(row["alt_r"]) for row in usable]
        output.append({
            "strategy": strategy,
            "fixed_step_pct": step,
            "n_forward_resolved": len(selected),
            "n_path_usable": len(usable),
            "n_path_coverage_error": len(selected) - len(usable),
            "baseline_total_r": round(sum(baseline), 6) if baseline else "",
            "alt_total_r": round(sum(alt), 6) if alt else "",
            "delta_total_r": round(sum(alt) - sum(baseline), 6) if baseline else "",
            "baseline_avg_r": round(mean(baseline), 6) if baseline else "",
            "alt_avg_r": round(mean(alt), 6) if alt else "",
            "delta_avg_r": (
                round(mean(alt) - mean(baseline), 6) if baseline else ""
            ),
            "baseline_wr_pct": (
                round(100 * sum(row["baseline_outcome"] == "tp" for row in usable) / len(usable), 4)
                if usable else ""
            ),
            "alt_positive_n": sum(value > 0 for value in alt),
            "alt_negative_n": sum(value <= 0 for value in alt),
            "trail_exit_n": sum(row["alt_outcome"] == "trail_stop" for row in usable),
        })
    return output


def write_report(
    output_dir: Path,
    cutoff: int,
    current_utc: str,
    summaries: list[dict[str, Any]],
    outlier_rows: dict[str, list[dict[str, Any]]],
    coverage: dict[str, Any],
) -> None:
    lines = [
        "# Fixed-step forward validation",
        "",
        "**Read-only. Production logic and the trading database were not changed.**",
        "",
        f"Grid cutoff: **{fmt_ts(cutoff)}**. Test generated: **{current_utc}**.",
        "",
        "The trailing steps were fixed before this forward slice: "
        "`overheated_24h=8%`, `ema_cross_confirmed=6%`. No grid search or "
        "step re-selection was performed.",
        "",
        "## Available forward results",
        "",
        "| Strategy | Fixed step | n resolved | Path usable | Baseline total R | Alt total R | Δ total R | Δ avg R | Baseline WR | Alt positive n |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['strategy']} | {row['fixed_step_pct']}% | "
            f"{row['n_forward_resolved']} | {row['n_path_usable']} | "
            f"{row['baseline_total_r']} | {row['alt_total_r']} | "
            f"{row['delta_total_r']} | {row['delta_avg_r']} | "
            f"{row['baseline_wr_pct']}% | {row['alt_positive_n']} |"
        )
    lines += [
        "",
        "This is currently an **insufficient preliminary forward sample**, not a "
        "decision-quality OOS test: the database contains only the newly resolved "
        "positions after the cutoff. A strategy with zero rows has no OOS result.",
        "A historical slice before the cutoff would not be a clean unseen holdout "
        "because the preceding grid-search used the full stored history.",
        "",
        "## In-sample outliers at the fixed steps",
        "",
        "These are diagnostics from the original in-sample path simulation, not "
        "additional OOS observations.",
    ]
    for strategy, rows in outlier_rows.items():
        lines += [
            "",
            f"### {strategy} ({FIXED_STEPS[strategy]}%) — top positive ΔR",
            "",
            "| Symbol | Baseline R | Alt R | ΔR | Alt outcome |",
            "|---|---:|---:|---:|---|",
        ]
        for row in rows[:5]:
            lines.append(
                f"| {row['symbol']} | {row['baseline_r']} | {row['alt_r']} | "
                f"{row['delta_r']} | {row['alt_outcome']} |"
            )
        lines += [
            "",
            f"### {strategy} ({FIXED_STEPS[strategy]}%) — top negative ΔR",
            "",
            "| Symbol | Baseline R | Alt R | ΔR | Alt outcome |",
            "|---|---:|---:|---:|---|",
        ]
        for row in rows[-5:][::-1]:
            lines.append(
                f"| {row['symbol']} | {row['baseline_r']} | {row['alt_r']} | "
                f"{row['delta_r']} | {row['alt_outcome']} |"
            )
    lines += [
        "",
        "## OOS interpretation",
        "",
        "Even a positive result on this forward slice would not establish that the "
        "rule works going forward until there are enough independent observations.",
        "The fixed-step test avoids re-optimizing on this slice, but it still needs a "
        "predefined minimum sample, a longer time span, and separate monitoring of "
        "fees, slippage, market regime, and 5m intrabar ambiguity.",
        "",
        "```json",
        json.dumps(coverage, indent=2, sort_keys=True),
        "```",
    ]
    (output_dir / "oos_analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--input", type=Path, default=Path("outcome_trailing_stop"))
    parser.add_argument("--out", type=Path, default=Path("outcome_trailing_stop"))
    parser.add_argument("--cutoff", help="Override cutoff with ISO-8601 UTC timestamp.")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    coverage = json.loads((args.input / "coverage.json").read_text(encoding="utf-8"))
    cutoff = parse_cutoff(coverage, args.cutoff)
    forward_positions = load_forward_positions(args.db, cutoff)
    ranges = build_ranges(forward_positions)
    paths, failures = fetch_paths(ranges, args.workers) if ranges else ({}, {})

    oos_rows: list[dict[str, Any]] = []
    for position in forward_positions:
        strategy = str(position["alert_type"])
        candles = paths.get(str(position["symbol"]), [])
        result = simulate(
            position,
            candles,
            candles,
            FIXED_STEPS[strategy],
        )
        result["ts_open_utc"] = fmt_ts(int(position["ts_open"]))
        result["ts_close_utc"] = fmt_ts(int(position["ts_close"]))
        result["delta_r"] = (
            round(float(result["alt_r"]) - float(result["baseline_r"]), 6)
            if not result["coverage_error"]
            else ""
        )
        if str(position["symbol"]) in failures:
            result["coverage_error"] = failures[str(position["symbol"])]
        oos_rows.append(result)

    path_rows = read_csv(args.input / "trailing_rows.csv")
    outlier_rows = {
        strategy: outliers(path_rows, strategy, step)
        for strategy, step in FIXED_STEPS.items()
    }
    summaries = summarize(oos_rows)
    current_utc = datetime.now(timezone.utc).isoformat()
    report_coverage = {
        "cutoff_utc": fmt_ts(cutoff),
        "generated_utc": current_utc,
        "fixed_steps_pct": FIXED_STEPS,
        "forward_positions": len(forward_positions),
        "forward_symbols_requested": len(ranges),
        "forward_symbols_loaded": len(paths),
        "symbol_fetch_failures": failures,
        "path_interval": PATH_INTERVAL,
        "historical_grid_generated_utc": coverage.get("generated_utc"),
        "outlier_source_rows": len(path_rows),
    }

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "oos_rows.csv", oos_rows)
    write_csv(args.out / "oos_summary.csv", summaries)
    write_csv(
        args.out / "oos_outliers.csv",
        [
            row
            for strategy_rows in outlier_rows.values()
            for row in strategy_rows[:5] + strategy_rows[-5:][::-1]
        ],
    )
    write_report(
        args.out, cutoff, current_utc, summaries, outlier_rows, report_coverage
    )
    print(json.dumps({
        "cutoff_utc": fmt_ts(cutoff),
        "forward_positions": len(forward_positions),
        "summaries": summaries,
        "output": str(args.out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())