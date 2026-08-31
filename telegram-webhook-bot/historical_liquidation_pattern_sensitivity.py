#!/usr/bin/env python3
"""Read-only liquidation-threshold sensitivity replay.

The completed baseline scan is the immutable source of pump, correction,
liquidation, and candle-coverage facts.  This module only replays downstream
flow/outcome checks for candidates admitted by a softer threshold and writes a
separate comparison report.  It never imports production trading code.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from historical_liquidation_pattern_review import (
    PRIMARY_DESCRIPTIVE_N,
    PRIMARY_INFERENCE_N,
    load_completed_scan_report,
)
from historical_liquidation_pattern_scanner import (
    DAY,
    EVENT_FIELDS,
    FIFTEEN_MINUTES,
    FIVE_MINUTES,
    FLOW_LOOKAHEAD_HOURS,
    FLOW_MULTIPLIER,
    GateClient,
    RESOLVED_OUTCOMES,
    SUCCESS_OUTCOMES,
    OUTCOME_HOURS,
    classify_support_retest,
    find_large_flow,
    finite_float,
    gate_contract,
    utc,
)


THRESHOLD_GRID = (
    {
        "id": "soft_25k_0_5pct",
        "label": "$25,000 / 0.5%",
        "min_usd": 25_000.0,
        "hourly_fraction": 0.005,
    },
    {
        "id": "soft_50k_1pct",
        "label": "$50,000 / 1%",
        "min_usd": 50_000.0,
        "hourly_fraction": 0.01,
    },
    {
        "id": "baseline_100k_2pct",
        "label": "$100,000 / 2% (baseline)",
        "min_usd": 100_000.0,
        "hourly_fraction": 0.02,
    },
    {
        "id": "strict_150k_3pct",
        "label": "$150,000 / 3%",
        "min_usd": 150_000.0,
        "hourly_fraction": 0.03,
    },
)
BASELINE_THRESHOLD_ID = "baseline_100k_2pct"
INCREMENTAL_FIELDS = [
    "threshold_id",
    "threshold_label",
    "stricter_threshold_id",
    "stricter_threshold_label",
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
    "incremental_quality",
    "stage_counts",
]
CUMULATIVE_FIELDS = [
    "threshold_id",
    "threshold_label",
    "min_usd",
    "hourly_fraction",
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
    "stage_counts",
]
EVENT_OUTPUT_FIELDS = [
    "threshold_id",
    "threshold_label",
    "min_usd",
    "hourly_fraction",
    "threshold_passed",
    *EVENT_FIELDS,
]
INCREMENTAL_EVENT_FIELDS = [
    "threshold_id",
    "threshold_label",
    "stricter_threshold_id",
    "stricter_threshold_label",
    "symbol",
    "cohort",
    "pump_ts",
    "pump_utc",
    "threshold_passed",
    "long_liq_notional_usd",
    "hour_futures_notional_usd",
    "liq_threshold_usd",
    "outcome",
    "reason",
    "stage",
]


class SensitivityError(RuntimeError):
    """The baseline cannot safely support a sensitivity replay."""


def _threshold_value(row: Mapping[str, Any], spec: Mapping[str, Any]) -> float | None:
    hourly = finite_float(row.get("hour_futures_notional_usd"))
    if hourly is None or hourly < 0:
        return None
    return max(float(spec["min_usd"]), float(spec["hourly_fraction"]) * hourly)


def _threshold_passes(row: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    threshold = _threshold_value(row, spec)
    long_liq = finite_float(row.get("long_liq_notional_usd"))
    return (
        threshold is not None
        and long_liq is not None
        and long_liq >= threshold
    )


def _row_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("symbol"),
        row.get("pump_ts"),
        row.get("correction_ts"),
        row.get("pump_episode_end_ts"),
    )


def _stage_for_row(row: Mapping[str, Any]) -> str:
    outcome = row.get("outcome")
    reason = str(row.get("reason") or "")
    if outcome in RESOLVED_OUTCOMES:
        return "resolved_outcome"
    if outcome == "no_outcome_in_window":
        return "no_outcome_in_window"
    if reason == "correction_not_found_in_12h":
        return "correction_not_found_in_12h"
    if reason == "long_liquidation_threshold_not_met":
        return "liquidation_burst_stage"
    if reason.startswith(("liquidation_coverage_", "missing_15m_hour_notional")):
        return "liquidation_burst_stage"
    if reason.startswith(("missing_5m_", "large_5m_", "candle_fetch_error")):
        return "large_5m_flow_stage"
    if reason.startswith("missing_15m_outcome_coverage"):
        return "outcome_coverage_stage"
    if reason.startswith("replay_error"):
        return "replay_error_stage"
    return "other_unresolved"


def _sufficiency(cohort: str, resolved_n: int) -> str:
    if cohort != "primary":
        return "controls_any_n"
    if resolved_n >= PRIMARY_INFERENCE_N:
        return "full_inference"
    if resolved_n >= PRIMARY_DESCRIPTIVE_N:
        return "descriptive_only"
    return "case_log_only"


def _summary(
    rows: Sequence[Mapping[str, Any]],
    cohort: str,
    *,
    label: str | None = None,
) -> dict[str, Any]:
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
    resolved_n = len(resolved)
    stage_counts = Counter(_stage_for_row(row) for row in rows)
    return {
        "cohort": label or cohort,
        "event_rows": len(rows),
        "resolved_n": resolved_n,
        "success_n": success_n,
        "success_continuation_n": continuation_n,
        "success_retest_hold_n": retest_n,
        "failure_n": failure_n,
        "no_outcome_n": no_outcome_n,
        "unresolved_precondition_n": len(rows) - resolved_n - no_outcome_n,
        "success_rate": success_n / resolved_n if resolved_n else None,
        "sufficiency": _sufficiency(cohort, resolved_n),
        "stage_counts": dict(sorted(stage_counts.items())),
    }


def _control_symbols(report: Mapping[str, Any]) -> list[str]:
    config = report.get("config")
    if isinstance(config, Mapping):
        symbols = config.get("control_symbols")
        if isinstance(symbols, list) and all(isinstance(item, str) for item in symbols):
            return list(dict.fromkeys(symbols))
    return sorted(
        {
            str(row.get("symbol"))
            for row in report.get("coverage", [])
            if row.get("cohort") == "control" and row.get("symbol")
        }
    )


def _cohort_rows(
    events: Sequence[Mapping[str, Any]],
    control_symbols: Sequence[str],
) -> dict[str, list[Mapping[str, Any]]]:
    result = {
        "primary": [row for row in events if row.get("cohort") == "primary"],
        "control": [row for row in events if row.get("cohort") == "control"],
    }
    for symbol in control_symbols:
        result[f"control:{symbol}"] = [
            row
            for row in events
            if row.get("cohort") == "control" and row.get("symbol") == symbol
        ]
    return result


def _empty_downstream(reason: str, *, coverage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "flow_ts": None,
        "flow_utc": None,
        "flow_notional_usd": None,
        "flow_baseline_median_usd": None,
        "flow_threshold_usd": None,
        "outcome_ts": None,
        "outcome_utc": None,
        "support_retest_ts": None,
        "support_retest_utc": None,
        "outcome": "not_reached",
        "reason": reason,
    }
    if coverage is not None:
        result["replay_coverage"] = dict(coverage)
    return result


def _fetch_downstream(
    row: Mapping[str, Any],
    client: GateClient,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    liq_end = int(row["liq_window_end_ts"])
    flow_start = liq_end
    flow_end = flow_start + FLOW_LOOKAHEAD_HOURS * 60 * 60
    candles_5m, status_5m = client.fetch_candles(
        gate_contract(symbol),
        "5m",
        flow_start - DAY,
        flow_end,
    )
    coverage = {
        "five_minute_status": status_5m.status,
        "five_minute_reason": status_5m.reason,
        "outcome_15m_status": "not_requested",
        "outcome_15m_reason": "",
    }
    if status_5m.status != "complete":
        return _empty_downstream(
            f"missing_5m_coverage:{status_5m.reason or status_5m.status}",
            coverage=coverage,
        )
    flow, flow_reason = find_large_flow(
        candles_5m,
        start_ts=flow_start,
        end_ts=flow_end,
    )
    if flow is None:
        return _empty_downstream(flow_reason, coverage=coverage)

    baseline = [
        candle.quote_notional
        for candle in candles_5m
        if flow.ts - DAY <= candle.ts < flow.ts
    ]
    baseline_median = statistics.median(baseline) if baseline else 0.0
    outcome_start = flow.ts + FIVE_MINUTES
    outcome_end = outcome_start + OUTCOME_HOURS * 60 * 60
    candles_15m, status_15m = client.fetch_candles(
        gate_contract(symbol),
        "15m",
        outcome_start,
        outcome_end,
    )
    coverage["outcome_15m_status"] = status_15m.status
    coverage["outcome_15m_reason"] = status_15m.reason
    if status_15m.status != "complete":
        return _empty_downstream(
            f"missing_15m_outcome_coverage:{status_15m.reason or status_15m.status}",
            coverage=coverage,
        )
    outcome, outcome_ts, outcome_reason = classify_support_retest(
        candles_15m,
        support=float(row["support"]),
        flow_high=flow.high,
        start_ts=outcome_start,
        end_ts=outcome_end,
    )
    return {
        "flow_ts": flow.ts,
        "flow_utc": utc(flow.ts),
        "flow_notional_usd": flow.quote_notional,
        "flow_baseline_median_usd": baseline_median,
        "flow_threshold_usd": baseline_median * FLOW_MULTIPLIER,
        "outcome_ts": outcome_ts,
        "outcome_utc": utc(outcome_ts),
        "support_retest_ts": outcome_ts if outcome == "success_retest_hold" else None,
        "support_retest_utc": (
            utc(outcome_ts) if outcome == "success_retest_hold" else None
        ),
        "outcome": outcome,
        "reason": outcome_reason,
        "replay_coverage": coverage,
    }


def _collect_replays(
    events: Sequence[Mapping[str, Any]],
    client: GateClient,
    *,
    workers: int,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    if workers < 1:
        raise SensitivityError("workers must be positive")
    candidates: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in events:
        if row.get("reason") != "long_liquidation_threshold_not_met":
            continue
        if not any(_threshold_passes(row, spec) for spec in THRESHOLD_GRID[:-2]):
            continue
        if not row.get("correction_ts") or not row.get("support"):
            continue
        candidates.setdefault(_row_key(row), row)

    results: dict[tuple[Any, ...], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_downstream, row, client): key
            for key, row in candidates.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = _empty_downstream(f"replay_error:{exc}")
    return results


def _apply_threshold(
    row: Mapping[str, Any],
    spec: Mapping[str, Any],
    replays: Mapping[tuple[Any, ...], Mapping[str, Any]],
) -> dict[str, Any]:
    current = dict(row)
    threshold = _threshold_value(row, spec)
    if threshold is not None:
        current["liq_threshold_usd"] = threshold
    current["threshold_passed"] = _threshold_passes(row, spec)

    if threshold is None or finite_float(row.get("long_liq_notional_usd")) is None:
        return current
    if not current["threshold_passed"]:
        for field in (
            "flow_ts",
            "flow_utc",
            "flow_notional_usd",
            "flow_baseline_median_usd",
            "flow_threshold_usd",
            "outcome_ts",
            "outcome_utc",
            "support_retest_ts",
            "support_retest_utc",
        ):
            current[field] = None
        current["outcome"] = "not_reached"
        current["reason"] = "long_liquidation_threshold_not_met"
        current.pop("replay_coverage", None)
        return current

    if row.get("reason") == "long_liquidation_threshold_not_met":
        downstream = replays.get(_row_key(row))
        if downstream is None:
            downstream = _empty_downstream("replay_error:missing_cached_replay")
        current.update(downstream)
    return current


def _quality_vs_baseline(
    incremental: Mapping[str, Any] | None,
    baseline: Mapping[str, Any],
) -> str:
    if incremental is None:
        return "not_applicable_strictest_threshold"
    if incremental["resolved_n"] == 0:
        return "no_incremental_resolved_evidence"
    if (
        incremental["success_rate"] is not None
        and baseline["success_rate"] is not None
        and incremental["success_rate"] > baseline["success_rate"]
        and incremental["failure_n"] <= baseline["failure_n"]
    ):
        return "clearly_better"
    return "not_clearly_better"


def _decision(
    cumulative: Mapping[str, Any],
    quality: str,
) -> str:
    if cumulative["sufficiency"] == "full_inference":
        if quality == "clearly_better":
            return "research_candidate_only"
        return "full_sample_but_no_clear_incremental_quality_gain"
    if cumulative["sufficiency"] == "descriptive_only":
        return "descriptive_only"
    return "case_log_only"


def build_sensitivity(
    report: Mapping[str, Any],
    *,
    replays: Mapping[tuple[Any, ...], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    events = report.get("events")
    if not isinstance(events, list):
        raise SensitivityError("baseline report events are not a list")
    replays = replays or {}
    control_symbols = _control_symbols(report)
    threshold_rows: dict[str, list[dict[str, Any]]] = {}
    cumulative: list[dict[str, Any]] = []
    for spec in THRESHOLD_GRID:
        rows = [_apply_threshold(row, spec, replays) for row in events]
        threshold_rows[spec["id"]] = rows
        for cohort, cohort_events in _cohort_rows(rows, control_symbols).items():
            base_cohort = "primary" if cohort == "primary" else "control"
            cumulative.append(
                {
                    **spec,
                    **_summary(cohort_events, base_cohort, label=cohort),
                }
            )

    baseline_primary = next(
        row
        for row in cumulative
        if row["threshold_id"] == BASELINE_THRESHOLD_ID
        and row["cohort"] == "primary"
    )
    incremental: list[dict[str, Any]] = []
    incremental_events: dict[str, list[dict[str, Any]]] = {}
    for index in range(len(THRESHOLD_GRID) - 1):
        current_spec = THRESHOLD_GRID[index]
        stricter_spec = THRESHOLD_GRID[index + 1]
        current_rows = threshold_rows[current_spec["id"]]
        band_indexes = [
            row_index
            for row_index, row in enumerate(events)
            if _threshold_passes(row, current_spec)
            and not _threshold_passes(row, stricter_spec)
        ]
        band_key = f"{current_spec['id']}__vs__{stricter_spec['id']}"
        band_rows = [current_rows[row_index] for row_index in band_indexes]
        for cohort, cohort_events in _cohort_rows(
            band_rows, control_symbols
        ).items():
            base_cohort = "primary" if cohort == "primary" else "control"
            row = {
                "threshold_id": current_spec["id"],
                "threshold_label": current_spec["label"],
                "stricter_threshold_id": stricter_spec["id"],
                "stricter_threshold_label": stricter_spec["label"],
                **_summary(cohort_events, base_cohort, label=cohort),
            }
            if cohort == "primary":
                row["incremental_quality"] = _quality_vs_baseline(
                    row, baseline_primary
                )
            else:
                row["incremental_quality"] = "controls_not_pooled"
            incremental.append(row)
        incremental_events[band_key] = [
            {
                "threshold_id": current_spec["id"],
                "threshold_label": current_spec["label"],
                "stricter_threshold_id": stricter_spec["id"],
                "stricter_threshold_label": stricter_spec["label"],
                "symbol": current_rows[index]["symbol"],
                "cohort": current_rows[index]["cohort"],
                "pump_ts": current_rows[index]["pump_ts"],
                "pump_utc": current_rows[index]["pump_utc"],
                "threshold_passed": current_rows[index]["threshold_passed"],
                "long_liq_notional_usd": current_rows[index].get(
                    "long_liq_notional_usd"
                ),
                "hour_futures_notional_usd": current_rows[index].get(
                    "hour_futures_notional_usd"
                ),
                "liq_threshold_usd": current_rows[index].get("liq_threshold_usd"),
                "outcome": current_rows[index].get("outcome"),
                "reason": current_rows[index].get("reason"),
                "stage": _stage_for_row(current_rows[index]),
            }
            for index in band_indexes
        ]

    quality_by_threshold = {
        row["threshold_id"]: row["incremental_quality"]
        for row in incremental
        if row["cohort"] == "primary"
    }
    threshold_decisions = []
    for spec in THRESHOLD_GRID:
        primary = next(
            row
            for row in cumulative
            if row["threshold_id"] == spec["id"] and row["cohort"] == "primary"
        )
        quality = (
            quality_by_threshold.get(spec["id"])
            if spec["id"] != THRESHOLD_GRID[-1]["id"]
            else "not_applicable_strictest_threshold"
        )
        threshold_decisions.append(
            {
                "threshold_id": spec["id"],
                "threshold_label": spec["label"],
                "sufficiency": primary["sufficiency"],
                "resolved_n": primary["resolved_n"],
                "incremental_quality": quality,
                "decision": _decision(primary, quality),
            }
        )
    full_and_better = [
        row
        for row in threshold_decisions
        if row["sufficiency"] == "full_inference"
        and row["incremental_quality"] == "clearly_better"
    ]
    global_decision = (
        "research_candidate_only_no_production_change"
        if full_and_better
        else "descriptive_only_no_threshold_optimization"
    )
    return {
        "report_type": "historical_liquidation_pattern_threshold_sensitivity",
        "production_changes": False,
        "source_report_generated_utc": report.get("generated_utc"),
        "control_symbols": control_symbols,
        "thresholds": list(THRESHOLD_GRID),
        "cumulative": cumulative,
        "incremental": incremental,
        "threshold_decisions": threshold_decisions,
        "global_decision": global_decision,
        "incremental_events": incremental_events,
        "threshold_rows": threshold_rows,
    }


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fields),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _validate_contract(report: Mapping[str, Any]) -> None:
    if report.get("production_changes") is not False:
        raise SensitivityError("baseline is not marked read-only")
    config = report.get("config")
    if not isinstance(config, Mapping):
        raise SensitivityError("baseline has no configuration")
    expected = {
        "lookback_days": 91,
        "top_n": 50,
        "pump_return": 0.15,
        "pump_lookback_bars": 32,
        "correction_return": 0.08,
        "correction_bars": 48,
        "flow_multiplier": 3.0,
        "flow_lookahead_hours": 6,
        "outcome_hours": 24,
        "liquidation_min_usd": 100_000.0,
        "liquidation_hourly_fraction": 0.02,
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise SensitivityError(
                f"baseline comparison contract mismatch for {key}: "
                f"{config.get(key)!r} != {value!r}"
            )
    if config.get("control_symbols") != ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        raise SensitivityError("baseline control symbols are not BTC/ETH/SOL")
    preflight = report.get("preflight")
    if not isinstance(preflight, Mapping) or preflight.get("ok") is not True:
        raise SensitivityError("baseline preflight did not pass")
    mapping = preflight.get("sign_to_side")
    if not isinstance(mapping, Mapping) or list(mapping.values()).count("long") != 1:
        raise SensitivityError("baseline does not contain one calibrated long sign")
    baseline = next(
        spec for spec in THRESHOLD_GRID if spec["id"] == BASELINE_THRESHOLD_ID
    )
    if (
        config.get("liquidation_min_usd") != baseline["min_usd"]
        or config.get("liquidation_hourly_fraction")
        != baseline["hourly_fraction"]
    ):
        raise SensitivityError("baseline threshold does not match fixed grid")


def write_sensitivity(
    report_dir: Path,
    output_dir: Path,
    *,
    client: GateClient,
    workers: int = 2,
) -> dict[str, Any]:
    if report_dir.resolve() == output_dir.resolve():
        raise SensitivityError("refusing to overwrite the baseline report")
    report_path = report_dir / "report.json"
    report = load_completed_scan_report(report_dir)
    _validate_contract(report)
    source_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    replays = _collect_replays(report["events"], client, workers=workers)
    result = build_sensitivity(report, replays=replays)
    result["source_report_sha256"] = source_hash
    result["replay_count"] = len(replays)
    result["replay_coverage"] = {
        str(key): value.get("replay_coverage", {})
        for key, value in replays.items()
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "thresholds.json").write_text(
        json.dumps(
            {
                "thresholds": list(THRESHOLD_GRID),
                "baseline_threshold_id": BASELINE_THRESHOLD_ID,
                "source_report_sha256": source_hash,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.json").write_text(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"threshold_rows", "incremental_events"}
            },
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cumulative = result["cumulative"]
    cumulative_csv = [
        {
            **row,
            "stage_counts": json.dumps(row["stage_counts"], sort_keys=True),
        }
        for row in cumulative
    ]
    _write_csv(output_dir / "cumulative.csv", cumulative_csv, CUMULATIVE_FIELDS)
    incremental_csv = [
        {
            **row,
            "stage_counts": json.dumps(row["stage_counts"], sort_keys=True),
        }
        for row in result["incremental"]
    ]
    _write_csv(output_dir / "incremental.csv", incremental_csv, INCREMENTAL_FIELDS)
    event_rows = []
    for spec in THRESHOLD_GRID:
        for row in result["threshold_rows"][spec["id"]]:
            event_rows.append(
                {
                    "threshold_id": spec["id"],
                    "threshold_label": spec["label"],
                    "min_usd": spec["min_usd"],
                    "hourly_fraction": spec["hourly_fraction"],
                    **row,
                }
            )
    _write_csv(output_dir / "events.csv", event_rows, EVENT_OUTPUT_FIELDS)
    _write_csv(
        output_dir / "incremental_events.csv",
        [row for rows in result["incremental_events"].values() for row in rows],
        INCREMENTAL_EVENT_FIELDS,
    )

    baseline_primary = next(
        row
        for row in cumulative
        if row["threshold_id"] == BASELINE_THRESHOLD_ID
        and row["cohort"] == "primary"
    )
    lines = [
        "# Historical liquidation threshold sensitivity",
        "",
        "**Read-only comparison. The baseline scan and production behavior are unchanged.**",
        "",
        f"- Source scan generated: **{result['source_report_generated_utc']}**",
        f"- Source report SHA-256: `{source_hash}`",
        f"- Downstream replays fetched from Gate: **{len(replays)}**",
        "",
        "## Fixed preregistered grid",
        "",
        "| Threshold | Minimum USD | Hourly fraction | Role |",
        "|---|---:|---:|---|",
    ]
    for spec in THRESHOLD_GRID:
        role = "baseline" if spec["id"] == BASELINE_THRESHOLD_ID else "sensitivity"
        lines.append(
            f"| {spec['label']} | ${spec['min_usd']:,.0f} | "
            f"{spec['hourly_fraction']:.1%} | {role} |"
        )
    lines += [
        "",
        "## Cumulative primary results",
        "",
        "| Threshold | Event rows | Resolved n | Continuation | Retest/hold | Failure | No outcome | Success rate | Sufficiency | Decision |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for decision in result["threshold_decisions"]:
        row = next(
            item
            for item in cumulative
            if item["threshold_id"] == decision["threshold_id"]
            and item["cohort"] == "primary"
        )
        rate = "—" if row["success_rate"] is None else f"{row['success_rate']:.3f}"
        lines.append(
            f"| {row['threshold_label']} | {row['event_rows']} | "
            f"{row['resolved_n']} | {row['success_continuation_n']} | "
            f"{row['success_retest_hold_n']} | {row['failure_n']} | "
            f"{row['no_outcome_n']} | {rate} | {row['sufficiency']} | "
            f"{decision['decision']} |"
        )
    lines += [
        "",
        "## Cumulative controls",
        "",
        "| Threshold | Cohort | Event rows | Resolved n | Continuation | Retest/hold | Failure | No outcome |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in cumulative:
        if row["cohort"] == "primary":
            continue
        lines.append(
            f"| {row['threshold_label']} | {row['cohort']} | {row['event_rows']} | "
            f"{row['resolved_n']} | {row['success_continuation_n']} | "
            f"{row['success_retest_hold_n']} | {row['failure_n']} | "
            f"{row['no_outcome_n']} |"
        )
    lines += [
        "",
        "## Adjacent incremental bands",
        "",
        "Each band contains rows that pass the softer threshold and fail the immediately stricter threshold. "
        "`no_outcome_in_window` is excluded from `resolved n`; controls are not pooled.",
        "",
        "| Softer threshold | Stricter threshold | Cohort | Rows | Resolved n | Success | Failure | No outcome | Success rate | Quality |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result["incremental"]:
        rate = "—" if row["success_rate"] is None else f"{row['success_rate']:.3f}"
        lines.append(
            f"| {row['threshold_label']} | {row['stricter_threshold_label']} | "
            f"{row['cohort']} | {row['event_rows']} | {row['resolved_n']} | "
            f"{row['success_n']} | {row['failure_n']} | {row['no_outcome_n']} | "
            f"{rate} | {row['incremental_quality']} |"
        )
    lines += [
        "",
        "## Decision",
        "",
        f"- Global decision: **{result['global_decision']}**",
        f"- Baseline remains **{baseline_primary['sufficiency']}** with resolved `n={baseline_primary['resolved_n']}` "
        f"and success rate {baseline_primary['success_rate']:.3f}.",
        "- A larger `n` alone is not treated as better quality; the comparison uses incremental resolved success/failure rates.",
        "- No production scoring, filters, whitelist, execution, TP/SL, polling, reserve protection, or Telegram behavior changed.",
        "",
        "## Replay coverage",
        "",
        "| Replay key | Coverage |",
        "|---|---|",
    ]
    for key, coverage in result["replay_coverage"].items():
        lines.append(f"| `{key}` | `{json.dumps(coverage, sort_keys=True)}` |")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


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
        default=Path("outcome_historical_liquidation_pattern_sensitivity"),
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--api-base", default=None)
    args = parser.parse_args(argv)
    try:
        kwargs = {} if args.api_base is None else {"base_url": args.api_base}
        result = write_sensitivity(
            args.report_dir,
            args.out,
            client=GateClient(**kwargs),
            workers=args.workers,
        )
    except (OSError, SensitivityError, ValueError) as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Wrote {args.out / 'report.md'} "
        f"(replays={result['replay_count']}, "
        f"decision={result['global_decision']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())