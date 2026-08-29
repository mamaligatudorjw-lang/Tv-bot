#!/usr/bin/env python3
"""Read-only in-sample simulation for partial TP plus trailing remainder.

The frozen #136 signal IDs define the sample. Historical 5m candles are read
from Gate.io only through the original frozen artifact cutoff. A baseline SL
signal never enters the partial-TP branch. A baseline TP signal closes half at
TP, then trails the other half with a hard floor at TP.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import requests

from shadow_outcome_report import fetch_candles
from trailing_stop_analysis import PATH_INTERVAL, PATH_INTERVAL_SEC, price_r
from wr35_trailing_bootstrap import paired_mean_ci


STRATEGY = "overheated_24h"
STEPS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
POSITION_FRACTION = 0.5
SOURCE_FILENAME = "trailing_rows.csv"
COVERAGE_FILENAME = "coverage.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def as_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Invalid non-finite {field}: {value!r}")
    return result


def parse_cutoff(value: str) -> int:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return int(timestamp.timestamp())


def frozen_positions(
    db_path: Path,
    source_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[int, dict[str, str]]]:
    coverage = json.loads(
        (source_dir / COVERAGE_FILENAME).read_text(encoding="utf-8")
    )
    source_rows = read_csv(source_dir / SOURCE_FILENAME)
    rows = [row for row in source_rows if row.get("strategy") == STRATEGY]
    if not rows:
        raise ValueError(f"No {STRATEGY} rows in frozen source")
    source_ids = {as_int(row.get("id"), "source id") for row in rows}
    source_steps = {as_float(row.get("step_pct"), "step_pct") for row in rows}
    if source_steps != set(STEPS):
        raise ValueError(f"Frozen source steps do not match: {source_steps}")
    if len({(row.get("id"), row.get("step_pct")) for row in rows}) != len(rows):
        raise ValueError("Frozen source contains duplicate id/step rows")

    source_by_id: dict[int, dict[str, str]] = {}
    for row in rows:
        source_by_id.setdefault(as_int(row["id"], "source id"), row)

    connection = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in source_ids)
    try:
        db_rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type, is_shadow
                  FROM demo_positions
                 WHERE id IN ({placeholders})
                """,
                sorted(source_ids),
            )
        ]
    finally:
        connection.close()

    positions_by_id = {as_int(row["id"], "position id"): row for row in db_rows}
    missing = source_ids - set(positions_by_id)
    if missing:
        raise ValueError(f"Frozen IDs missing from demo_positions: {sorted(missing)[:10]}")
    for position_id, row in positions_by_id.items():
        if (
            row["alert_type"] != STRATEGY
            or row["status"] not in ("tp", "sl")
            or row["ts_close"] is None
            or row["exit_price"] is None
        ):
            raise ValueError(f"Frozen position {position_id} is not resolved target data")
        source = source_by_id[position_id]
        source_baseline = as_float(source["baseline_r"], "source baseline_r")
        db_baseline = (
            price_r(
                row["direction"],
                as_float(row["entry_price"], "entry_price"),
                as_float(row["sl_price"], "sl_price"),
                as_float(row["tp_price"], "tp_price"),
            )
            if row["status"] == "tp"
            else -1.0
        )
        if abs(source_baseline - db_baseline) > 1e-6:
            raise ValueError(
                f"Frozen baseline mismatch for id={position_id}: "
                f"source={source_baseline}, db={db_baseline}"
            )

    cutoff_value = coverage.get("generated_utc")
    if not cutoff_value:
        raise ValueError("Frozen coverage.json has no generated_utc cutoff")
    cutoff_ts = parse_cutoff(str(cutoff_value))
    too_new = [
        position_id
        for position_id, row in positions_by_id.items()
        if as_int(row["ts_open"], "ts_open") >= cutoff_ts
    ]
    if too_new:
        raise ValueError(f"Frozen sample contains entries at/after cutoff: {too_new[:10]}")
    return (
        sorted(positions_by_id.values(), key=lambda row: (row["ts_open"], row["id"])),
        {
            "frozen_source": str(source_dir / SOURCE_FILENAME),
            "frozen_source_generated_utc": cutoff_value,
            "frozen_source_rows": len(source_rows),
            "frozen_target_rows": len(rows),
            "frozen_target_ids": len(source_ids),
            "frozen_target_strategy": STRATEGY,
            "frozen_steps_pct": list(STEPS),
            "cutoff_ts": cutoff_ts,
        },
        source_by_id,
    )


