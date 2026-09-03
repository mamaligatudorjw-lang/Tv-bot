#!/usr/bin/env python3
"""Read-only out-of-sample sweep of the partial-TP position fraction.

The in-sample study (`partial_tp50_trailing`) fixed the split at 50%: half the
position closes at TP and half trails with a hard TP floor.  That fraction was
never tuned.  This script re-runs the same simulation on signals the in-sample
cohort never saw — everything at or after the frozen artifact cutoff — and
sweeps the fraction instead of assuming it.

Method note.  The trailed leg's price path does not depend on how large that
leg is, so the fraction is a pure re-weighting of two already simulated legs:

    total_r(f) = f * baseline_r + (1 - f) * second_half_r   (TP branch)
    total_r(f) = baseline_r                                 (no TP reached)

The simulation itself is imported unchanged from `partial_tp50_trailing`, and
at f = POSITION_FRACTION the derived value is asserted against that module's
own `total_r` on every row, so the arithmetic is checked against the reference
implementation on each run rather than trusted.

This analysis does not import app.py, opens SQLite read-only, and never writes
to the production database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from partial_tp50_trailing import (
    POSITION_FRACTION,
    STEPS,
    STRATEGY,
    as_float,
    as_int,
    fetch_paths,
    simulate_partial,
    write_csv,
)
from trailing_stop_analysis import PATH_INTERVAL
from wr35_trailing_bootstrap import paired_mean_ci

# Fractions closed at TP.  1.0 reproduces today's production behaviour (the
# whole position exits at TP); 0.0 keeps everything in the trail.
FRACTIONS = (1.00, 0.75, 0.50, 0.25, 0.00)

# End of the in-sample window.  Signals before this belong to the frozen
# cohort and are excluded so the sweep is genuinely out of sample.
IN_SAMPLE_CUTOFF_UTC = "2026-08-26T06:09:56.963415+00:00"

RESOLVED_STATUSES = ("tp", "sl")
BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20260903


def parse_ts(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def oos_positions(
    db_path: Path,
    start_ts: int,
    cutoff_ts: int,
    strategy: str,
) -> list[dict[str, Any]]:
    """Resolved signals of one strategy strictly after the in-sample window."""
    if start_ts >= cutoff_ts:
        raise ValueError("start_ts must precede cutoff_ts")
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type,
                       is_shadow
                  FROM demo_positions
                 WHERE alert_type = ?
                   AND ts_open >= ?
                   AND ts_open < ?
                   AND status IN ({",".join("?" for _ in RESOLVED_STATUSES)})
                 ORDER BY id
                """,
                (strategy, start_ts, cutoff_ts, *RESOLVED_STATUSES),
            )
        ]
    finally:
        connection.close()
    if not rows:
        raise ValueError(
            f"No resolved {strategy} signals in [{start_ts}, {cutoff_ts})"
        )
    # A position still running has no realized baseline; the simulation would
    # silently treat any non-"tp" status as a stop-out, so they are excluded
    # above rather than counted as losses here.
    return rows


def derived_total_r(row: dict[str, Any], fraction: float) -> float | None:
    """Re-weight one simulated row to an arbitrary close fraction."""
    if row["outcome"] == "unresolved" or row["total_r"] == "":
        return None
    baseline_r = as_float(row["baseline_r"], "baseline_r")
    if row["partial_branch"] != "tp_branch":
        return baseline_r
    second_half_r = as_float(row["second_half_r"], "second_half_r")
    return fraction * baseline_r + (1.0 - fraction) * second_half_r


def assert_matches_reference(rows: Sequence[dict[str, Any]]) -> None:
    """Check the re-weighting against the imported module's own total_r."""
    for row in rows:
        derived = derived_total_r(row, POSITION_FRACTION)
        if derived is None:
            continue
        reference = as_float(row["total_r"], "total_r")
        if abs(derived - reference) > 1e-9:
            raise AssertionError(
                f"fraction re-weighting disagrees with simulate_partial on "
                f"id={row['id']} step={row['step_pct']}: "
                f"{derived!r} vs {reference!r}"
            )


