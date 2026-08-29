#!/usr/bin/env python3
"""Read-only percentile bootstrap for confirmed-signal level cohorts."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

from wr35_trailing_bootstrap import paired_mean_ci


BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20260829
TARGET_STRATEGY = "overheated_confirmed"
TARGET_LEVELS = ("2/3", "3/3")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_float(value: str, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def as_int(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def load_target_rows(path: Path) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {level: [] for level in TARGET_LEVELS}
    seen_ids: set[int] = set()
    for row in read_csv(path):
        signal_id = as_int(row.get("id", ""), "signal id")
        if signal_id in seen_ids:
            raise ValueError(f"Duplicate signal ID in audit: {signal_id}")
        seen_ids.add(signal_id)
        if row.get("strategy") != TARGET_STRATEGY:
            continue
        level = row.get("confirmation_level")
        if level in grouped:
            grouped[level].append(row)
    return grouped


def _ci_crosses_zero(low: float, high: float) -> bool:
    return low <= 0.0 <= high


def build_report(
    grouped: dict[str, list[dict[str, str]]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    breakeven = {"2/3": 40.0, "3/3": 50.0}
    for level_index, level in enumerate(TARGET_LEVELS):
        rows = grouped.get(level, [])
        if not rows:
            raise ValueError(f"No rows found for {TARGET_STRATEGY}/{level}")
        ids = [as_int(row["id"], "signal id") for row in rows]
        result_values = [as_float(row["result_r"], "result_r") for row in rows]
        wins = sum(row.get("status") == "tp" for row in rows)
        losses = sum(row.get("status") == "sl" for row in rows)
        if wins + losses != len(rows):
            raise ValueError(f"Unexpected status in {TARGET_STRATEGY}/{level}")
        be_wr = breakeven[level]
        delta_values = [
            (100.0 if row["status"] == "tp" else 0.0) - be_wr for row in rows
        ]
        avg_r, avg_low, avg_high = paired_mean_ci(
            result_values,
            iterations=iterations,
            seed=seed + level_index,
        )
        delta, delta_low, delta_high = paired_mean_ci(
            delta_values,
            iterations=iterations,
            seed=seed + 100 + level_index,
        )
        n = len(rows)
        results.append(
            {
                "strategy": TARGET_STRATEGY,
                "confirmation_level": level,
                "rr_multiple": 1.5 if level == "2/3" else 1.0,
                "n_unique_signals": n,
                "wins": wins,
                "losses": losses,
                "wr_pct": 100.0 * wins / n,
                "sample_status": "ready" if n >= 20 else "insufficient",
                "breakeven_wr_pct": be_wr,
                "avg_r": avg_r,
                "avg_r_ci_low": avg_low,
                "avg_r_ci_high": avg_high,
                "avg_r_ci_crosses_zero": _ci_crosses_zero(avg_low, avg_high),
                "delta_wr_minus_breakeven_pp": delta,
                "delta_ci_low": delta_low,
                "delta_ci_high": delta_high,
                "delta_ci_crosses_zero": _ci_crosses_zero(delta_low, delta_high),
                "bootstrap_iterations": iterations,
                "avg_r_bootstrap_seed": seed + level_index,
                "delta_bootstrap_seed": seed + 100 + level_index,
                "resampling_unit": "unique signal ID",
                "signal_ids": ids,
            }
        )
    return {
        "analysis": "confirmation_level_percentile_bootstrap",
        "read_only": True,
        "uses_forward_window": False,
        "strategy": TARGET_STRATEGY,
        "levels": list(TARGET_LEVELS),
        "confidence_level": 0.95,
        "bootstrap_method": (
            "percentile bootstrap of per-signal result R and "
            "WR-indicator minus fixed breakeven WR"
        ),
        "bootstrap_iterations": iterations,
        "base_seed": seed,
        "results": results,
        "interpretation": (
            "A CI crossing zero is not evidence of a negative level; "
            "the n=6 level is explicitly descriptive/insufficient."
        ),
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fields = [
        key
        for key in rows[0]
        if key not in {"signal_ids", "resampling_unit"}
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            [{key: row.get(key, "") for key in fields} for row in rows]
        )


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Bootstrap CI for overheated_confirmed confirmation levels",
        "",
        "**Read-only analysis; no production, SQLite, gate, alert, or forward logic was changed.**",
        "",
        "The bootstrap resamples individual resolved signal IDs, uses 20,000 percentile-bootstrap iterations, and reports both avg R and the WR-minus-breakeven delta. Level `2/3` is the n=20 cohort that motivated this check; level `3/3` is shown separately despite its n=6 insufficient sample.",
        "",
        "| Level | n | Wins | Losses | WR | BE WR | avg R | avg R 95% CI | WR − BE | delta 95% CI | CI avg R crosses 0 | CI delta crosses 0 |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---|---|---|",
    ]
    for row in report["results"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["confirmation_level"],
                    str(row["n_unique_signals"]),
                    str(row["wins"]),
                    str(row["losses"]),
                    f"{row['wr_pct']:.4f}%",
                    f"{row['breakeven_wr_pct']:.4f}%",
                    f"{row['avg_r']:.6f}",
                    f"[{row['avg_r_ci_low']:.6f}, {row['avg_r_ci_high']:.6f}]",
                    f"{row['delta_wr_minus_breakeven_pp']:.6f} pp",
                    f"[{row['delta_ci_low']:.6f}, {row['delta_ci_high']:.6f}] pp",
                    "yes" if row["avg_r_ci_crosses_zero"] else "no",
                    "yes" if row["delta_ci_crosses_zero"] else "no",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- `2/3` is exactly at the minimum n=20 threshold; the CI is evidence about uncertainty, not an automatic production rule.",
            "- `3/3` has n=6 and remains insufficient regardless of the bootstrap interval.",
            "- The bootstrap is in-sample and does not replace the planned forward check.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    audit_path: Path,
    output_dir: Path,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    grouped = load_target_rows(audit_path)
    report = build_report(grouped, iterations=iterations, seed=seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "bootstrap.csv", report["results"])
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "report.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path(__file__).parent
        / "outcome_confirmation_level_analysis"
        / "resolved_level_audit.csv",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "outcome_confirmation_level_bootstrap",
    )
    parser.add_argument("--iterations", type=int, default=BOOTSTRAP_ITERATIONS)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    args = parser.parse_args()
    if args.iterations <= 0:
        parser.error("--iterations must be positive")
    report = run_analysis(
        args.audit,
        args.out,
        iterations=args.iterations,
        seed=args.seed,
    )
    print(json.dumps(report["results"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())