#!/usr/bin/env python3
"""Read-only follow-up report for the partial-TP50 trailing artifact.

This consumes the already frozen partial-TP artifact and joins only the
persisted entry/SL prices from SQLite in read-only mode for a fee/slippage
sensitivity. It does not rerun candles, mutate SQLite, or change production.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Sequence


STEPS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
DEFAULT_COST_RATES_BPS = (5.0, 10.0)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Invalid non-finite {field}: {value!r}")
    return result


def as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def load_positions(
    db_path: Path,
    ids: Sequence[int],
) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, entry_price, sl_price, direction
                  FROM demo_positions
                 WHERE id IN ({placeholders})
                """,
                list(ids),
            )
        ]
    finally:
        connection.close()
    positions = {as_int(row["id"], "position id"): row for row in rows}
    missing = sorted(set(ids) - set(positions))
    if missing:
        raise ValueError(f"Missing demo_positions for IDs: {missing[:10]}")
    return positions


def extra_second_leg_cost_r(
    audit_row: dict[str, str],
    position: dict[str, Any],
    *,
    cost_rate_bps: float,
) -> float:
    """Estimate an extra adverse cost on the second 50% close, in R.

    This intentionally models only the extra second close execution. It is a
    sensitivity, not a claim about the exchange's actual fee schedule.
    """
    if audit_row["partial_branch"] != "tp_branch":
        return 0.0
    trail_price = as_float(audit_row["trail_price"], "trail_price")
    entry = as_float(position["entry_price"], "entry_price")
    sl = as_float(position["sl_price"], "sl_price")
    risk = abs(entry - sl)
    if risk <= 0:
        raise ValueError(f"Zero risk for id={audit_row['id']}")
    rate = cost_rate_bps / 10_000.0
    return 0.5 * rate * abs(trail_price) / risk