def summarize(
    rows: Sequence[dict[str, Any]],
    baseline_by_id: dict[int, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary: list[dict[str, Any]] = []
    bootstrap: list[dict[str, Any]] = []
    for step in STEPS:
        step_rows = [row for row in rows if as_float(row["step_pct"], "step_pct") == step]
        for fraction in FRACTIONS:
            realized: list[float] = []
            deltas: list[float] = []
            unresolved = 0
            for row in step_rows:
                value = derived_total_r(row, fraction)
                if value is None:
                    unresolved += 1
                    continue
                realized.append(value)
                deltas.append(value - baseline_by_id[as_int(row["id"], "id")])
            n = len(realized)
            summary.append(
                {
                    "step_pct": step,
                    "close_fraction": fraction,
                    "n_resolved": n,
                    "n_unresolved": unresolved,
                    "sum_r": sum(realized),
                    "avg_r": (sum(realized) / n) if n else "",
                    "win_rate_pct": (
                        100.0 * sum(1 for v in realized if v > 0) / n
                    ) if n else "",
                }
            )
            observed, low, high = paired_mean_ci(
                deltas,
                iterations=BOOTSTRAP_ITERATIONS,
                seed=BOOTSTRAP_SEED,
            )
            bootstrap.append(
                {
                    "step_pct": step,
                    "close_fraction": fraction,
                    "n_paired": len(deltas),
                    "n_unresolved": unresolved,
                    "delta_avg_r": observed,
                    "ci_low": low,
                    "ci_high": high,
                    "ci_width": high - low,
                    "crosses_zero": "yes" if low <= 0.0 <= high else "no",
                }
            )
    return summary, bootstrap


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    coverage = report["coverage"]
    lines = [
        "# Out-of-sample partial-TP fraction sweep",
        "",
        "**Read-only out-of-sample analysis. Production, SQLite, demo positions "
        "and forward-shadow state were not changed.**",
        "",
        "The in-sample study fixed the close fraction at 50% without tuning it. "
        "This sweep uses only signals at or after the in-sample cutoff and "
        "re-weights the two simulated legs; the simulation itself is the "
        "unchanged `partial_tp50_trailing` implementation.",
        "",
        "## Coverage",
        "",
        f"- Strategy: `{report['config']['strategy']}`.",
        f"- Out-of-sample window: `{coverage['window_start_utc']}` "
        f"to `{coverage['cutoff_utc']}`.",
        f"- Resolved signals: `{coverage['signals']}`.",
        f"- Baseline TP reach: `{coverage['baseline_tp_n']}/{coverage['signals']}` "
        f"({coverage['baseline_tp_reach_pct']:.4f}%).",
        f"- Symbols requested/loaded: "
        f"`{coverage['symbols_requested']}/{coverage['symbols_loaded']}`.",
        f"- Shadow / real composition: `{coverage['shadow_n']}` / "
        f"`{coverage['real_n']}`.",
        "",
        "## Average R by close fraction",
        "",
        "| Step | " + " | ".join(f"f={f:.2f}" for f in FRACTIONS) + " |",
        "|---|" + "---:|" * len(FRACTIONS),
    ]
    by_key = {
        (row["step_pct"], row["close_fraction"]): row for row in report["summary"]
    }
    for step in STEPS:
        cells = []
        for fraction in FRACTIONS:
            value = by_key[(step, fraction)]["avg_r"]
            cells.append(f"{value:.6f}" if value != "" else "n/a")
        lines.append(f"| {step} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Paired bootstrap CI against the fixed-TP baseline",
        "",
        "The paired delta is the re-weighted total R minus the single-TP "
        "baseline R for the same signal. Resampling is by unique signal ID. "
        "Unresolved rows are excluded from the realized metric and stay "
        "visible in `n_unresolved`.",
        "",
        "| Step | f | n paired | Unresolved | Δ avg R | 95% CI | Crosses 0 |",
        "|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in report["bootstrap"]:
        lines.append(
            f"| {row['step_pct']} | {row['close_fraction']:.2f} | "
            f"{row['n_paired']} | {row['n_unresolved']} | "
            f"{row['delta_avg_r']:.6f} | "
            f"[{row['ci_low']:.6f}, {row['ci_high']:.6f}] | "
            f"{row['crosses_zero']} |"
        )

    lines += [
        "",
        "## Limitations",
        "",
        "- Out-of-sample for the fraction, but the trailing step grid is the "
        "one selected in-sample; picking a step here would re-introduce the "
        "same double-dip this analysis exists to avoid.",
        "- The trailed leg carries a hard floor at TP. Live execution cannot "
        "guarantee that floor: price may touch TP and reverse inside one "
        f"{PATH_INTERVAL} candle, which the path data cannot resolve.",
        "- Lower fractions raise expected R and widen the distribution; the "
        "CI width column is the risk side of the same trade.",
        "- No fees or slippage beyond the source R semantics. A proportional "
        "fee does not grow when one close is split into two.",
        "- Positions unresolved at the cutoff are censored, not counted as "
        "wins or as TP exits.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_analysis(
    db_path: Path,
    output_dir: Path,
    *,
    cutoff_ts: int,
    start_ts: int,
    workers: int,
) -> dict[str, Any]:
    positions = oos_positions(db_path, start_ts, cutoff_ts, STRATEGY)
    paths, failures, cached_count = fetch_paths(
        positions,
        cutoff_ts,
        output_dir / "candle_cache",
        workers,
    )

    simulation_rows: list[dict[str, Any]] = []
    for position in positions:
        position = dict(position)
        position["cutoff_ts"] = cutoff_ts
        candles = paths.get(str(position["symbol"]), [])
        for step in STEPS:
            row = simulate_partial(position, candles, step)
            if str(position["symbol"]) in failures and position["status"] == "tp":
                row["coverage_error"] = "symbol_fetch_failure"
                row["outcome"] = "unresolved"
                row["total_r"] = ""
            simulation_rows.append(row)

    assert_matches_reference(simulation_rows)

    baseline_by_id = {
        as_int(row["id"], "id"): as_float(row["baseline_r"], "baseline_r")
        for row in simulation_rows
    }
    summary, bootstrap = summarize(simulation_rows, baseline_by_id)

    for row in simulation_rows:
        for fraction in FRACTIONS:
            value = derived_total_r(row, fraction)
            row[f"total_r_f{int(fraction * 100):03d}"] = (
                "" if value is None else value
            )

    baseline_tp_n = sum(row["status"] == "tp" for row in positions)
    shadow_n = sum(int(as_int(row["is_shadow"], "is_shadow")) for row in positions)
    report = {
        "config": {
            "analysis": "partial_tp_fraction_oos",
            "strategy": STRATEGY,
            "close_fractions": list(FRACTIONS),
            "reference_fraction": POSITION_FRACTION,
            "trailing_floor": "tp",
            "steps_pct": list(STEPS),
            "path_interval": PATH_INTERVAL,
            "read_only": True,
            "in_sample": False,
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
        "coverage": {
            "window_start_utc": datetime.fromtimestamp(
                start_ts, timezone.utc
            ).isoformat(),
            "cutoff_utc": datetime.fromtimestamp(
                cutoff_ts, timezone.utc
            ).isoformat(),
            "signals": len(positions),
            "baseline_tp_n": baseline_tp_n,
            "baseline_sl_n": len(positions) - baseline_tp_n,
            "baseline_tp_reach_pct": 100.0 * baseline_tp_n / len(positions),
            "shadow_n": shadow_n,
            "real_n": len(positions) - shadow_n,
            "symbols_requested": len({str(row["symbol"]) for row in positions}),
            "symbols_loaded": len(paths),
            "symbol_fetch_failures": failures,
            "cached_symbol_paths": cached_count,
            "simulation_rows": len(simulation_rows),
        },
        "summary": summary,
        "bootstrap": bootstrap,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "audit.csv", simulation_rows)
    write_csv(output_dir / "grid_summary.csv", summary)
    write_csv(output_dir / "paired_bootstrap.csv", bootstrap)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_markdown(output_dir / "analysis.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outcome_partial_tp_fraction_oos"),
    )
    parser.add_argument(
        "--start",
        default=IN_SAMPLE_CUTOFF_UTC,
        help="Start of the out-of-sample window (in-sample cutoff by default).",
    )
    parser.add_argument(
        "--cutoff",
        required=True,
        help="ISO timestamp that freezes candle processing for this run.",
    )
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    report = run_analysis(
        args.db,
        args.output_dir,
        cutoff_ts=parse_ts(args.cutoff),
        start_ts=parse_ts(args.start),
        workers=args.workers,
    )
    coverage = report["coverage"]
    print(
        f"signals={coverage['signals']} "
        f"tp_reach={coverage['baseline_tp_reach_pct']:.2f}% "
        f"symbols={coverage['symbols_loaded']}/{coverage['symbols_requested']} "
        f"-> {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
