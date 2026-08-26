#!/usr/bin/env python3
"""Follow-up checks for the exploratory trailing-stop analysis.

This script is deliberately local and read-only. It compares the observed
position result used by strategy selection with the level-based baseline used
by the 5m path simulation, then calculates paired bootstrap intervals for the
best in-sample trailing step. It does not fetch candles, import app.py, or
mutate the trading database.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable


BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20260826
RECONCILIATION_STRATEGIES = (
    "high_rejection_short",
    "ema_cross",
    "overheated_24h",
)
BOOTSTRAP_STRATEGIES = (
    "overheated_24h",
    "ema_cross_confirmed",
    "overheated_early",
    "ema_cross",
    "overheated_confirmed",
)


def price_r(direction: str, entry: float, stop: float, price: float) -> float:
    risk = abs(stop - entry)
    if risk <= 0:
        return float("nan")
    return (price - entry) / risk if direction == "LONG" else (entry - price) / risk


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


def bootstrap_ci(
    values: list[float],
    statistic: Callable[[list[float]], float],
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    if not values:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(values)
    estimates = [
        statistic([values[rng.randrange(n)] for _ in range(n)])
        for _ in range(iterations)
    ]
    estimates.sort()
    return (
        statistic(values),
        quantile(estimates, 0.025),
        quantile(estimates, 0.975),
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_positions(db_path: Path, ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT id, symbol, alert_type, direction, entry_price, sl_price,
               tp_price, status, exit_price
          FROM demo_positions
         WHERE id IN ({placeholders})
        """,
        sorted(ids),
    )
    output = {int(row["id"]): dict(row) for row in rows}
    connection.close()
    return output


def level_baseline(position: dict[str, Any]) -> float:
    exit_level = (
        float(position["tp_price"])
        if position["status"] == "tp"
        else float(position["sl_price"])
    )
    return price_r(
        position["direction"],
        float(position["entry_price"]),
        float(position["sl_price"]),
        exit_level,
    )