def build_report(
    source_report: dict[str, Any],
    audit_rows: Sequence[dict[str, str]],
    positions: dict[int, dict[str, Any]],
    *,
    cost_rates_bps: Sequence[float] = DEFAULT_COST_RATES_BPS,
) -> dict[str, Any]:
    baseline = next(
        row for row in source_report["summary"] if row["sample"] == "baseline_fixed"
    )
    unique_ids = sorted({as_int(row["id"], "audit id") for row in audit_rows})
    if len(unique_ids) != baseline["n"]:
        raise ValueError(
            f"Expected {baseline['n']} unique frozen IDs, got {len(unique_ids)}"
        )
    rows: list[dict[str, Any]] = []
    for source in source_report["summary"]:
        if source["sample"] == "baseline_fixed":
            baseline_row = {
                "sample": "baseline_fixed",
                "step_pct": "",
                "n": source["n"],
                "wins": source["n_tp_branch"],
                "losses": source["n"] - source["n_tp_branch"],
                "sum_r": source["total_r"],
                "avg_r": source["avg_r"],
                "wr_pct": source["wr_pct"],
                "tp_branch_n": source["n_tp_branch"],
                "floor_exit_n": 0,
                "trail_exit_n": 0,
                "branch_rows": 0,
                "proportional_split_fee_delta_r": 0.0,
            }
            for rate in cost_rates_bps:
                prefix = f"extra_second_leg_cost_{int(rate)}bps"
                baseline_row.update(
                    {
                        f"{prefix}_total_r": 0.0,
                        f"{prefix}_avg_r": 0.0,
                        f"{prefix}_adjusted_avg_r": source["avg_r"],
                    }
                )
            rows.append(baseline_row)
            continue
        step = as_float(source["step_pct"], "step_pct")
        step_rows = [
            row
            for row in audit_rows
            if as_float(row["step_pct"], "step_pct") == step
        ]
        if len(step_rows) != baseline["n"]:
            raise ValueError(f"Step {step} has {len(step_rows)} rows")
        branch_rows = [
            row for row in step_rows if row["partial_branch"] == "tp_branch"
        ]
        floor_n = sum(row["outcome"] == "partial_tp_floor" for row in branch_rows)
        trail_n = sum(row["outcome"] == "partial_trail_stop" for row in branch_rows)
        if floor_n != int(source["floor_exit_n"]) or trail_n != int(
            source["trail_exit_n"]
        ):
            raise ValueError(f"Branch counts disagree for step {step}")
        cost_totals = {
            f"extra_second_leg_cost_{int(rate)}bps_total_r": sum(
                extra_second_leg_cost_r(
                    row,
                    positions[as_int(row["id"], "audit id")],
                    cost_rate_bps=rate,
                )
                for row in branch_rows
            )
            for rate in cost_rates_bps
        }
        cost_avgs = {
            key.replace("_total_r", "_avg_r"): total / baseline["n"]
            for key, total in cost_totals.items()
        }
        rows.append(
            {
                "sample": "partial_tp50",
                "step_pct": step,
                "n": source["n"],
                "wins": source["n_tp_branch"],
                "losses": source["n"] - source["n_tp_branch"],
                "sum_r": source["total_r"],
                "avg_r": source["avg_r"],
                "wr_pct": source["wr_pct"],
                "tp_branch_n": source["n_tp_branch"],
                "floor_exit_n": floor_n,
                "trail_exit_n": trail_n,
                "branch_rows": floor_n + trail_n,
                "proportional_split_fee_delta_r": 0.0,
                **cost_totals,
                **cost_avgs,
                **{
                    key.replace("_avg_r", "_adjusted_avg_r"): source["avg_r"]
                    - average
                    for key, average in cost_avgs.items()
                },
            }
        )
    return {
        "analysis": "partial_tp50_followup_summary_and_second_leg_sensitivity",
        "read_only": True,
        "uses_forward_window": False,
        "source_report": "outcome_partial_tp50_trailing/report.json",
        "source_audit": "outcome_partial_tp50_trailing/audit.csv",
        "commission_accounting": {
            "included_in_source_simulation": False,
            "proportional_split_fee_delta": (
                "zero versus one full close when the same total exit notional "
                "is charged at the same proportional rate; only order minimums/"
                "rounding could make it non-zero"
            ),
            "sensitivity": (
                "extra adverse cost on the second 50% close only, averaged over "
                "all 408 signals; does not estimate total all-in fees"
            ),
            "cost_rates_bps": list(cost_rates_bps),
        },
        "rows": rows,
        "branch_total": {
            "tp_branch_step_rows": sum(row["tp_branch_n"] for row in rows[1:]),
            "floor_step_rows": sum(row["floor_exit_n"] for row in rows[1:]),
            "trail_step_rows": sum(row["trail_exit_n"] for row in rows[1:]),
        },
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    rates = report["commission_accounting"]["cost_rates_bps"]
    headers = [
        "Sample",
        "Step",
        "n",
        "ΣR",
        "avg R",
        "WR",
        "TP branch",
        "Floor exits",
        "Trailing exits",
        "Branch rows",
    ]
    for rate in rates:
        headers.extend(
            [
                f"Extra {rate:g}bps cost ΣR",
                f"Extra {rate:g}bps cost avg R",
                f"avg R after extra {rate:g}bps",
            ]
        )
    lines = [
        "# Partial TP50 follow-up",
        "",
        "**Read-only report over the frozen partial-TP artifact; no SQLite, production, or forward state was changed.**",
        "",
        "The source simulation includes no fees or slippage. With a purely proportional fee, splitting one full close into two 50% closes does not add commission on the same total exit notional. The sensitivity columns instead show an explicitly hypothetical adverse cost on the second 50% close only, averaged over all 408 signals.",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---:" if index > 1 else "---" for index in range(len(headers))) + "|",
    ]
    for row in report["rows"]:
        values = [
            row["sample"],
            row["step_pct"],
            row["n"],
            fmt(row["sum_r"]),
            fmt(row["avg_r"]),
            f"{row['wr_pct']:.4f}%" if row["wr_pct"] is not None else "",
            row["tp_branch_n"],
            row["floor_exit_n"],
            row["trail_exit_n"],
            row["branch_rows"],
        ]
        for rate in rates:
            prefix = f"extra_second_leg_cost_{int(rate)}bps"
            values.extend(
                [
                    fmt(row.get(f"{prefix}_total_r")),
                    fmt(row.get(f"{prefix}_avg_r")),
                    fmt(row.get(f"{prefix}_adjusted_avg_r")),
                ]
            )
        lines.append("| " + " | ".join(map(str, values)) + " |")
    lines.extend(
        [
            "",
            "## Branch totals",
            "",
            f"- TP branch-step rows: `{report['branch_total']['tp_branch_step_rows']}`.",
            f"- Floor exits at the TP floor: `{report['branch_total']['floor_step_rows']}`.",
            f"- Real trailing exits above the floor: `{report['branch_total']['trail_step_rows']}`.",
            "",
            "The floor and trailing counts sum to the TP branch count for every step. Floor exits have the same modeled R as the baseline TP exit; trailing exits are the branch-step combinations with a modeled exit above the TP floor.",
            "",
            "## Commission/slippage interpretation",
            "",
            "- The source artifact has no explicit commission or slippage model.",
            "- A proportional fee charged on notional does not become larger merely because the exit is split: `fee(50%) + fee(50%) = fee(100%)`.",
            "- The sensitivity is intentionally conservative as an incremental second-leg execution cost and should not be added to an all-in fee model a second time.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    db_path: Path,
    source_dir: Path,
    output_dir: Path,
    *,
    cost_rates_bps: Sequence[float] = DEFAULT_COST_RATES_BPS,
) -> dict[str, Any]:
    source_report = json.loads(
        (source_dir / "report.json").read_text(encoding="utf-8")
    )
    audit_rows = read_csv(source_dir / "audit.csv")
    ids = sorted({as_int(row["id"], "audit id") for row in audit_rows})
    positions = load_positions(db_path, ids)
    report = build_report(
        source_report,
        audit_rows,
        positions,
        cost_rates_bps=cost_rates_bps,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "followup_summary.csv", report["rows"])
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "report.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).parent
    parser.add_argument("--db", type=Path, default=base / "alerts.db")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=base / "outcome_partial_tp50_trailing",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=base / "outcome_partial_tp50_followup",
    )
    parser.add_argument(
        "--cost-rates-bps",
        type=float,
        nargs="+",
        default=list(DEFAULT_COST_RATES_BPS),
    )
    args = parser.parse_args()
    if any(rate < 0 for rate in args.cost_rates_bps):
        parser.error("cost rates must be non-negative")
    report = run_analysis(
        args.db,
        args.source_dir,
        args.out,
        cost_rates_bps=args.cost_rates_bps,
    )
    print(
        json.dumps(
            {
                "output": str(args.out),
                "branch_total": report["branch_total"],
                "rows": report["rows"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())