def load_or_fetch_symbol(
    symbol: str,
    start: int,
    end: int,
    cache_dir: Path,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    cache_path = cache_dir / f"{symbol}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cached.get("start") <= start
            and cached.get("end") >= end
            and cached.get("interval") == PATH_INTERVAL
        ):
            return cached.get("candles", []), None, True

    session = requests.Session()
    try:
        candles = fetch_candles(
            session,
            symbol,
            start,
            end,
            interval=PATH_INTERVAL,
            interval_sec=PATH_INTERVAL_SEC,
        )
        cache_path.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "start": start,
                    "end": end,
                    "interval": PATH_INTERVAL,
                    "candles": candles,
                }
            ),
            encoding="utf-8",
        )
        return candles, None, False
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}", False
    finally:
        session.close()


def fetch_paths(
    positions: Sequence[dict[str, Any]],
    cutoff_ts: int,
    cache_dir: Path,
    workers: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], int]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    bounds: dict[str, int] = {}
    for row in positions:
        symbol = str(row["symbol"])
        bounds[symbol] = min(bounds.get(symbol, cutoff_ts), as_int(row["ts_open"], "ts_open"))

    paths: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, str] = {}
    cached_count = 0

    def fetch_one(symbol: str, start: int):
        return symbol, load_or_fetch_symbol(symbol, start, cutoff_ts, cache_dir)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(fetch_one, symbol, start): symbol
            for symbol, start in bounds.items()
        }
        for index, future in enumerate(as_completed(futures), start=1):
            symbol, (candles, error, cached) = future.result()
            if error:
                failures[symbol] = error
            else:
                paths[symbol] = candles
            cached_count += int(cached)
            if index < len(futures):
                time.sleep(0.05)
    return paths, failures, cached_count


def candle_starts(candles: Sequence[dict[str, Any]]) -> list[int]:
    return sorted({as_int(candle["t"], "candle timestamp") for candle in candles})


