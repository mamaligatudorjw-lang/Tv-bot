#!/usr/bin/env python3
"""Paired bootstrap stability check for the WR35 trailing-stop analysis.

This is a read-only follow-up over the frozen #150 artifacts. It resamples
paired per-signal differences, never the seven correlated step rows for a
signal, and does not access SQLite or any production code.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from pathlib import Path
from typing import Any, Sequence


STEPS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
SELECTED_STEPS = {
    "overheated_24h": 8.0,
    "ema_cross_confirmed": 6.0,
}
BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20260826


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def as_float(value: str, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Invalid non-finite {field}: {value!r}")
    return result


def quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    weight = position - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def paired_mean_ci(
    differences: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    """Return observed mean and a percentile CI for paired per-signal deltas."""
    if not differences:
        return float("nan"), float("nan"), float("nan")
    values = list(differences)
    observed = sum(values) / len(values)
    rng = random.Random(seed)
    n = len(values)
    estimates = [
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(iterations)
    ]
    estimates.sort()
    return observed, quantile(estimates, 0.025), quantile(estimates, 0.975)


def load_inputs(
    decisions_path: Path,
    rows_path: Path,
) -> tuple[dict[int, dict[str, str]], dict[tuple[int, float], dict[str, str]]]:
    decisions = read_csv(decisions_path)
    decision_by_id: dict[int, dict[str, str]] = {}
    for row in decisions:
        signal_id = as_int(row.get("id", ""), "decision id")
        if signal_id in decision_by_id:
            raise ValueError(f"Duplicate signal decision for id={signal_id}")
        decision_by_id[signal_id] = row

    simulation_by_key: dict[tuple[int, float], dict[str, str]] = {}
    for row in read_csv(rows_path):
        signal_id = as_int(row.get("id", ""), "simulation id")
        step = as_float(row.get("step_pct", ""), "step_pct")
        key = (signal_id, step)
        if key in simulation_by_key:
            raise ValueError(f"Duplicate simulation row for id={signal_id}, step={step}")
        simulation_by_key[key] = row
    return decision_by_id, simulation_by_key


def interval_overlaps(
    left_low: float,
    left_high: float,
    right_low: float,
    right_high: float,
) -> bool:
    return max(left_low, right_low) <= min(left_high, right_high)


def build_report(
    decision_by_id: dict[int, dict[str, str]],
    simulation_by_key: dict[tuple[int, float], dict[str, str]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    strategies = sorted(SELECTED_STEPS)
    rows: list[dict[str, Any]] = []
    for strategy in strategies:
        filtered_ids = sorted(
            signal_id
            for signal_id, decision in decision_by_id.items()
            if decision.get("strategy") == strategy
            and decision.get("filter_pass") == "yes"
        )
        strategy_rows: list[dict[str, Any]] = []
        for step_index, step in enumerate(STEPS):
            differences: list[float] = []
            baseline_values: list[float] = []
            trailing_values: list[float] = []
            for signal_id in filtered_ids:
                simulation = simulation_by_key.get((signal_id, step))
                if simulation is None:
                    raise ValueError(
                        f"Missing simulation row for filtered id={signal_id}, step={step}"
                    )
                baseline = as_float(simulation.get("baseline_r", ""), "baseline_r")
                trailing = as_float(simulation.get("alt_r", ""), "alt_r")
                baseline_values.append(baseline)
                trailing_values.append(trailing)
                differences.append(trailing - baseline)

            step_seed = seed + (strategies.index(strategy) * len(STEPS)) + step_index
            delta, ci_low, ci_high = paired_mean_ci(
                differences,
                iterations=iterations,
                seed=step_seed,
            )
            strategy_rows.append(
                {
                    "strategy": strategy,
                    "step_pct": step,
                    "n_unique_signals": len(filtered_ids),
                    "baseline_avg_r": sum(baseline_values) / len(baseline_values),
                    "trailing_avg_r": sum(trailing_values) / len(trailing_values),
                    "delta_avg_r": delta,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "ci_width": ci_high - ci_low,
                    "ci_crosses_zero": ci_low <= 0.0 <= ci_high,
                    "selected_step": step == SELECTED_STEPS[strategy],
                    "bootstrap_iterations": iterations,
                    "bootstrap_seed": step_seed,
                }
            )

        for index, row in enumerate(strategy_rows):
            neighbors = []
            if index > 0:
                neighbors.append(strategy_rows[index - 1])
            if index + 1 < len(strategy_rows):
                neighbors.append(strategy_rows[index + 1])
            row["neighbor_steps_pct"] = ",".join(
                f"{neighbor['step_pct']:g}" for neighbor in neighbors
            )
            row["neighbor_ci_overlap"] = any(
                interval_overlaps(
                    row["ci_low"],
                    row["ci_high"],
                    neighbor["ci_low"],
                    neighbor["ci_high"],
                )
                for neighbor in neighbors
            )
            row["same_sign_as_neighbors"] = all(
                row["delta_avg_r"] * neighbor["delta_avg_r"] > 0
                for neighbor in neighbors
            )
            rows.extend([row])

    selected = {}
    for strategy, step in SELECTED_STEPS.items():
        selected_row = next(
            row
            for row in rows
            if row["strategy"] == strategy and row["step_pct"] == step
        )
        selected[strategy] = {
            "step_pct": step,
            "ci_crosses_zero": selected_row["ci_crosses_zero"],
            "neighbor_ci_overlap": selected_row["neighbor_ci_overlap"],
            "same_sign_as_neighbors": selected_row["same_sign_as_neighbors"],
            "forward_recommendation": (
                "do_not_spend_forward_ci_crosses_zero"
                if selected_row["ci_crosses_zero"]
                else "do_not_spend_forward_not_distinct_from_neighbors"
                if selected_row["neighbor_ci_overlap"]
                else "candidate_for_forward"
            ),
        }

    return {
        "analysis": "paired_signal_level_bootstrap_wr35_trailing_stability",
        "read_only": True,
        "uses_forward_window": False,
        "resampling_unit": "unique signal ID",
        "paired_value": "trailing alt_r - filtered fixed baseline_r",
        "confidence_level": 0.95,
        "bootstrap_method": "percentile bootstrap of paired per-signal deltas",
        "bootstrap_iterations": iterations,
        "base_seed": seed,
        "source": {
            "signal_decisions": "signal_filter_decisions.csv",
            "trailing_rows": "trailing_rows_wr35.csv",
            "source_trailing_rows": len(simulation_by_key),
            "source_unique_signal_ids": len(decision_by_id),
        },
        "selected_step_recommendations": selected,
        "results": rows,
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fields = (
        "strategy",
        "step_pct",
        "n_unique_signals",
        "baseline_avg_r",
        "trailing_avg_r",
        "delta_avg_r",
        "ci_low",
        "ci_high",
        "ci_width",
        "ci_crosses_zero",
        "selected_step",
        "neighbor_steps_pct",
        "neighbor_ci_overlap",
        "same_sign_as_neighbors",
        "bootstrap_iterations",
        "bootstrap_seed",
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def display(value: Any, digits: int = 6) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# WR≥35% trailing paired bootstrap stability",
        "",
        "**Read-only descriptive analysis over the frozen #150 artifacts.**",
        "No SQLite, production logic, or forward window was used.",
        "",
        "## Method",
        "",
        "- Paired value: `trailing alt_r - filtered fixed baseline_r`.",
        "- Resampling unit: one unique signal ID; the seven step rows for one signal are never sampled independently.",
        "- CI: 95% percentile bootstrap, 20,000 iterations per strategy/step.",
        "- This does not correct the best-of-7 selection bias; it reports every step and compares the selected step descriptively with adjacent steps.",
        "",
        "## Results",
        "",
        "| Strategy | Step | n signals | Δavg R | 95% CI | Width | Crosses 0 | Neighbors overlap | Same sign |",
        "|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in report["results"]:
        lines.append(
            "| "
            + " | ".join(
                (
                    str(row["strategy"]),
                    f"{row['step_pct']:g}%",
                    str(row["n_unique_signals"]),
                    display(row["delta_avg_r"]),
                    f"[{display(row['ci_low'])}, {display(row['ci_high'])}]",
                    display(row["ci_width"]),
                    "yes" if row["ci_crosses_zero"] else "no",
                    "yes" if row["neighbor_ci_overlap"] else "no",
                    "yes" if row["same_sign_as_neighbors"] else "no",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Selected-step gate",
            "",
            "A selected step is not recommended for the new forward window when its CI crosses zero or its CI overlaps an adjacent step. The neighbor comparison is descriptive, not a multiple-comparison correction.",
            "",
            "| Strategy | Selected step | CI crosses 0 | Neighbor CI overlap | Recommendation |",
            "|---|---:|---|---|---|",
        ]
    )
    for strategy, recommendation in report["selected_step_recommendations"].items():
        lines.append(
            "| "
            + " | ".join(
                (
                    strategy,
                    f"{recommendation['step_pct']:g}%",
                    "yes" if recommendation["ci_crosses_zero"] else "no",
                    "yes" if recommendation["neighbor_ci_overlap"] else "no",
                    recommendation["forward_recommendation"],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation guardrail",
            "",
            "A positive CI on a selected in-sample step is not evidence that the step generalizes: the step was selected from seven candidates. Confidence in a forward candidate requires both a CI above zero and a meaningful separation from neighboring steps under this descriptive check.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    input_dir: Path,
    output_dir: Path,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError("Refusing to overwrite the source artifact directory")
    decisions_path = input_dir / "signal_filter_decisions.csv"
    rows_path = input_dir / "trailing_rows_wr35.csv"
    decisions, simulation_rows = load_inputs(decisions_path, rows_path)
    report = build_report(
        decisions,
        simulation_rows,
        iterations=iterations,
        seed=seed,
    )
    report["source"]["signal_decisions"] = str(decisions_path)
    report["source"]["trailing_rows"] = str(rows_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "paired_bootstrap.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_csv(output_dir / "paired_bootstrap.csv", report["results"])
    write_markdown(output_dir / "paired_bootstrap.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("outcome_trailing_stop_wr35"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outcome_trailing_stop_wr35/bootstrap"),
    )
    parser.add_argument("--iterations", type=int, default=BOOTSTRAP_ITERATIONS)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    args = parser.parse_args()
    report = run_analysis(
        args.input,
        args.out,
        iterations=args.iterations,
        seed=args.seed,
    )
    print(
        json.dumps(
            {
                "output": str(args.out),
                "resampling_unit": report["resampling_unit"],
                "results": len(report["results"]),
                "selected_step_recommendations": report[
                    "selected_step_recommendations"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())