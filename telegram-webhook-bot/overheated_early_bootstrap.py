#!/usr/bin/env python3
"""Read-only bootstrap for the frozen overheated_early post-fix cohort.

The source database is opened in SQLite read-only mode.  This module deliberately
does not use the live alerts.db by default: the historical headline cohort is
frozen at the last backup containing exactly 121 post-fix resolved rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


FIX_SPLIT_TS = 1787422679
BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20260830
EXPECTED_POST_FIX_N = 121
BREAKEVEN_WR_PCT = 100.0 / 3.0

DEFAULT_DB = (
    Path(__file__).parent
    / "alerts_db_backups"
    / "alerts_20260828_185322.db"
)
DEFAULT_OUT = Path(__file__).parent / "outcome_overheated_early_bootstrap"


def fmt_ts(value: int | None) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat()
        if value is not None
        else ""
    )


def _result_r(row: sqlite3.Row) -> float:
    entry = float(row["entry_price"])
    stop = float(row["sl_price"])
    exit_price = float(row["exit_price"])
    risk = abs(stop - entry)
    if not math.isfinite(risk) or risk <= 0:
        raise ValueError(f"Invalid entry-to-SL risk for signal ID {row['id']}")
    result = (
        (exit_price - entry) / risk
        if row["direction"] == "LONG"
        else (entry - exit_price) / risk
    )
    if not math.isfinite(result):
        raise ValueError(f"Invalid result R for signal ID {row['id']}")
    return result


def load_resolved_rows(db_path: Path) -> list[dict[str, Any]]:
    """Load only resolved shadow rows, with no write-capable SQLite connection."""
    if not db_path.exists():
        raise FileNotFoundError(f"Frozen source database does not exist: {db_path}")
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, ts_open, ts_close, symbol, direction, status,
                   entry_price, sl_price, tp_price, exit_price
              FROM demo_positions
             WHERE is_shadow=1
               AND alert_type='overheated_early'
               AND status IN ('tp', 'sl')
               AND ts_close IS NOT NULL
             ORDER BY ts_close, id
            """
        ).fetchall()
    finally:
        connection.close()

    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        signal_id = int(row["id"])
        if signal_id in seen:
            raise ValueError(f"Duplicate signal ID in source: {signal_id}")
        seen.add(signal_id)
        item = dict(row)
        item["result_r"] = _result_r(row)
        item["fix_cohort_by_close"] = (
            "post_fix" if int(row["ts_close"]) >= FIX_SPLIT_TS else "pre_fix"
        )
        item["ts_open_utc"] = fmt_ts(int(row["ts_open"]))
        item["ts_close_utc"] = fmt_ts(int(row["ts_close"]))
        result.append(item)
    return result


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    weight = position - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def _metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "wr_pct": None,
            "avg_r": None,
        }
    wins = sum(row["status"] == "tp" for row in rows)
    values = [float(row["result_r"]) for row in rows]
    return {
        "n": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "wr_pct": 100.0 * wins / len(rows),
        "avg_r": sum(values) / len(values),
    }


