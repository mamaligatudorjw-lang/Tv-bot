#!/usr/bin/env python3
"""Fail-closed review of a completed historical liquidation scan.

This report is read-only.  It refuses to review a missing or unvalidated scan
and keeps ``no_outcome_in_window`` outside the resolved-outcome denominator.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from historical_liquidation_pattern_scanner import (
    RESOLVED_OUTCOMES,
    SUCCESS_OUTCOMES,
    utc,
)


PRIMARY_INFERENCE_N = 20
PRIMARY_DESCRIPTIVE_N = 5
KNOWN_OUTCOMES = RESOLVED_OUTCOMES | {"no_outcome_in_window", "not_reached"}
REVIEW_FIELDS = [
    "cohort",
    "event_rows",
    "resolved_n",
    "success_n",
    "success_continuation_n",
    "success_retest_hold_n",
    "failure_n",
    "no_outcome_n",
    "unresolved_precondition_n",
    "success_rate",
    "sufficiency",
]
STAGE_FIELDS = ["cohort", "stage", "count"]


class ReviewError(RuntimeError):
    """The scan is not safe to interpret."""


def _stage_for_row(row: Mapping[str, Any]) -> str:
    outcome = row.get("outcome")
    reason = str(row.get("reason") or "")
    if outcome in RESOLVED_OUTCOMES:
        return "resolved_outcome"
    if outcome == "no_outcome_in_window":
        return "no_outcome_in_window"
    if reason == "correction_not_found_in_12h":
        return "correction_not_found_in_12h"
    if (
        reason == "long_liquidation_threshold_not_met"
        or reason == "missing_15m_hour_notional"
        or reason.startswith("liquidation_coverage_")
        or reason.startswith("liquidation_fetch_error")
    ):
        return "liquidation_burst_stage"
    if (
        reason == "large_5m_flow_not_found"
        or reason.startswith("missing_5m_")
        or reason.startswith("candle_fetch_error")
    ):
        return "large_5m_flow_stage"
    return "other_unresolved"


def _stage_breakdown(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    breakdown: dict[str, dict[str, int]] = {}
    for row in events:
        cohort = str(row.get("cohort") or "unknown")
        stage = _stage_for_row(row)
        breakdown.setdefault(cohort, {})[stage] = (
            breakdown.setdefault(cohort, {}).get(stage, 0) + 1
        )
    return breakdown


def _control_symbols(report: Mapping[str, Any]) -> list[str]:
    config = report.get("config")
    if isinstance(config, Mapping):
        configured = config.get("control_symbols")
        if isinstance(configured, list) and all(
            isinstance(symbol, str) and symbol for symbol in configured
        ):
            return list(dict.fromkeys(configured))

    coverage = report.get("coverage")
    if isinstance(coverage, list):
        return sorted(
            {
                str(row["symbol"])
                for row in coverage
                if isinstance(row, Mapping)
                and row.get("cohort") == "control"
                and row.get("symbol")
            }
        )
    return []


def _coverage_summary(
    coverage: Sequence[Mapping[str, Any]],
    control_symbols: Sequence[str],
) -> dict[str, Any]:
    incomplete_symbols = sorted(
        {
            str(row.get("symbol"))
            for row in coverage
            if row.get("symbol")
            and any(
                row.get(field) == "incomplete"
                for field in ("candle_15m_status", "liquidation_status", "flow_5m_status")
            )
        }
    )
    control_coverage = {
        symbol: next(
            (
                {
                    "candle_15m_status": row.get("candle_15m_status"),
                    "liquidation_status": row.get("liquidation_status"),
                    "flow_5m_status": row.get("flow_5m_status"),
                    "reason": row.get("reason") or None,
                }
                for row in coverage
                if row.get("cohort") == "control" and row.get("symbol") == symbol
            ),
            None,
        )
        for symbol in control_symbols
    }
    return {
        "coverage_rows": len(coverage),
        "incomplete_symbols": incomplete_symbols,
        "incomplete_n": len(incomplete_symbols),
        "control_symbols": control_coverage,
    }


def load_completed_scan_report(report_dir: Path) -> dict[str, Any]:
    """Load a physically present, successful scan report or fail closed."""
    report_path = report_dir / "report.json"
    if not report_path.is_file():
        raise ReviewError(f"scan report is missing: {report_path}")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ReviewError(f"cannot read scan report: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReviewError("scan report must be a JSON object")
    if payload.get("production_changes") is not False:
        raise ReviewError("scan report is not marked read-only")
    preflight = payload.get("preflight")
    if not isinstance(preflight, dict) or preflight.get("ok") is not True:
        raise ReviewError("scan report does not contain a passing preflight")
    if not isinstance(payload.get("events"), list):
        raise ReviewError("scan report has no event list")
    if not isinstance(payload.get("coverage"), list):
        raise ReviewError("scan report has no coverage list")
    unknown_outcomes = {
        row.get("outcome")
        for row in payload["events"]
        if not isinstance(row, dict)
        or row.get("outcome") not in KNOWN_OUTCOMES
    }
    if unknown_outcomes:
        raise ReviewError(
            f"scan report has unknown outcomes: {sorted(unknown_outcomes, key=str)}"
        )
    return payload


def _cohort_summary(
    events: Sequence[Mapping[str, Any]],
    cohort: str,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    rows = [row for row in events if row.get("cohort") == cohort]
    resolved = [row for row in rows if row.get("outcome") in RESOLVED_OUTCOMES]
    continuation_n = sum(
        row.get("outcome") == "success_continuation" for row in resolved
    )
    retest_n = sum(
        row.get("outcome") == "success_retest_hold" for row in resolved
    )
    success_n = continuation_n + retest_n
    failure_n = sum(row.get("outcome") == "failure_breakdown" for row in resolved)
    no_outcome_n = sum(
        row.get("outcome") == "no_outcome_in_window" for row in rows
    )
    unresolved_precondition_n = len(rows) - len(resolved) - no_outcome_n
    resolved_n = len(resolved)
    if cohort == "primary":
        if resolved_n >= PRIMARY_INFERENCE_N:
            sufficiency = "full_inference"
        elif resolved_n >= PRIMARY_DESCRIPTIVE_N:
            sufficiency = "descriptive_only"
        else:
            sufficiency = "case_log_only"
    else:
        sufficiency = "controls_any_n"
    return {
        "cohort": label or cohort,
        "event_rows": len(rows),
        "resolved_n": resolved_n,
        "success_n": success_n,
        "success_continuation_n": continuation_n,
        "success_retest_hold_n": retest_n,
        "failure_n": failure_n,
        "no_outcome_n": no_outcome_n,
        "unresolved_precondition_n": unresolved_precondition_n,
        "success_rate": (
            success_n / resolved_n
            if resolved_n
            and (cohort != "primary" or resolved_n >= PRIMARY_DESCRIPTIVE_N)
            else None
        ),
        "sufficiency": sufficiency,
    }


def build_review(report: Mapping[str, Any]) -> dict[str, Any]:
    events = report["events"]
    if not isinstance(events, list):
        raise ReviewError("scan report events are not a list")
    coverage = report["coverage"]
    if not isinstance(coverage, list):
        raise ReviewError("scan report coverage is not a list")
    control_symbols = _control_symbols(report)
    cohorts = {
        "primary": _cohort_summary(events, "primary"),
        "control": _cohort_summary(events, "control"),
    }
    control_cohorts = {
        symbol: _cohort_summary(
            [
                row
                for row in events
                if row.get("cohort") == "control" and row.get("symbol") == symbol
            ],
            "control",
            label=f"control:{symbol}",
        )
        for symbol in control_symbols
    }
    stage_breakdown = _stage_breakdown(events)
    control_stage_breakdown = {
        symbol: _stage_breakdown(
            [
                row
                for row in events
                if row.get("cohort") == "control" and row.get("symbol") == symbol
            ]
        ).get("control", {})
        for symbol in control_symbols
    }
    generated_ts = int(time.time())
    return {
        "generated_ts": generated_ts,
        "generated_utc": utc(generated_ts),
        "source_report_generated_utc": report.get("generated_utc"),
        "resolved_denominator_definition": (
            "n counts only success_continuation, success_retest_hold, "
            "and failure_breakdown; no_outcome_in_window is excluded"
        ),
        "cohorts": cohorts,
        "control_cohorts": control_cohorts,
        "stage_breakdown": stage_breakdown,
        "control_stage_breakdown": control_stage_breakdown,
        "coverage": _coverage_summary(coverage, control_symbols),
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=REVIEW_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_stage_csv(
    path: Path,
    stage_breakdown: Mapping[str, Mapping[str, int]],
) -> None:
    rows = [
        {"cohort": cohort, "stage": stage, "count": count}
        for cohort, stages in stage_breakdown.items()
        for stage, count in stages.items()
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=STAGE_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_review(report_dir: Path, output_dir: Path) -> dict[str, Any]:
    if report_dir.resolve() == output_dir.resolve():
        raise ReviewError("refusing to overwrite the source scan report")
    report = load_completed_scan_report(report_dir)
    review = build_review(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    rows = list(review["cohorts"].values()) + list(
        review["control_cohorts"].values()
    )
    _write_csv(output_dir / "review.csv", rows)
    _write_stage_csv(
        output_dir / "stage_breakdown.csv",
        review["stage_breakdown"],
    )
    lines = [
        "# Historical liquidation pattern review",
        "",
        f"- Source scan generated: **{review['source_report_generated_utc']}**",
        f"- `n` definition: **{review['resolved_denominator_definition']}**",
        "",
        "| Cohort | Events | Resolved n | Success | Failure | No outcome | Precondition unresolved | Success rate | Sufficiency |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        rate = "—" if row["success_rate"] is None else f"{row['success_rate']:.3f}"
        lines.append(
            f"| {row['cohort']} | {row['event_rows']} | {row['resolved_n']} | "
            f"{row['success_n']} | {row['failure_n']} | {row['no_outcome_n']} | "
            f"{row['unresolved_precondition_n']} | {rate} | {row['sufficiency']} |"
        )
    lines += [
        "",
        "`no_outcome_in_window` is excluded from resolved n and is never "
        "reclassified as success or failure.",
        "",
        "The `Success` column is the sum of the distinct "
        "`success_continuation` and `success_retest_hold` buckets; both remain "
        "available as separate fields in `review.json` and `review.csv`.",
        "",
        "## Control cohorts by symbol",
        "",
        "| Symbol | Events | Resolved n | Continuation | Retest and hold | Failure | No outcome | Success rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, row in review["control_cohorts"].items():
        rate = "—" if row["success_rate"] is None else f"{row['success_rate']:.3f}"
        lines.append(
            f"| {symbol} | {row['event_rows']} | {row['resolved_n']} | "
            f"{row['success_continuation_n']} | {row['success_retest_hold_n']} | "
            f"{row['failure_n']} | {row['no_outcome_n']} | {rate} |"
        )
    lines += [
        "",
        "## Coverage validation",
        "",
        f"- Coverage rows: **{review['coverage']['coverage_rows']}**",
        f"- Symbols with incomplete coverage: **{review['coverage']['incomplete_n']}**"
        + (
            f" ({', '.join(review['coverage']['incomplete_symbols'])})"
            if review["coverage"]["incomplete_symbols"]
            else ""
        ),
        "",
        "| Control | 15m status | Liquidation status | 5m status | Reason |",
        "|---|---|---|---|---|",
    ]
    for symbol, status in review["coverage"]["control_symbols"].items():
        if status is None:
            lines.append(f"| {symbol} | — | — | — | missing coverage row |")
        else:
            lines.append(
                f"| {symbol} | {status['candle_15m_status'] or '—'} | "
                f"{status['liquidation_status'] or '—'} | "
                f"{status['flow_5m_status'] or '—'} | {status['reason'] or '—'} |"
            )
    lines += [
        "",
        "",
        "## Unresolved precondition stages",
        "",
        "| Cohort | Stage | Count |",
        "|---|---|---:|",
    ]
    for cohort, stages in review["stage_breakdown"].items():
        for stage, count in stages.items():
            lines.append(f"| {cohort} | {stage} | {count} |")
    lines += [
        "",
        "Control stage breakdown by symbol:",
        "",
        "| Control | Stage | Count |",
        "|---|---|---:|",
    ]
    for symbol, stages in review["control_stage_breakdown"].items():
        if not stages:
            lines.append(f"| {symbol} | — | 0 |")
        else:
            for stage, count in stages.items():
                lines.append(f"| {symbol} | {stage} | {count} |")
    lines += [
        "",
        "`liquidation_burst_stage` includes the $100,000 / 2% threshold and "
        "any incomplete liquidation-hour coverage. "
        "`large_5m_flow_stage` includes missing 5m coverage and not-found flow.",
        "",
    ]
    (output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return review


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("outcome_historical_liquidation_pattern"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outcome_historical_liquidation_pattern_review"),
    )
    args = parser.parse_args(argv)
    try:
        review = write_review(args.report_dir, args.out)
    except (OSError, ReviewError, ValueError) as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {args.out / 'review.md'} "
        f"(primary_resolved_n={review['cohorts']['primary']['resolved_n']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())