def first_completed_candle(ts_open: int) -> int:
    return ((ts_open + PATH_INTERVAL_SEC - 1) // PATH_INTERVAL_SEC) * PATH_INTERVAL_SEC


def tp_trigger(
    row: dict[str, Any],
    candles: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    start = first_completed_candle(as_int(row["ts_open"], "ts_open"))
    close = as_int(row["ts_close"], "ts_close")
    target = as_float(row["tp_price"], "tp_price")
    relevant = [
        candle
        for candle in candles
        if start <= as_int(candle["t"], "candle timestamp") <= close
    ]
    starts = candle_starts(relevant)
    if any(right - left > PATH_INTERVAL_SEC for left, right in zip(starts, starts[1:])):
        return None, "5m_candle_gap_before_tp"
    for candle in relevant:
        high = as_float(candle["h"], "candle high")
        low = as_float(candle["l"], "candle low")
        hit = high >= target if row["direction"] == "LONG" else low <= target
        if hit:
            return candle, None
    return None, "tp_candle_not_covered"


def simulate_partial(
    row: dict[str, Any],
    candles: Sequence[dict[str, Any]],
    step_pct: float,
) -> dict[str, Any]:
    entry = as_float(row["entry_price"], "entry_price")
    original_sl = as_float(row["sl_price"], "sl_price")
    target = as_float(row["tp_price"], "tp_price")
    direction = str(row["direction"])
    baseline_r = (
        price_r(direction, entry, original_sl, target)
        if row["status"] == "tp"
        else -1.0
    )
    result: dict[str, Any] = {
        "id": as_int(row["id"], "id"),
        "strategy": STRATEGY,
        "symbol": row["symbol"],
        "direction": direction,
        "baseline_status": row["status"],
        "baseline_r": baseline_r,
        "step_pct": step_pct,
        "tp_reached": row["status"] == "tp",
        "tp_trigger_ts": "",
        "tp_trigger_price": "",
        "partial_branch": "not_activated",
        "post_tp_candles": 0,
        "trajectory_updates_json": "[]",
        "trail_ts": "",
        "trail_price": "",
        "second_half_r": "",
        "total_r": baseline_r,
        "outcome": "baseline_sl" if row["status"] == "sl" else "",
        "coverage_error": "",
    }
    if row["status"] == "sl":
        return result

    trigger, trigger_error = tp_trigger(row, candles)
    if trigger_error:
        result["partial_branch"] = "tp_branch_unresolved"
        result["outcome"] = "unresolved"
        result["total_r"] = ""
        result["coverage_error"] = trigger_error
        return result

    trigger_ts = as_int(trigger["t"], "tp trigger timestamp")
    result.update(
        {
            "tp_trigger_ts": trigger_ts,
            "tp_trigger_price": target,
            "partial_branch": "tp_branch",
        }
    )
    post = [
        candle
        for candle in candles
        if trigger_ts < as_int(candle["t"], "candle timestamp")
        and as_int(candle["t"], "candle timestamp") + PATH_INTERVAL_SEC
        <= as_int(row["cutoff_ts"], "cutoff_ts")
    ]
    starts = candle_starts(post)
    if any(right - left > PATH_INTERVAL_SEC for left, right in zip(starts, starts[1:])):
        result["outcome"] = "unresolved"
        result["total_r"] = ""
        result["coverage_error"] = "5m_candle_gap_after_tp"
        return result

    stop = target
    favorable_extreme = target
    trajectory: list[dict[str, Any]] = []
    for candle in post:
        candle_ts = as_int(candle["t"], "candle timestamp")
        high = as_float(candle["h"], "candle high")
        low = as_float(candle["l"], "candle low")
        hit_stop = low <= stop if direction == "LONG" else high >= stop
        if hit_stop:
            second_r = price_r(direction, entry, original_sl, stop)
            total_r = POSITION_FRACTION * baseline_r + POSITION_FRACTION * second_r
            result.update(
                {
                    "post_tp_candles": len(trajectory) + 1,
                    "trajectory_updates_json": json.dumps(trajectory, separators=(",", ":")),
                    "trail_ts": candle_ts,
                    "trail_price": stop,
                    "second_half_r": second_r,
                    "total_r": total_r,
                    "outcome": "partial_tp_floor"
                    if stop == target
                    else "partial_trail_stop",
                }
            )
            return result

        previous_stop = stop
        previous_extreme = favorable_extreme
        if direction == "LONG" and high > entry:
            favorable_extreme = max(favorable_extreme, high)
            stop = max(target, favorable_extreme * (1.0 - step_pct / 100.0))
        elif direction == "SHORT" and low < entry:
            favorable_extreme = min(favorable_extreme, low)
            stop = min(target, favorable_extreme * (1.0 + step_pct / 100.0))
        if stop != previous_stop or favorable_extreme != previous_extreme:
            trajectory.append(
                {
                    "ts": candle_ts,
                    "favorable_extreme": favorable_extreme,
                    "stop": stop,
                }
            )

    result.update(
        {
            "post_tp_candles": len(post),
            "trajectory_updates_json": json.dumps(trajectory, separators=(",", ":")),
            "outcome": "unresolved",
            "total_r": "",
            "coverage_error": "trailing_not_hit_by_frozen_cutoff",
        }
    )
    return result


def metric(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    total = sum(values)
    return total, total / len(values)


def summarize(
    rows: Sequence[dict[str, Any]],
    steps: Sequence[float],
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_rows = list(rows)
    baseline_values = [as_float(row["baseline_r"], "baseline_r") for row in baseline_rows]
    baseline_wins = sum(row["baseline_status"] == "tp" for row in baseline_rows)
    baseline_total, baseline_avg = metric(baseline_values)
    summary: list[dict[str, Any]] = [
        {
            "sample": "baseline_fixed",
            "step_pct": "",
            "n": len(baseline_rows),
            "n_signals": len(baseline_rows),
            "n_resolved": len(baseline_rows),
            "n_tp_branch": sum(row["baseline_status"] == "tp" for row in baseline_rows),
            "n_unresolved": 0,
            "baseline_total_r": baseline_total,
            "baseline_avg_r": baseline_avg,
            "total_r": baseline_total,
            "avg_r": baseline_avg,
            "wr_pct": 100.0 * baseline_wins / len(baseline_rows),
            "tp_reach_pct": 100.0 * baseline_wins / len(baseline_rows),
            "trail_exit_n": 0,
            "floor_exit_n": 0,
            "sample_status": "ready",
        }
    ]
    bootstrap: list[dict[str, Any]] = []
    for step in steps:
        selected = [row for row in rows if row["step_pct"] == step]
        resolved = [
            row
            for row in selected
            if row["total_r"] != "" and not row["coverage_error"]
        ]
        values = [as_float(row["total_r"], "total_r") for row in resolved]
        total, avg = metric(values)
        wins = sum(value > 0 for value in values)
        tp_branch = sum(row["partial_branch"] == "tp_branch" for row in selected)
        unresolved = len(selected) - len(resolved)
        summary.append(
            {
                "sample": "partial_tp50",
                "step_pct": step,
                "n": len(resolved),
                "n_signals": len(selected),
                "n_resolved": len(resolved),
                "n_tp_branch": tp_branch,
                "n_unresolved": unresolved,
                "baseline_total_r": baseline_total,
                "baseline_avg_r": baseline_avg,
                "total_r": total,
                "avg_r": avg,
                "wr_pct": 100.0 * wins / len(values) if values else None,
                "tp_reach_pct": 100.0 * tp_branch / len(selected),
                "trail_exit_n": sum(
                    row["outcome"] == "partial_trail_stop" for row in selected
                ),
                "floor_exit_n": sum(
                    row["outcome"] == "partial_tp_floor" for row in selected
                ),
                "sample_status": "ready" if not unresolved else "censored_at_cutoff",
            }
        )
        deltas = [
            as_float(row["total_r"], "total_r") - as_float(row["baseline_r"], "baseline_r")
            for row in resolved
        ]
        delta, ci_low, ci_high = paired_mean_ci(
            deltas,
            iterations=bootstrap_iterations,
            seed=bootstrap_seed + int(step * 10),
        )
        bootstrap.append(
            {
                "strategy": STRATEGY,
                "step_pct": step,
                "n_unique_signals": len(resolved),
                "n_unresolved": unresolved,
                "delta_avg_r": delta,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "ci_width": ci_high - ci_low
                if math.isfinite(ci_low) and math.isfinite(ci_high)
                else None,
                "ci_crosses_zero": (
                    ci_low <= 0.0 <= ci_high
                    if math.isfinite(ci_low) and math.isfinite(ci_high)
                    else None
                ),
                "bootstrap_iterations": bootstrap_iterations,
                "bootstrap_seed": bootstrap_seed + int(step * 10),
            }
        )
    return summary, bootstrap


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str] | None = None):
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    path: Path,
    report: dict[str, Any],
) -> None:
    lines = [
        "# Partial TP 50% + trailing on remainder",
        "",
        "**Read-only in-sample analysis. Production, SQLite, demo positions, and forward-shadow state were not changed.**",
        "",
        "The sample is frozen from #136/#150 and candle processing stops at the original frozen artifact cutoff. A baseline SL signal never enters the partial branch. A baseline TP signal closes 50% at TP and trails the other 50% with a hard TP floor.",
        "",
        "The TP-trigger candle is not reused to update the trailing stop; trailing updates begin on the next completed 5m candle. Stop-first semantics are used when a candle touches the current stop.",
        "",
        "## Coverage and baseline",
        "",
        f"- Frozen target: `{report['coverage']['frozen_target_ids']}` unique `{STRATEGY}` signals.",
        f"- Baseline TP reach: `{report['coverage']['baseline_tp_n']}/{report['coverage']['frozen_target_ids']}` ({report['coverage']['baseline_tp_reach_pct']:.4f}%).",
        f"- Historical candle cutoff: `{report['coverage']['frozen_source_generated_utc']}`.",
        f"- Symbols requested/loaded: `{report['coverage']['symbols_requested']}/{report['coverage']['symbols_loaded']}`.",
        "",
        "## Grid summary",
        "",
        "| Sample | Step | n resolved | Signals | TP branch | Unresolved | ΣR | avg R | WR | TP reach | Trail exits | Floor exits | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["summary"]:
        def fmt(value: Any) -> str:
            return "" if value is None else f"{value:.6f}" if isinstance(value, float) else str(value)

        lines.append(
            "| "
            + " | ".join(
                (
                    str(row["sample"]),
                    str(row["step_pct"]),
                    str(row["n"]),
                    str(row["n_signals"]),
                    str(row["n_tp_branch"]),
                    str(row["n_unresolved"]),
                    fmt(row["total_r"]),
                    fmt(row["avg_r"]),
                    f"{row['wr_pct']:.4f}%" if row["wr_pct"] is not None else "",
                    f"{row['tp_reach_pct']:.4f}%",
                    str(row["trail_exit_n"]),
                    str(row["floor_exit_n"]),
                    str(row["sample_status"]),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Paired bootstrap CI",
            "",
            "The paired delta is partial-TP total R minus the fixed baseline R for the same signal. Resampling is by unique signal ID. Unresolved-at-cutoff rows are excluded from the realized paired metric and remain visible in `n_unresolved`.",
            "",
            "| Step | n paired | Unresolved | Δavg R | 95% CI | Width | Crosses 0 |",
            "|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in report["bootstrap"]:
        lines.append(
            "| "
            + " | ".join(
                (
                    f"{row['step_pct']:g}%",
                    str(row["n_unique_signals"]),
                    str(row["n_unresolved"]),
                    "" if row["delta_avg_r"] != row["delta_avg_r"] else f"{row['delta_avg_r']:.6f}",
                    (
                        ""
                        if row["ci_low"] != row["ci_low"]
                        else f"[{row['ci_low']:.6f}, {row['ci_high']:.6f}]"
                    ),
                    "" if row["ci_width"] is None else f"{row['ci_width']:.6f}",
                    "" if row["ci_crosses_zero"] is None else ("yes" if row["ci_crosses_zero"] else "no"),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- This is an in-sample simulation and does not justify production enablement.",
            "- Positions whose second-half trailing exit was not observed by the frozen cutoff are censored, not silently treated as profitable or as TP exits.",
            "- Historical 5m OHLC cannot resolve intrabar ordering beyond the conservative stop-first rule.",
            "- No fees or slippage are added beyond the source #136 R semantics.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    db_path: Path,
    source_dir: Path,
    output_dir: Path,
    *,
    workers: int = 3,
    bootstrap_iterations: int = 20_000,
    bootstrap_seed: int = 20260826,
) -> dict[str, Any]:
    if source_dir.resolve() == output_dir.resolve():
        raise ValueError("Refusing to overwrite frozen source directory")
    positions, coverage, source_by_id = frozen_positions(db_path, source_dir)
    cutoff_ts = coverage["cutoff_ts"]
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
            row["source_baseline_outcome"] = source_by_id[row["id"]]["baseline_outcome"]
            if str(position["symbol"]) in failures and position["status"] == "tp":
                row["coverage_error"] = "symbol_fetch_failure"
                row["outcome"] = "unresolved"
                row["total_r"] = ""
            simulation_rows.append(row)

    summary, bootstrap = summarize(
        simulation_rows,
        STEPS,
        bootstrap_iterations,
        bootstrap_seed,
    )
    baseline_tp_n = sum(row["status"] == "tp" for row in positions)
    coverage.update(
        {
            "symbols_requested": len(
                {str(row["symbol"]) for row in positions}
            ),
            "symbols_loaded": len(paths),
            "symbol_fetch_failures": failures,
            "cached_symbol_paths": cached_count,
            "baseline_tp_n": baseline_tp_n,
            "baseline_sl_n": len(positions) - baseline_tp_n,
            "baseline_tp_reach_pct": 100.0 * baseline_tp_n / len(positions),
            "simulation_rows": len(simulation_rows),
            "simulation_unique_ids": len(positions),
            "simulation_cutoff_utc": datetime.fromtimestamp(
                cutoff_ts, timezone.utc
            ).isoformat(),
        }
    )
    report = {
        "config": {
            "analysis": "partial_tp50_trailing_with_tp_floor",
            "strategy": STRATEGY,
            "partial_close_fraction": POSITION_FRACTION,
            "trailing_floor": "tp",
            "steps_pct": list(STEPS),
            "path_interval": PATH_INTERVAL,
            "tp_trigger_candle_not_reused": True,
            "stop_first_on_current_stop": True,
            "read_only": True,
            "uses_forward_window": False,
            "in_sample": True,
        },
        "coverage": coverage,
        "summary": summary,
        "bootstrap": bootstrap,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_fields = (
        "id",
        "strategy",
        "symbol",
        "direction",
        "baseline_status",
        "baseline_r",
        "source_baseline_outcome",
        "step_pct",
        "tp_reached",
        "tp_trigger_ts",
        "tp_trigger_price",
        "partial_branch",
        "post_tp_candles",
        "trajectory_updates_json",
        "trail_ts",
        "trail_price",
        "second_half_r",
        "total_r",
        "outcome",
        "coverage_error",
    )
    write_csv(output_dir / "audit.csv", simulation_rows, audit_fields)
    write_csv(output_dir / "grid_summary.csv", summary)
    write_csv(output_dir / "paired_bootstrap.csv", bootstrap)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "analysis.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("outcome_trailing_stop"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outcome_partial_tp50_trailing"),
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--bootstrap-iterations", type=int, default=20_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260826)
    args = parser.parse_args()
    report = run_analysis(
        args.db,
        args.source,
        args.out,
        workers=args.workers,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    print(
        json.dumps(
            {
                "output": str(args.out),
                "strategy": STRATEGY,
                "frozen_target_ids": report["coverage"]["frozen_target_ids"],
                "baseline_tp_n": report["coverage"]["baseline_tp_n"],
                "symbols_loaded": report["coverage"]["symbols_loaded"],
                "symbols_failed": len(report["coverage"]["symbol_fetch_failures"]),
                "simulation_rows": report["coverage"]["simulation_rows"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())