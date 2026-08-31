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
    "failure_n",
    "no_outcome_n",
    "unresolved_precondition_n",
    "success_rate",
    "sufficiency",
]


class ReviewError(RuntimeError):
    """The scan is not safe to interpret."""


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
) -> dict[str, Any]:
    rows = [row for row in events if row.get("cohort") == cohort]
    resolved = [row for row in rows if row.get("outcome") in RESOLVED_OUTCOMES]
    success_n = sum(row.get("outcome") in SUCCESS_OUTCOMES for row in resolved)
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
        "cohort": cohort,
        "event_rows": len(rows),
        "resolved_n": resolved_n,
        "success_n": success_n,
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
    cohorts = {
        "primary": _cohort_summary(events, "primary"),
        "control": _cohort_summary(events, "control"),
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
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
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
    rows = list(review["cohorts"].values())
    _write_csv(output_dir / "review.csv", rows)
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