def observed_r(position: dict[str, Any]) -> float:
    return price_r(
        position["direction"],
        float(position["entry_price"]),
        float(position["sl_price"]),
        float(position["exit_price"]),
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def reconcile(
    ranking: list[dict[str, str]],
    path_rows: list[dict[str, str]],
    positions: dict[int, dict[str, Any]],
    strategies: tuple[str, ...],
) -> list[dict[str, Any]]:
    ranking_by_strategy = {row["strategy"]: row for row in ranking}
    by_strategy: dict[str, dict[int, dict[str, str]]] = defaultdict(dict)
    for row in path_rows:
        if row["strategy"] in strategies and not row["coverage_error"]:
            by_strategy[row["strategy"]][int(row["id"])] = row

    output = []
    for strategy in strategies:
        path_items = by_strategy[strategy]
        position_items = [
            positions[position_id]
            for position_id in path_items
            if position_id in positions
        ]
        observed = [observed_r(item) for item in position_items]
        level = [float(path_items[int(item["id"])]["baseline_r"]) for item in position_items]
        observed = [value for value in observed if math.isfinite(value)]
        level = [value for value in level if math.isfinite(value)]
        mismatches = sum(
            abs(observed_r(item) - level_baseline(item)) > 1e-9
            for item in position_items
        )
        tp_items = [item for item in position_items if item["status"] == "tp"]
        sl_items = [item for item in position_items if item["status"] == "sl"]
        selection = ranking_by_strategy.get(strategy, {})
        output.append({
            "strategy": strategy,
            "n_selection": int(selection.get("n_resolved", 0)),
            "n_path_baseline": len(level),
            "coverage_excluded_n": int(selection.get("n_resolved", 0)) - len(level),
            "selection_avg_r_table": selection.get("avg_r_observed", ""),
            "selection_avg_r_recomputed": round(mean(observed), 6) if observed else "",
            "path_baseline_avg_r": round(mean(level), 6) if level else "",
            "observed_minus_path_avg_r": (
                round(mean(observed) - mean(level), 6)
                if observed and level
                else ""
            ),
            "selection_total_r_recomputed": round(sum(observed), 6),
            "path_baseline_total_r": round(sum(level), 6),
            "exit_vs_level_mismatch_n": mismatches,
            "tp_n": len(tp_items),
            "sl_n": len(sl_items),
            "observed_tp_avg_r": (
                round(mean(observed_r(item) for item in tp_items), 6)
                if tp_items
                else ""
            ),
            "observed_sl_avg_r": (
                round(mean(observed_r(item) for item in sl_items), 6)
                if sl_items
                else ""
            ),
            "level_tp_r": (
                round(mean(level_baseline(item) for item in tp_items), 6)
                if tp_items
                else ""
            ),
            "level_sl_r": (
                round(mean(level_baseline(item) for item in sl_items), 6)
                if sl_items
                else ""
            ),
        })
    return output


def bootstrap_best_steps(
    summaries: list[dict[str, str]],
    path_rows: list[dict[str, str]],
    strategies: tuple[str, ...],
) -> list[dict[str, Any]]:
    best_step: dict[str, dict[str, str]] = {}
    for strategy in strategies:
        candidates = [
            row
            for row in summaries
            if row["strategy"] == strategy and row["range_bucket"] == "all"
        ]
        if not candidates:
            continue
        best_step[strategy] = max(
            candidates, key=lambda row: float(row["delta_total_r"])
        )

    output = []
    for strategy in strategies:
        chosen = best_step.get(strategy)
        if not chosen:
            continue
        step = chosen["step_pct"]
        items = [
            row
            for row in path_rows
            if row["strategy"] == strategy
            and row["step_pct"] == step
            and not row["coverage_error"]
        ]
        differences = [
            float(row["alt_r"]) - float(row["baseline_r"]) for row in items
        ]
        mean_value, mean_low, mean_high = bootstrap_ci(differences, mean)
        median_value, median_low, median_high = bootstrap_ci(
            differences, median, seed=BOOTSTRAP_SEED + 1
        )
        output.append({
            "strategy": strategy,
            "best_step_pct": step,
            "n": len(differences),
            "baseline_avg_r": chosen["baseline_avg_r"],
            "alt_avg_r": chosen["alt_avg_r"],
            "diff_avg_r": round(mean_value, 6),
            "mean_ci95_low": round(mean_low, 6),
            "mean_ci95_high": round(mean_high, 6),
            "mean_ci_excludes_zero": mean_low > 0 or mean_high < 0,
            "median_diff_r": round(median_value, 6),
            "median_ci95_low": round(median_low, 6),
            "median_ci95_high": round(median_high, 6),
            "median_ci_excludes_zero": median_low > 0 or median_high < 0,
            "positive_diff_n": sum(value > 0 for value in differences),
            "negative_diff_n": sum(value < 0 for value in differences),
            "zero_diff_n": sum(value == 0 for value in differences),
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "bootstrap_seed": BOOTSTRAP_SEED,
        })
    return output


def write_report(
    output_dir: Path,
    reconciliation: list[dict[str, Any]],
    bootstrap: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> None:
    lines = [
        "# Trailing-stop follow-up analysis",
        "",
        "**Read-only. Production logic and the trading database were not changed.**",
        "",
        "## Baseline reconciliation",
        "",
        "The selection table uses the recorded market `exit_price` from `demo_positions`.",
        "The path-simulation baseline uses the nominal TP level for recorded TP outcomes",
        "and the nominal SL level for recorded SL outcomes. Therefore these averages are",
        "not expected to be identical when exits overshoot or undershoot their levels.",
        "",
        "| Strategy | n | Selection avg R | Recomputed selection avg R | Path baseline avg R | Observed − path | Exit/level mismatches |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in reconciliation:
        lines.append(
            f"| {row['strategy']} | {row['n_path_baseline']} | "
            f"{row['selection_avg_r_table']} | {row['selection_avg_r_recomputed']} | "
            f"{row['path_baseline_avg_r']} | {row['observed_minus_path_avg_r']} | "
            f"{row['exit_vs_level_mismatch_n']} |"
        )
    lines += [
        "",
        "The reconciliation is systematic in definition, not necessarily in direction:",
        "actual exits can improve TP trades or worsen SL trades depending on slippage and",
        "the recorded close price. `selection_avg_r_recomputed` matches the ranking-table",
        "definition, while `path_baseline_avg_r` is the level-based counterfactual.",
        "",
        "## Paired bootstrap at the best in-sample step",
        "",
        "Each bootstrap resample keeps the per-trade pairing and resamples the vector",
        "`alt_best_step_r - baseline_r`. The interval is a 95% percentile bootstrap CI.",
        "",
        "| Strategy | Best step | n | Mean ΔR | Mean CI 95% | Median ΔR | Median CI 95% | CI excludes 0? |",
        "|---|---:|---:|---:|---|---:|---|---|",
    ]
    for row in bootstrap:
        lines.append(
            f"| {row['strategy']} | {row['best_step_pct']}% | {row['n']} | "
            f"{row['diff_avg_r']} | [{row['mean_ci95_low']}, {row['mean_ci95_high']}] | "
            f"{row['median_diff_r']} | "
            f"[{row['median_ci95_low']}, {row['median_ci95_high']}] | "
            f"mean={row['mean_ci_excludes_zero']}; median={row['median_ci_excludes_zero']} |"
        )
    lines += [
        "",
        "A CI excluding zero would only indicate that the paired in-sample difference",
        "is unlikely to be explained by resampling noise under this fixed sample and",
        "model. It does **not** replace an out-of-sample test on a separate time period.",
        "Bootstrap assesses whether this observed effect looks like noise; it does not",
        "establish that the trailing rule will work going forward. The best-step choice",
        "itself is also subject to grid-search and selection bias.",
        "",
        "```json",
        json.dumps(coverage, indent=2, sort_keys=True),
        "```",
    ]
    (output_dir / "followup_analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--input", type=Path, default=Path("outcome_trailing_stop"))
    parser.add_argument("--out", type=Path, default=Path("outcome_trailing_stop"))
    args = parser.parse_args()

    ranking = read_csv(args.input / "strategy_ranking.csv")
    summaries = read_csv(args.input / "summary.csv")
    path_rows = read_csv(args.input / "trailing_rows.csv")
    ids = {int(row["id"]) for row in path_rows}
    positions = load_positions(args.db, ids)

    reconciliation = reconcile(
        ranking, path_rows, positions, RECONCILIATION_STRATEGIES
    )
    bootstrap = bootstrap_best_steps(summaries, path_rows, BOOTSTRAP_STRATEGIES)
    coverage = {
        "input": str(args.input),
        "reconciliation_strategies": list(RECONCILIATION_STRATEGIES),
        "bootstrap_strategies": list(BOOTSTRAP_STRATEGIES),
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "path_rows": len(path_rows),
        "positions_loaded": len(positions),
    }

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "baseline_reconciliation.csv", reconciliation)
    write_csv(args.out / "paired_bootstrap.csv", bootstrap)
    write_report(args.out, reconciliation, bootstrap, coverage)
    print(json.dumps({
        "baseline_reconciliation": reconciliation,
        "paired_bootstrap": bootstrap,
        "output": str(args.out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())