def bootstrap_metrics(
    rows: Sequence[dict[str, Any]],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
    breakeven_wr_pct: float = BREAKEVEN_WR_PCT,
) -> dict[str, Any]:
    """Use one resample per iteration for avg R and WR-minus-BE together."""
    if not rows:
        raise ValueError("Cannot bootstrap an empty cohort")
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    ids = [int(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Bootstrap input must contain unique signal IDs")

    result_values = [float(row["result_r"]) for row in rows]
    win_values = [1.0 if row["status"] == "tp" else 0.0 for row in rows]
    n = len(rows)
    rng = random.Random(seed)
    avg_r_samples: list[float] = []
    wr_delta_samples: list[float] = []

    for _ in range(iterations):
        sample = [rng.randrange(n) for _ in range(n)]
        avg_r_samples.append(sum(result_values[index] for index in sample) / n)
        wr_pct = 100.0 * sum(win_values[index] for index in sample) / n
        wr_delta_samples.append(wr_pct - breakeven_wr_pct)

    avg_r_samples.sort()
    wr_delta_samples.sort()
    observed = _metrics(rows)
    avg_r_low = _quantile(avg_r_samples, 0.025)
    avg_r_high = _quantile(avg_r_samples, 0.975)
    delta_low = _quantile(wr_delta_samples, 0.025)
    delta_high = _quantile(wr_delta_samples, 0.975)
    observed_delta = float(observed["wr_pct"]) - breakeven_wr_pct
    return {
        **observed,
        "breakeven_wr_pct": breakeven_wr_pct,
        "delta_wr_minus_breakeven_pp": observed_delta,
        "avg_r_ci_low": avg_r_low,
        "avg_r_ci_high": avg_r_high,
        "avg_r_ci_crosses_zero": avg_r_low <= 0.0 <= avg_r_high,
        "wr_ci_low_pct": _quantile(
            sorted(value + breakeven_wr_pct for value in wr_delta_samples),
            0.025,
        ),
        "wr_ci_high_pct": _quantile(
            sorted(value + breakeven_wr_pct for value in wr_delta_samples),
            0.975,
        ),
        "delta_ci_low_pp": delta_low,
        "delta_ci_high_pp": delta_high,
        "delta_ci_crosses_zero": delta_low <= 0.0 <= delta_high,
        "bootstrap_iterations": iterations,
        "bootstrap_seed": seed,
        "resampling_unit": "unique signal ID",
        "signal_ids": ids,
    }


def build_report(
    rows: Sequence[dict[str, Any]],
    *,
    fix_split_ts: int = FIX_SPLIT_TS,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
    expected_post_fix_n: int | None = EXPECTED_POST_FIX_N,
    source_path: Path | None = None,
) -> dict[str, Any]:
    if fix_split_ts != FIX_SPLIT_TS:
        raise ValueError(
            "This frozen reconstruction is tied to the recorded price-basis "
            f"fix timestamp {FIX_SPLIT_TS}"
        )
    post_fix = [
        row for row in rows if int(row["ts_close"]) >= fix_split_ts
    ]
    pre_fix = [
        row for row in rows if int(row["ts_close"]) < fix_split_ts
    ]
    if expected_post_fix_n is not None and len(post_fix) != expected_post_fix_n:
        raise ValueError(
            f"Expected post-fix n={expected_post_fix_n}, found n={len(post_fix)}"
        )

    result = bootstrap_metrics(
        post_fix,
        iterations=iterations,
        seed=seed,
    )
    result["sample_status"] = "ready" if len(post_fix) >= 20 else "insufficient"
    result["cohort"] = "post_fix_by_ts_close"
    report = {
        "analysis": "overheated_early_post_fix_percentile_bootstrap",
        "read_only": True,
        "production_logic_changed": False,
        "database_changed": False,
        "confidence_level": 0.95,
        "bootstrap_method": (
            "paired percentile bootstrap: one resample of whole signal IDs "
            "per iteration, used for avg R and WR-minus-breakeven"
        ),
        "bootstrap_iterations": iterations,
        "bootstrap_seed": seed,
        "resampling_unit": "unique signal ID",
        "fix": {
            "timestamp": fix_split_ts,
            "utc": fmt_ts(fix_split_ts),
            "classification_field": "ts_close",
            "classification": "post_fix when ts_close >= fix timestamp",
            "note": (
                "The live price-basis fix protects the persistence boundary "
                "that writes entry/SL/TP in app.py; ts_open is the causal "
                "creation-time field. ts_close is retained here only to "
                "reproduce the requested frozen n=121 headline cohort."
            ),
        },
        "source": {
            "database": str(source_path) if source_path else None,
            "selection": (
                "shadow demo_positions, alert_type=overheated_early, "
                "status in (tp, sl), valid persisted exit"
            ),
            "resolved_all": _metrics(rows),
            "pre_fix_by_close": _metrics(pre_fix),
            "post_fix_by_close": _metrics(post_fix),
            "historical_headline_context": {
                "reported_overall_resolved_n": 227,
                "reported_overall_wr_pct": 43.61,
                "reported_post_fix_n": 121,
                "reported_post_fix_wr_pct": 35.64,
                "reported_post_fix_avg_r": 0.084,
                "reproduced_from_source": False,
                "note": (
                    "The old per-signal fixed-window audit was not retained. "
                    "The current report records the frozen backup actually used "
                    "and does not silently claim it reproduces that headline."
                ),
            },
        },
        "post_fix": result,
        "recommendation": (
            "do_not_promote_collect_more_data"
            if result["avg_r_ci_crosses_zero"]
            or result["delta_ci_crosses_zero"]
            else "candidate_for_promotion_review"
        ),
    }
    return report


def write_outputs(
    output_dir: Path,
    report: dict[str, Any],
    post_fix_rows: Sequence[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "post_fix_cohort.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        fields = [
            "id",
            "ts_open",
            "ts_open_utc",
            "ts_close",
            "ts_close_utc",
            "symbol",
            "direction",
            "status",
            "entry_price",
            "sl_price",
            "tp_price",
            "exit_price",
            "result_r",
            "fix_cohort_by_close",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in post_fix_rows)

    summary = report["post_fix"]
    with (output_dir / "bootstrap.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        fields = [
            "cohort",
            "n",
            "wins",
            "losses",
            "wr_pct",
            "breakeven_wr_pct",
            "delta_wr_minus_breakeven_pp",
            "avg_r",
            "avg_r_ci_low",
            "avg_r_ci_high",
            "wr_ci_low_pct",
            "wr_ci_high_pct",
            "delta_ci_low_pp",
            "delta_ci_high_pp",
            "avg_r_ci_crosses_zero",
            "delta_ci_crosses_zero",
            "bootstrap_iterations",
            "bootstrap_seed",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({field: summary.get(field, "") for field in fields})

    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    context = report["source"]["historical_headline_context"]
    lines = [
        "# Bootstrap CI for overheated_early post-fix cohort",
        "",
        "**Read-only analysis. No production logic, promotion switch, or SQLite row was changed.**",
        "",
        "## Frozen source and cohort",
        "",
        f"- Source: `{report['source']['database']}`.",
        f"- Price-basis fix boundary: **{report['fix']['utc']}**.",
        "- Cohort rule: `ts_close >= fix timestamp` (explicit compatibility choice for the requested frozen n=121 cohort).",
        f"- Post-fix cohort: **n={summary['n']} unique signal IDs**; "
        f"wins={summary['wins']}, losses={summary['losses']}.",
        "- `ts_close` is not the causal creation-time field. The live fix protects "
        "the `demo_positions` persistence boundary; `ts_close` is used here only "
        "because the historical headline specified n=121 under that frozen split.",
        "",
        "## Bootstrap method",
        "",
        f"- **{summary['bootstrap_iterations']:,}** percentile-bootstrap iterations, seed `{summary['bootstrap_seed']}`.",
        "- Each iteration resamples whole unique signal IDs with replacement.",
        "- The same resample is used for avg R and WR-minus-breakeven, preserving their pairing.",
        "- Breakeven WR for the 2:1 target/stop geometry: **33.3333%**.",
        "",
        "| Cohort | n | Wins | Losses | WR | WR − BE | avg R | avg R 95% CI | WR 95% CI | delta 95% CI |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|---|",
        "| post-fix by ts_close | "
        f"{summary['n']} | {summary['wins']} | {summary['losses']} | "
        f"{summary['wr_pct']:.4f}% | {summary['delta_wr_minus_breakeven_pp']:.4f} pp | "
        f"{summary['avg_r']:.6f} | "
        f"[{summary['avg_r_ci_low']:.6f}, {summary['avg_r_ci_high']:.6f}] | "
        f"[{summary['wr_ci_low_pct']:.4f}%, {summary['wr_ci_high_pct']:.4f}%] | "
        f"[{summary['delta_ci_low_pp']:.4f}, {summary['delta_ci_high_pp']:.4f}] pp |",
        "",
        "## Decision",
        "",
        f"- avg R CI crosses zero: **{'yes' if summary['avg_r_ci_crosses_zero'] else 'no'}**.",
        f"- WR-minus-breakeven CI crosses zero: **{'yes' if summary['delta_ci_crosses_zero'] else 'no'}**.",
        f"- Recommendation: **`{report['recommendation']}`**.",
        "- The post-fix result is not statistically separated from breakeven, so "
        "the strategy should not be promoted on this cohort.",
        "",
        "## Reconciliation with the earlier headline",
        "",
        f"The earlier review reported overall resolved n={context['reported_overall_resolved_n']}, "
        f"WR={context['reported_overall_wr_pct']:.2f}%, and post-fix n={context['reported_post_fix_n']}, "
        f"WR={context['reported_post_fix_wr_pct']:.2f}%, avg R={context['reported_post_fix_avg_r']:.3f}.",
        "That historical per-signal fixed-window audit is not present in the workspace. "
        "The frozen backup used here contains a different resolved context, so its "
        "observed values are reported honestly rather than relabeled as the old audit.",
        "",
        "## Files",
        "",
        "- `post_fix_cohort.csv`: exact 121 signal IDs and per-signal R values used.",
        "- `bootstrap.csv`: observed metrics and percentile intervals.",
        "- `report.json`: machine-readable provenance and results.",
        "",
    ]
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    db_path: Path = DEFAULT_DB,
    output_dir: Path = DEFAULT_OUT,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
    expected_post_fix_n: int | None = EXPECTED_POST_FIX_N,
) -> dict[str, Any]:
    rows = load_resolved_rows(db_path)
    report = build_report(
        rows,
        iterations=iterations,
        seed=seed,
        expected_post_fix_n=expected_post_fix_n,
        source_path=db_path,
    )
    post_fix_rows = [
        row for row in rows if int(row["ts_close"]) >= FIX_SPLIT_TS
    ]
    write_outputs(output_dir, report, post_fix_rows)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--iterations", type=int, default=BOOTSTRAP_ITERATIONS)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument(
        "--allow-different-post-n",
        action="store_true",
        help="disable the frozen n=121 guard (not used for the published artifact)",
    )
    args = parser.parse_args()
    if args.iterations <= 0:
        parser.error("--iterations must be positive")
    report = run_analysis(
        args.db,
        args.out,
        iterations=args.iterations,
        seed=args.seed,
        expected_post_fix_n=None if args.allow_different_post_n else EXPECTED_POST_FIX_N,
    )
    print(
        json.dumps(
            {
                "output": str(args.out),
                "cohort": report["post_fix"]["cohort"],
                "n": report["post_fix"]["n"],
                "avg_r": report["post_fix"]["avg_r"],
                "avg_r_ci": [
                    report["post_fix"]["avg_r_ci_low"],
                    report["post_fix"]["avg_r_ci_high"],
                ],
                "wr_pct": report["post_fix"]["wr_pct"],
                "wr_ci_pct": [
                    report["post_fix"]["wr_ci_low_pct"],
                    report["post_fix"]["wr_ci_high_pct"],
                ],
                "recommendation": report["recommendation"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())