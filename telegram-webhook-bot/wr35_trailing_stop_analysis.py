#!/usr/bin/env python3
"""Read-only in-sample trailing-stop analysis with a dynamic WR gate.

The input is intentionally the frozen output of trailing_stop_analysis.py.
Only signal IDs present in that output are loaded from the database; current
rows added after the original grid search cannot silently enter this report.
No candles are fetched, no production module is imported, and SQLite is only
read.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

TARGET_STRATEGIES = ("overheated_24h", "ema_cross_confirmed")
WR_THRESHOLD = 0.35
MIN_COHORT_N = 20
DEFAULT_STEPS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
SIMULATION_FILENAME = "trailing_rows.csv"
REGIME_FILENAME = "signal_regimes.csv"
COVERAGE_FILENAME = "coverage.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer {field}={value!r}") from exc


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _fmt_ts(timestamp: int | None) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        if timestamp is not None
        else ""
    )


def load_frozen_simulations(
    input_dir: Path,
    strategies: Sequence[str] = TARGET_STRATEGIES,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Load only target rows from the already-generated #136 simulation."""
    coverage_path = input_dir / COVERAGE_FILENAME
    simulation_path = input_dir / SIMULATION_FILENAME
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    source_rows = read_csv(simulation_path)
    wanted = set(strategies)
    rows = [row for row in source_rows if row.get("strategy") in wanted]
    if not rows:
        raise ValueError("Frozen trailing output contains no target strategy rows")

    ids = {row.get("id", "") for row in rows}
    if "" in ids:
        raise ValueError("Frozen trailing output contains a row without an id")
    raw_steps = {_as_float(row.get("step_pct")) for row in rows}
    if any(step is None for step in raw_steps):
        raise ValueError("Frozen trailing output contains an invalid step_pct")
    steps = sorted(step for step in raw_steps if step is not None)
    expected_steps = {
        float(step)
        for step in coverage.get("steps_pct", list(DEFAULT_STEPS))
    }
    actual_steps = {float(step) for step in steps if step is not None}
    if actual_steps != expected_steps:
        raise ValueError(
            "Frozen trailing output steps do not match coverage.json: "
            f"rows={sorted(actual_steps)}, coverage={sorted(expected_steps)}"
        )

    keys = {(row.get("id"), row.get("step_pct")) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("Frozen trailing output contains duplicate id/step rows")
    coverage = dict(coverage)
    coverage.update(
        {
            "frozen_source": str(simulation_path),
            "frozen_source_generated_utc": coverage.get("generated_utc"),
            "frozen_source_rows_all": len(source_rows),
            "frozen_target_rows": len(rows),
            "frozen_target_ids": len(ids),
            "frozen_target_strategies": sorted(wanted),
            "frozen_steps_pct": sorted(actual_steps),
        }
    )
    return rows, coverage


def load_positions(
    db_path: Path,
    ids: Iterable[int],
    strategies: Sequence[str] = TARGET_STRATEGIES,
) -> dict[int, dict[str, Any]]:
    """Read the frozen positions from SQLite and reject missing or mismatched rows."""
    sorted_ids = sorted({int(value) for value in ids})
    if not sorted_ids:
        return {}
    connection = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in sorted_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
                   status, ts_close, exit_price, alert_type, is_shadow
              FROM demo_positions
             WHERE id IN ({placeholders})
            """,
            sorted_ids,
        )
        positions = {int(row["id"]): dict(row) for row in rows}
    finally:
        connection.close()
    missing = set(sorted_ids) - set(positions)
    if missing:
        raise ValueError(
            f"Frozen input references {len(missing)} missing demo_positions IDs: "
            f"{sorted(missing)[:10]}"
        )
    wanted = set(strategies)
    mismatched = {
        position_id: row.get("alert_type")
        for position_id, row in positions.items()
        if row.get("alert_type") not in wanted
        or row.get("status") not in ("tp", "sl")
        or row.get("ts_close") is None
    }
    if mismatched:
        raise ValueError(
            "Frozen IDs do not resolve to the expected target positions: "
            f"{dict(list(mismatched.items())[:10])}"
        )
    return positions


def load_regimes(
    regime_path: Path,
    ids: Iterable[int],
) -> dict[int, dict[str, str]]:
    """Load the saved lookahead-safe regime snapshot for frozen IDs only."""
    wanted = {int(value) for value in ids}
    source = read_csv(regime_path)
    regimes: dict[int, dict[str, str]] = {}
    for row in source:
        if not row.get("id"):
            continue
        row_id = _as_int(row["id"], "regime id")
        if row_id not in wanted:
            continue
        if row_id in regimes:
            raise ValueError(f"Duplicate regime snapshot for id={row_id}")
        regimes[row_id] = {
            "trend_regime": str(row.get("trend_regime") or "unknown"),
            "regime_reason": str(row.get("regime_reason") or "missing"),
            "btc_candle_ts": str(row.get("btc_candle_ts") or ""),
            "btc_close": str(row.get("btc_close") or ""),
            "btc_ema50": str(row.get("btc_ema50") or ""),
        }
    missing = wanted - set(regimes)
    if missing:
        raise ValueError(
            f"Frozen input has {len(missing)} IDs without regime snapshots: "
            f"{sorted(missing)[:10]}"
        )
    for regime in regimes.values():
        if regime["trend_regime"] not in ("bull", "bear", "unknown"):
            regime["trend_regime"] = "unknown"
    return regimes


def cohort_key(
    strategy: str,
    direction: str,
    regime: str,
) -> tuple[str, str, str]:
    return strategy, direction, regime or "unknown"


def _sort_key(row: dict[str, Any]) -> tuple[int, int]:
    return _as_int(row["ts_open"], "ts_open"), _as_int(row["id"], "id")


def build_filter_decisions(
    positions: Sequence[dict[str, Any]],
    regimes: dict[int, dict[str, str]],
    *,
    threshold: float = WR_THRESHOLD,
    minimum_n: int = MIN_COHORT_N,
) -> list[dict[str, Any]]:
    """Apply the dynamic, history-only WR gate to each frozen signal.

    A prior result is eligible only when its position ordering precedes the
    current signal and its close is known by the current signal timestamp.
    The current signal is appended to history only after its own decision.
    """
    ordered = sorted(positions, key=_sort_key)
    history: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    decisions: list[dict[str, Any]] = []
    for position in ordered:
        position_id = _as_int(position["id"], "id")
        strategy = str(position.get("alert_type") or "unknown")
        direction = str(position["direction"])
        regime = str(regimes[position_id].get("trend_regime") or "unknown")
        key = cohort_key(strategy, direction, regime)
        current_order = _sort_key(position)
        signal_ts = _as_int(position["ts_open"], "ts_open")
        prior = [
            item
            for item in history[key]
            if item["order"] < current_order
            and item["ts_close"] is not None
            and item["ts_close"] <= signal_ts
        ]
        prior_n = len(prior)
        prior_wins = sum(item["status"] == "tp" for item in prior)
        prior_losses = sum(item["status"] == "sl" for item in prior)
        historical_wr = prior_wins / prior_n if prior_n else None
        if prior_n < minimum_n:
            reason = "insufficient_history" if prior_n else "no_history"
            passes = False
        elif historical_wr is not None and historical_wr >= threshold:
            reason = "passes"
            passes = True
        else:
            reason = "below_threshold"
            passes = False
        decisions.append(
            {
                "id": position_id,
                "strategy": strategy,
                "direction": direction,
                "regime": regime,
                "cohort": "|".join(key),
                "prior_cohort_n": prior_n,
                "prior_cohort_wins": prior_wins,
                "prior_cohort_losses": prior_losses,
                "historical_wr_pct": (
                    round(100.0 * historical_wr, 4)
                    if historical_wr is not None
                    else None
                ),
                "filter_pass": "yes" if passes else "no",
                "filter_reason": reason,
            }
        )
        history[key].append(
            {
                "order": current_order,
                "ts_close": (
                    _as_int(position["ts_close"], "ts_close")
                    if position.get("ts_close") is not None
                    else None
                ),
                "status": str(position["status"]),
            }
        )
    return decisions


def annotate_simulations(
    simulation_rows: Sequence[dict[str, str]],
    decisions: Sequence[dict[str, Any]],
    positions: dict[int, dict[str, Any]],
    regimes: dict[int, dict[str, str]],
) -> list[dict[str, Any]]:
    decision_by_id = {int(row["id"]): row for row in decisions}
    output: list[dict[str, Any]] = []
    for source in simulation_rows:
        position_id = _as_int(source["id"], "simulation id")
        if position_id not in decision_by_id:
            raise ValueError(f"No WR decision for frozen simulation id={position_id}")
        position = positions[position_id]
        decision = decision_by_id[position_id]
        row: dict[str, Any] = dict(source)
        row.update(
            {
                "ts_open": int(position["ts_open"]),
                "ts_open_utc": _fmt_ts(int(position["ts_open"])),
                "ts_close": int(position["ts_close"]),
                "ts_close_utc": _fmt_ts(int(position["ts_close"])),
                "regime": decision["regime"],
                "regime_reason": regimes[position_id]["regime_reason"],
                "cohort": decision["cohort"],
                "prior_cohort_n": decision["prior_cohort_n"],
                "prior_cohort_wins": decision["prior_cohort_wins"],
                "prior_cohort_losses": decision["prior_cohort_losses"],
                "historical_wr_pct": decision["historical_wr_pct"],
                "filter_pass": decision["filter_pass"],
                "filter_reason": decision["filter_reason"],
            }
        )
        output.append(row)
    return output


def _metric_values(
    rows: Sequence[dict[str, Any]],
    value_field: str,
) -> list[float]:
    values = [_as_float(row.get(value_field)) for row in rows]
    return [value for value in values if value is not None]


def _metrics(
    rows: Sequence[dict[str, Any]],
    *,
    sample: str,
    strategy: str,
    step_pct: float | None = None,
) -> dict[str, Any]:
    usable = [row for row in rows if not str(row.get("coverage_error") or "")]
    baseline = _metric_values(usable, "baseline_r")
    alt = _metric_values(usable, "alt_r")
    baseline_total = sum(baseline) if baseline else None
    alt_total = sum(alt) if alt else None
    baseline_wr = (
        100.0 * sum(row.get("baseline_outcome") == "tp" for row in usable) / len(usable)
        if usable
        else None
    )
    alt_wr = (
        100.0 * sum(value > 0 for value in alt) / len(alt)
        if alt
        else None
    )
    result = {
        "sample": sample,
        "strategy": strategy,
        "step_pct": step_pct,
        "n": len(usable),
        "n_signals": len(rows),
        "n_usable": len(usable),
        "n_coverage_error": len(rows) - len(usable),
        "baseline_total_r": _round_or_none(baseline_total, 6),
        "alt_total_r": _round_or_none(alt_total, 6),
        "delta_total_r": _round_or_none(
            alt_total - baseline_total
            if alt_total is not None and baseline_total is not None
            else None,
            6,
        ),
        "baseline_avg_r": _round_or_none(
            baseline_total / len(baseline) if baseline else None, 6
        ),
        "alt_avg_r": _round_or_none(alt_total / len(alt) if alt else None, 6),
        "delta_avg_r": _round_or_none(
            alt_total / len(alt) - baseline_total / len(baseline)
            if alt and baseline
            else None,
            6,
        ),
        "baseline_wr_pct": _round_or_none(baseline_wr, 4),
        "alt_wr_pct": _round_or_none(alt_wr, 4),
        "alt_positive_n": sum(value > 0 for value in alt),
        "alt_non_positive_n": sum(value <= 0 for value in alt),
        "trail_exit_n": sum(
            row.get("alt_outcome") == "trail_stop" for row in usable
        ),
        "sample_status": "ready" if len(usable) >= MIN_COHORT_N else "insufficient",
    }
    return result


def build_grid_summary(
    rows: Sequence[dict[str, Any]],
    steps: Sequence[float],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for strategy in TARGET_STRATEGIES:
        for step in sorted(float(value) for value in steps):
            for sample, selected in (
                ("all", [row for row in rows if row["strategy"] == strategy]),
                (
                    "filtered",
                    [
                        row
                        for row in rows
                        if row["strategy"] == strategy
                        and row["filter_pass"] == "yes"
                    ],
                ),
            ):
                selected_step = [
                    row
                    for row in selected
                    if _as_float(row.get("step_pct")) == step
                ]
                output.append(
                    _metrics(
                        selected_step,
                        sample=sample,
                        strategy=strategy,
                        step_pct=step,
                    )
                )
    return output


def build_cohort_summary(
    decisions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        grouped[
            (
                decision["strategy"],
                decision["direction"],
                decision["regime"],
            )
        ].append(decision)
    pairs = {
        (strategy, direction)
        for strategy in TARGET_STRATEGIES
        for direction in ("LONG", "SHORT")
    }
    observed_unknown = {
        (item["strategy"], item["direction"])
        for item in decisions
        if item["regime"] == "unknown"
    }
    expected_keys = {
        (strategy, direction, regime)
        for strategy, direction in pairs
        for regime in ("bull", "bear")
    }
    expected_keys.update(
        (strategy, direction, "unknown")
        for strategy, direction in observed_unknown
    )
    output: list[dict[str, Any]] = []
    for strategy, direction, regime in sorted(expected_keys):
        items = grouped[(strategy, direction, regime)]
        reasons = Counter(str(item["filter_reason"]) for item in items)
        n_total = len(items)
        n_passed = sum(item["filter_pass"] == "yes" for item in items)
        output.append(
            {
                "strategy": strategy,
                "direction": direction,
                "regime": regime,
                "cohort": "|".join((strategy, direction, regime)),
                "n_total": n_total,
                "n_passed": n_passed,
                "n_excluded": n_total - n_passed,
                "pass_pct": _round_or_none(
                    100.0 * n_passed / n_total if n_total else None,
                    4,
                ),
                "no_history_n": reasons["no_history"],
                "insufficient_history_n": reasons["insufficient_history"],
                "below_threshold_n": reasons["below_threshold"],
                "sample_status": "ready" if n_total >= MIN_COHORT_N else "insufficient",
            }
        )
    return output


def build_filter_effect(
    rows: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    decision_by_id = {int(row["id"]): row for row in decisions}
    output: list[dict[str, Any]] = []
    for strategy in TARGET_STRATEGIES:
        signal_rows = [
            row
            for row in rows
            if row["strategy"] == strategy and float(row["step_pct"]) == 2.0
        ]
        filtered = [
            row
            for row in signal_rows
            if decision_by_id[int(row["id"])]["filter_pass"] == "yes"
        ]
        all_metrics = _metrics(signal_rows, sample="all", strategy=strategy)
        filtered_metrics = _metrics(
            filtered,
            sample="filtered",
            strategy=strategy,
        )
        output.append(
            {
                "strategy": strategy,
                "n_all": all_metrics["n_signals"],
                "n_filtered": filtered_metrics["n_signals"],
                "n_excluded": all_metrics["n_signals"] - filtered_metrics["n_signals"],
                "all_baseline_total_r": all_metrics["baseline_total_r"],
                "all_baseline_avg_r": all_metrics["baseline_avg_r"],
                "all_baseline_wr_pct": all_metrics["baseline_wr_pct"],
                "filtered_baseline_total_r": filtered_metrics["baseline_total_r"],
                "filtered_baseline_avg_r": filtered_metrics["baseline_avg_r"],
                "filtered_baseline_wr_pct": filtered_metrics["baseline_wr_pct"],
                "filter_baseline_avg_r_delta": _round_or_none(
                    (
                        filtered_metrics["baseline_avg_r"]
                        - all_metrics["baseline_avg_r"]
                        if filtered_metrics["baseline_avg_r"] is not None
                        and all_metrics["baseline_avg_r"] is not None
                        else None
                    ),
                    6,
                ),
                "filter_baseline_wr_delta_pp": _round_or_none(
                    (
                        filtered_metrics["baseline_wr_pct"]
                        - all_metrics["baseline_wr_pct"]
                        if filtered_metrics["baseline_wr_pct"] is not None
                        and all_metrics["baseline_wr_pct"] is not None
                        else None
                    ),
                    4,
                ),
                "selection_effect_note": (
                    "descriptive filtered-vs-all comparison; not causal"
                ),
            }
        )
    return output


def write_csv(
    path: Path,
    rows: Sequence[dict[str, Any]],
    fields: Sequence[str] | None = None,
) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: "" if row.get(field) is None else row.get(field, "")
                    for field in fields
                }
            )


def _grid_table(rows: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| Sample | Strategy | Step | n | Baseline avg R | Trailing avg R | Δ avg R | "
        "Baseline WR | Trailing WR | Trail exits | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        def display(value: Any, suffix: str = "") -> str:
            return "—" if value is None or value == "" else f"{value}{suffix}"

        lines.append(
            f"| {row['sample']} | {row['strategy']} | {row['step_pct']}% | "
            f"{row['n_usable']}/{row['n_signals']} | "
            f"{display(row['baseline_avg_r'])} | {display(row['alt_avg_r'])} | "
            f"{display(row['delta_avg_r'])} | {display(row['baseline_wr_pct'], '%')} | "
            f"{display(row['alt_wr_pct'], '%')} | {row['trail_exit_n']} | "
            f"{row['sample_status']} |"
        )
    return lines


def write_report(
    output_dir: Path,
    coverage: dict[str, Any],
    decisions: Sequence[dict[str, Any]],
    simulation_rows: Sequence[dict[str, Any]],
    cohort_summary: Sequence[dict[str, Any]],
    filter_effect: Sequence[dict[str, Any]],
    grid_summary: Sequence[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    decision_fields = (
        "id",
        "strategy",
        "direction",
        "regime",
        "regime_reason",
        "cohort",
        "prior_cohort_n",
        "prior_cohort_wins",
        "prior_cohort_losses",
        "historical_wr_pct",
        "filter_pass",
        "filter_reason",
    )
    simulation_fields = (
        "id",
        "strategy",
        "symbol",
        "direction",
        "is_shadow",
        "ts_open",
        "ts_open_utc",
        "ts_close",
        "ts_close_utc",
        "regime",
        "regime_reason",
        "cohort",
        "prior_cohort_n",
        "prior_cohort_wins",
        "prior_cohort_losses",
        "historical_wr_pct",
        "filter_pass",
        "filter_reason",
        "step_pct",
        "range_pct",
        "range_bucket",
        "baseline_r",
        "baseline_outcome",
        "alt_r",
        "alt_outcome",
        "trail_ts",
        "trail_price",
        "coverage_error",
    )
    cohort_fields = (
        "strategy",
        "direction",
        "regime",
        "cohort",
        "n_total",
        "n_passed",
        "n_excluded",
        "pass_pct",
        "no_history_n",
        "insufficient_history_n",
        "below_threshold_n",
        "sample_status",
    )
    effect_fields = (
        "strategy",
        "n_all",
        "n_filtered",
        "n_excluded",
        "all_baseline_total_r",
        "all_baseline_avg_r",
        "all_baseline_wr_pct",
        "filtered_baseline_total_r",
        "filtered_baseline_avg_r",
        "filtered_baseline_wr_pct",
        "filter_baseline_avg_r_delta",
        "filter_baseline_wr_delta_pp",
        "selection_effect_note",
    )
    grid_fields = (
        "sample",
        "strategy",
        "step_pct",
        "n",
        "n_signals",
        "n_usable",
        "n_coverage_error",
        "baseline_total_r",
        "alt_total_r",
        "delta_total_r",
        "baseline_avg_r",
        "alt_avg_r",
        "delta_avg_r",
        "baseline_wr_pct",
        "alt_wr_pct",
        "alt_positive_n",
        "alt_non_positive_n",
        "trail_exit_n",
        "sample_status",
    )
    write_csv(output_dir / "signal_filter_decisions.csv", decisions, decision_fields)
    write_csv(output_dir / "trailing_rows_wr35.csv", simulation_rows, simulation_fields)
    write_csv(output_dir / "cohort_summary.csv", cohort_summary, cohort_fields)
    write_csv(output_dir / "filter_effect.csv", filter_effect, effect_fields)
    write_csv(output_dir / "grid_summary.csv", grid_summary, grid_fields)

    report = {
        "config": {
            "analysis": "in_sample_trailing_stop_with_dynamic_wr_filter",
            "target_strategies": list(TARGET_STRATEGIES),
            "wr_threshold_pct": WR_THRESHOLD * 100.0,
            "minimum_prior_cohort_n": MIN_COHORT_N,
            "cohort": "strategy|direction|regime",
            "regime_source": REGIME_FILENAME,
            "trailing_source": SIMULATION_FILENAME,
            "trailing_grid_source": "outcome_trailing_stop/coverage.json",
            "trailing_semantics": "frozen #136 simulation; TP remains ceiling",
            "history_rule": "same cohort only; position order precedes signal and ts_close <= signal ts_open",
            "read_only": True,
            "lookahead_safe_filter": True,
            "uses_forward_window": False,
        },
        "coverage": coverage,
        "cohort_summary": list(cohort_summary),
        "filter_effect": list(filter_effect),
        "grid_summary": list(grid_summary),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Trailing-stop on dynamic WR≥35% cohorts",
        "",
        "**Read-only in-sample analysis. Production logic, Telegram state, and SQLite were not changed.**",
        "",
        "This report applies the WR gate only to the frozen signal IDs used by the "
        "original #136 grid search. It does not load new current rows and does not "
        "reuse a forward/OOS window.",
        "",
        "## Method",
        "",
        f"- Target strategies: `{', '.join(TARGET_STRATEGIES)}`.",
        f"- Cohort: `strategy × direction × regime`.",
        f"- A signal passes only when at least {MIN_COHORT_N} same-cohort results were "
        f"already resolved and historical WR is **≥ {WR_THRESHOLD * 100:.0f}%**.",
        "- Only a preceding position with `ts_close <= current ts_open` contributes "
        "to the historical WR; the current result is appended after its decision.",
        "- Regime comes from the saved BTC 4h/EMA50 snapshot and keeps `unknown` explicit.",
        "- Trailing values are reused from the frozen #136 5m grid with the original "
        "fixed SL/TP baseline and TP ceiling.",
        "",
        "## Frozen input",
        "",
        "```json",
        json.dumps(
            {
                key: coverage[key]
                for key in (
                    "frozen_source",
                    "frozen_source_generated_utc",
                    "frozen_target_rows",
                    "frozen_target_ids",
                    "frozen_target_strategies",
                    "frozen_steps_pct",
                )
                if key in coverage
            },
            indent=2,
            ensure_ascii=False,
        ),
        "```",
        "",
        "## Filter counts by cohort",
        "",
        "| Strategy | Direction | Regime | Total | Passed | Excluded | Pass % | "
        "No history | Insufficient | Below 35% | Status |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in cohort_summary:
        lines.append(
            f"| {row['strategy']} | {row['direction']} | {row['regime']} | "
            f"{row['n_total']} | {row['n_passed']} | {row['n_excluded']} | "
            f"{row['pass_pct']}% | {row['no_history_n']} | "
            f"{row['insufficient_history_n']} | {row['below_threshold_n']} | "
            f"{row['sample_status']} |"
        )
    lines += [
        "",
        "## Filter-only effect on fixed baseline",
        "",
        "This is a descriptive selection comparison: filtered baseline versus all "
        "baseline. It is not a causal estimate because the filter selects a subset.",
        "",
        "| Strategy | All n | Filtered n | Excluded | All avg R | Filtered avg R | "
        "Δ avg R | All WR | Filtered WR | Δ WR pp |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in filter_effect:
        lines.append(
            f"| {row['strategy']} | {row['n_all']} | {row['n_filtered']} | "
            f"{row['n_excluded']} | {row['all_baseline_avg_r']} | "
            f"{row['filtered_baseline_avg_r']} | {row['filter_baseline_avg_r_delta']} | "
            f"{row['all_baseline_wr_pct']}% | {row['filtered_baseline_wr_pct']}% | "
            f"{row['filter_baseline_wr_delta_pp']} |"
        )
    lines += [
        "",
        "## Fixed baseline versus trailing on the same filtered sample",
        "",
        *_grid_table(grid_summary),
        "",
        "The `Δ avg R` and `Δ total R` columns compare trailing against the fixed "
        "baseline for exactly the same filtered signal IDs. `n` is shown as "
        "`path-usable / selected`; groups below 20 usable rows are insufficient.",
        "",
        "## Guardrails",
        "",
        "- This is an in-sample result and remains subject to grid-search selection bias.",
        "- It does not justify enabling trailing-stop in production.",
        "- No forward/shadow tracker was created and no `demo_positions` row was updated.",
        "- Small, empty, and `unknown` cohorts remain explicit rather than being hidden.",
        "",
    ]
    (output_dir / "analysis.md").write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    db_path: Path,
    input_dir: Path,
    output_dir: Path,
    regime_path: Path | None = None,
) -> dict[str, Any]:
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError("Refusing to overwrite the frozen input directory")
    simulation_rows, coverage = load_frozen_simulations(input_dir)
    frozen_ids = {_as_int(row["id"], "simulation id") for row in simulation_rows}
    positions = load_positions(db_path, frozen_ids)
    regimes = load_regimes(
        regime_path
        or input_dir.parent / "trend_regime_analysis" / REGIME_FILENAME,
        frozen_ids,
    )
    selected_positions = [
        row for row in positions.values() if str(row["alert_type"]) in TARGET_STRATEGIES
    ]
    decisions = build_filter_decisions(selected_positions, regimes)
    annotated = annotate_simulations(simulation_rows, decisions, positions, regimes)
    steps = sorted(
        {
            float(value)
            for value in coverage.get("frozen_steps_pct", DEFAULT_STEPS)
        }
    )
    grid_summary = build_grid_summary(annotated, steps)
    cohort_summary = build_cohort_summary(decisions)
    filter_effect = build_filter_effect(annotated, decisions)
    coverage = dict(coverage)
    coverage.update(
        {
            "positions_loaded": len(positions),
            "decisions": len(decisions),
            "simulation_rows_annotated": len(annotated),
            "signals_passed": sum(
                row["filter_pass"] == "yes" for row in decisions
            ),
            "signals_excluded": sum(
                row["filter_pass"] != "yes" for row in decisions
            ),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_report(
        output_dir,
        coverage,
        decisions,
        annotated,
        cohort_summary,
        filter_effect,
        grid_summary,
    )
    return {
        "coverage": coverage,
        "decisions": decisions,
        "cohort_summary": cohort_summary,
        "filter_effect": filter_effect,
        "grid_summary": grid_summary,
        "output": str(output_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only dynamic WR≥35% trailing-stop analysis"
    )
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--input", type=Path, default=Path("outcome_trailing_stop"))
    parser.add_argument(
        "--regimes",
        type=Path,
        default=Path("trend_regime_analysis") / REGIME_FILENAME,
        help="Saved lookahead-safe regime CSV.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outcome_trailing_stop_wr35"),
    )
    args = parser.parse_args()
    if not args.regimes.exists():
        raise SystemExit(f"Regime snapshot does not exist: {args.regimes}")
    result = run_analysis(args.db, args.input, args.out, regime_path=args.regimes)
    print(
        json.dumps(
            {
                "output": result["output"],
                "frozen_target_ids": result["coverage"]["frozen_target_ids"],
                "signals_passed": result["coverage"]["signals_passed"],
                "signals_excluded": result["coverage"]["signals_excluded"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
