#!/usr/bin/env python3
"""Read-only TP-vs-SL analysis for the strong-signal shadow strategies.

The script reads resolved demo positions and the append-only runtime log.  It
never imports app.py, writes to SQLite, or changes signal behavior.  Runtime
log values are retained as rounded observations with explicit provenance.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Iterable


TARGET_STRATEGIES = (
    "ema_cross_confirmed",
    "overheated_early",
    "ema_cross",
    "overheated_confirmed",
)
MIN_GROUP_N = 20
BOOTSTRAP_ITERATIONS = 800
PERMUTATION_ITERATIONS = 800
RANDOM_SEED = 143
LOG_MATCH_WINDOWS = {
    "ema_cross": 120.0,
    "overheated_early": 120.0,
    "ema_cross_confirmed": 300.0,
    "overheated_confirmed": 300.0,
}

LOG_TIMESTAMP_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)"
)
EMA_CROSS_RE = re.compile(
    r"EMA cross shadow (?P<symbol>\S+) (?P<direction>LONG|SHORT): "
    r"price=(?P<price>[-\d.eE]+) gap=(?P<gap>[-\d.eE]+)% "
    r"sl=(?P<sl>[-\d.eE]+) tp=(?P<tp>[-\d.eE]+)"
)
OVERHEATED_EARLY_RE = re.compile(
    r"overheated_early (?:PRECHECK|EVAL) (?P<symbol>\S+): "
    r"pct24=(?P<pct24>[-\d.eE]+)%.*?rsi=(?P<rsi>[-\d.eE]+)"
)
CONFIRMED_RE = re.compile(
    r"cont_confirmed: (?P<strategy>ema_cross_confirmed|overheated_confirmed) "
    r"(?P<symbol>\S+) (?P<direction>LONG|SHORT) "
    r"confirmed(?:#(?P<number>\d+))? @(?P<entry>[-\d.eE]+) "
    r"signal=(?P<signal>[-\d.eE]+) vol=(?P<volume>[-\d.eE]+)x"
    r"(?: tp_mult=(?P<tp_mult>[-\d.eE]+)x)? age=(?P<age>\d+)min"
)

FEATURE_META = {
    "risk_pct": {
        "label": "SL distance from entry (%)",
        "provenance": "exact_persisted_derived",
        "description": "abs(entry_price - sl_price) / entry_price",
    },
    "reward_pct": {
        "label": "TP distance from entry (%)",
        "provenance": "exact_persisted_derived",
        "description": "abs(tp_price - entry_price) / entry_price",
    },
    "reward_risk": {
        "label": "TP/SL distance ratio",
        "provenance": "exact_persisted_derived",
        "description": "reward_pct / risk_pct",
    },
    "entry_vs_signal_pct": {
        "label": "Directional entry move from signal (%)",
        "provenance": "exact_persisted_derived",
        "description": "direction-adjusted entry_price vs signal_price",
    },
    "ema_gap_pct_log": {
        "label": "EMA cross gap (%)",
        "provenance": "runtime_log_rounded",
        "description": "EMA(9)-EMA(21) gap emitted by the signal path",
    },
    "overheated_pct24_log": {
        "label": "Overheated 24h move (%)",
        "provenance": "runtime_log_rounded",
        "description": "pct24 emitted by the overheated early signal path",
    },
    "overheated_rsi_log": {
        "label": "Overheated RSI",
        "provenance": "runtime_log_rounded",
        "description": "RSI emitted by the overheated early signal path",
    },
    "confirmation_volume_ratio_log": {
        "label": "Confirmation volume ratio (x)",
        "provenance": "runtime_log_rounded",
        "description": "completed-candle volume / 10-bar average",
    },
    "confirmation_number_log": {
        "label": "Confirmation number",
        "provenance": "runtime_log_exact_integer",
        "description": "confirmation count emitted by continuation telemetry",
    },
    "confirmation_age_min_log": {
        "label": "Confirmation age (minutes)",
        "provenance": "runtime_log_exact_integer",
        "description": "age of the parent signal at confirmation",
    },
}


def fmt_ts(value: int | float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def parse_log_ts(line: str) -> float | None:
    match = LOG_TIMESTAMP_RE.search(line)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match["stamp"], "%Y-%m-%d %H:%M:%S,%f")
        return stamp.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(value)
    return parsed


def parse_runtime_log(path: Path) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    events: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return events
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            ts = parse_log_ts(line)
            if ts is None:
                continue
            match = EMA_CROSS_RE.search(line)
            if match:
                events[(
                    "ema_cross", match["symbol"], match["direction"]
                )].append({
                    "ts": ts,
                    "ema_gap_pct_log": _number(match["gap"]),
                })
                continue
            match = OVERHEATED_EARLY_RE.search(line)
            if match:
                events[(
                    "overheated_early", match["symbol"], "LONG"
                )].append({
                    "ts": ts,
                    "overheated_pct24_log": _number(match["pct24"]),
                    "overheated_rsi_log": _number(match["rsi"]),
                })
                continue
            match = CONFIRMED_RE.search(line)
            if match:
                strategy = match["strategy"]
                events[(
                    strategy, match["symbol"], match["direction"]
                )].append({
                    "ts": ts,
                    "confirmation_volume_ratio_log": _number(match["volume"]),
                    "confirmation_number_log": int(match["number"] or 1),
                    "confirmation_age_min_log": int(match["age"]),
                    "confirmation_tp_mult_log": (
                        _number(match["tp_mult"])
                        if match["tp_mult"] is not None else None
                    ),
                    "confirmed_entry_log": _number(match["entry"]),
                    "confirmed_signal_log": _number(match["signal"]),
                })
    for values in events.values():
        values.sort(key=lambda event: event["ts"])
    return events


def nearest_event(
    events: dict[tuple[str, str, str], list[dict[str, Any]]],
    strategy: str,
    symbol: str,
    direction: str,
    ts_open: int,
) -> dict[str, Any]:
    values = events.get((strategy, symbol, direction), [])
    if not values:
        return {}
    timestamps = [float(event["ts"]) for event in values]
    index = bisect_left(timestamps, float(ts_open))
    candidates = []
    if index < len(values):
        candidates.append(values[index])
    if index:
        candidates.append(values[index - 1])
    event = min(candidates, key=lambda item: abs(item["ts"] - ts_open))
    if abs(event["ts"] - ts_open) > LOG_MATCH_WINDOWS[strategy]:
        return {}
    return dict(event)


def load_resolved(db_path: Path) -> list[dict[str, Any]]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type,
                       is_shadow, signal_price
                  FROM demo_positions
                 WHERE is_shadow=1
                   AND alert_type IN (?, ?, ?, ?)
                   AND direction IN ('LONG', 'SHORT')
                   AND status IN ('tp', 'sl')
                   AND entry_price > 0
                   AND sl_price > 0
                   AND tp_price > 0
                   AND exit_price IS NOT NULL
                   AND exit_price > 0
                 ORDER BY ts_open, id
                """,
                TARGET_STRATEGIES,
            )
        ]
    finally:
        conn.close()
    return rows


def enrich_rows(
    rows: Iterable[dict[str, Any]],
    events: dict[tuple[str, str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    enriched = []
    for source in rows:
        row = dict(source)
        entry = float(row["entry_price"])
        stop = float(row["sl_price"])
        target = float(row["tp_price"])
        exit_price = float(row["exit_price"])
        direction = str(row["direction"])
        risk = abs(stop - entry)
        reward = abs(target - entry)
        result_r = (
            (exit_price - entry) / risk
            if direction == "LONG"
            else (entry - exit_price) / risk
        )
        signal_price = row.get("signal_price")
        directional_entry_move = None
        if signal_price is not None and float(signal_price) > 0:
            signal = float(signal_price)
            directional_entry_move = (
                (entry - signal) / signal * 100.0
                if direction == "LONG"
                else (signal - entry) / signal * 100.0
            )
        event = nearest_event(
            events,
            str(row["alert_type"]),
            str(row["symbol"]),
            direction,
            int(row["ts_open"]),
        )
        row.update({
            "ts_open_utc": fmt_ts(int(row["ts_open"])),
            "ts_close_utc": fmt_ts(row.get("ts_close")),
            "outcome": "tp" if row["status"] == "tp" else "sl",
            "result_r": result_r if math.isfinite(result_r) else None,
            "risk_pct": risk / entry * 100.0,
            "reward_pct": reward / entry * 100.0,
            "reward_risk": reward / risk if risk > 0 else None,
            "entry_vs_signal_pct": directional_entry_move,
            "log_match_ts": event.get("ts"),
            "log_match_delta_sec": (
                abs(float(event["ts"]) - int(row["ts_open"]))
                if event else None
            ),
        })
        row.update(event)
        enriched.append(row)
    return enriched


def _finite_values(rows: Iterable[dict[str, Any]], field: str) -> list[float]:
    values = []
    for row in rows:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def describe(rows: Iterable[dict[str, Any]], field: str) -> dict[str, Any]:
    values = _finite_values(rows, field)
    if not values:
        return {
            "n": 0, "coverage_pct": 0.0, "mean": None, "median": None,
            "stdev": None, "min": None, "max": None,
        }
    return {
        "n": len(values),
        "mean": round(mean(values), 6),
        "median": round(median(values), 6),
        "stdev": round(stdev(values), 6) if len(values) > 1 else 0.0,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(row["outcome"] == "tp" for row in rows)
    sl = sum(row["outcome"] == "sl" for row in rows)
    rs = [
        float(row["result_r"])
        for row in rows
        if row.get("result_r") is not None
        and math.isfinite(float(row["result_r"]))
    ]
    n = tp + sl
    return {
        "n": n,
        "tp": tp,
        "sl": sl,
        "resolved_wr_pct": round(100.0 * tp / n, 2) if n else None,
        "avg_r": round(mean(rs), 6) if rs else None,
        "sample_status": "ready" if n >= MIN_GROUP_N else "insufficient_sample",
    }


def _rank_auc(higher_values: list[float], lower_values: list[float]) -> float:
    if not higher_values or not lower_values:
        return float("nan")
    wins = ties = 0.0
    for higher in higher_values:
        for lower in lower_values:
            if higher > lower:
                wins += 1.0
            elif higher == lower:
                ties += 1.0
    return (wins + ties * 0.5) / (len(higher_values) * len(lower_values))


def _cliffs_delta(tp_values: list[float], sl_values: list[float]) -> float:
    auc = _rank_auc(tp_values, sl_values)
    return 2.0 * auc - 1.0 if math.isfinite(auc) else float("nan")


def _bootstrap_delta_ci(
    tp_values: list[float], sl_values: list[float], rng: random.Random
) -> tuple[float | None, float | None]:
    if not tp_values or not sl_values:
        return None, None
    sampled = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        tp = [rng.choice(tp_values) for _ in tp_values]
        sl = [rng.choice(sl_values) for _ in sl_values]
        sampled.append(_cliffs_delta(tp, sl))
    sampled.sort()
    return (
        round(sampled[int(0.025 * len(sampled))], 6),
        round(sampled[int(0.975 * len(sampled))], 6),
    )


def _permutation_p_value(
    tp_values: list[float], sl_values: list[float], rng: random.Random
) -> float | None:
    observed = abs(_cliffs_delta(tp_values, sl_values))
    if not math.isfinite(observed):
        return None
    combined = tp_values + sl_values
    tp_n = len(tp_values)
    exceed = 0
    for _ in range(PERMUTATION_ITERATIONS):
        shuffled = list(combined)
        rng.shuffle(shuffled)
        value = abs(_cliffs_delta(shuffled[:tp_n], shuffled[tp_n:]))
        if value >= observed - 1e-12:
            exceed += 1
    return round((exceed + 1) / (PERMUTATION_ITERATIONS + 1), 6)


def compare_feature(
    tp_rows: list[dict[str, Any]],
    sl_rows: list[dict[str, Any]],
    field: str,
    rng: random.Random,
) -> dict[str, Any]:
    tp_values = _finite_values(tp_rows, field)
    sl_values = _finite_values(sl_rows, field)
    tp_desc = describe(tp_rows, field)
    sl_desc = describe(sl_rows, field)
    result = {
        "feature": field,
        "label": FEATURE_META[field]["label"],
        "provenance": FEATURE_META[field]["provenance"],
        "description": FEATURE_META[field]["description"],
        "tp_first": tp_desc,
        "sl_first": sl_desc,
        "comparison_allowed": (
            len(tp_values) >= MIN_GROUP_N and len(sl_values) >= MIN_GROUP_N
        ),
        "median_diff_tp_minus_sl": (
            round(tp_desc["median"] - sl_desc["median"], 6)
            if tp_desc["median"] is not None and sl_desc["median"] is not None
            else None
        ),
        "cliffs_delta_tp_higher": None,
        "bootstrap_95ci": [None, None],
        "permutation_p_two_sided": None,
    }
    if result["comparison_allowed"]:
        result["cliffs_delta_tp_higher"] = round(
            _cliffs_delta(tp_values, sl_values), 6
        )
        low, high = _bootstrap_delta_ci(tp_values, sl_values, rng)
        result["bootstrap_95ci"] = [low, high]
        result["permutation_p_two_sided"] = _permutation_p_value(
            tp_values, sl_values, rng
        )
    return result


def _classification(
    rows: list[dict[str, Any]], field: str, threshold: float, direction: str
) -> dict[str, Any]:
    predicted_tp = lambda value: value >= threshold if direction == "gte" else value <= threshold
    matrix = {"tp_pred_tp": 0, "tp_pred_sl": 0, "sl_pred_tp": 0, "sl_pred_sl": 0}
    used = 0
    for row in rows:
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        used += 1
        is_pred_tp = predicted_tp(value)
        if row["outcome"] == "tp":
            matrix["tp_pred_tp" if is_pred_tp else "tp_pred_sl"] += 1
        else:
            matrix["sl_pred_tp" if is_pred_tp else "sl_pred_sl"] += 1
    total = sum(matrix.values())
    correct = matrix["tp_pred_tp"] + matrix["sl_pred_sl"]
    tp_pred_total = matrix["tp_pred_tp"] + matrix["sl_pred_tp"]
    sl_pred_total = matrix["tp_pred_sl"] + matrix["sl_pred_sl"]
    tp_total = matrix["tp_pred_tp"] + matrix["tp_pred_sl"]
    sl_total = matrix["sl_pred_tp"] + matrix["sl_pred_sl"]
    accuracy = correct / total if total else None
    tp_precision = (
        matrix["tp_pred_tp"] / tp_pred_total if tp_pred_total else None
    )
    sl_precision = (
        matrix["sl_pred_sl"] / sl_pred_total if sl_pred_total else None
    )
    balanced_accuracy = (
        (
            matrix["tp_pred_tp"] / tp_total
            + matrix["sl_pred_sl"] / sl_total
        ) / 2.0
        if tp_total and sl_total else None
    )
    return {
        **matrix,
        "used_n": used,
        "accuracy": round(accuracy, 6) if accuracy is not None else None,
        "balanced_accuracy": (
            round(balanced_accuracy, 6)
            if balanced_accuracy is not None else None
        ),
        "precision_tp": round(tp_precision, 6) if tp_precision is not None else None,
        "precision_sl": round(sl_precision, 6) if sl_precision is not None else None,
    }


def find_candidate(
    rows: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        item for item in comparisons
        if item["comparison_allowed"]
        and item["tp_first"]["n"] / sum(row["outcome"] == "tp" for row in rows) >= 0.8
        and item["sl_first"]["n"] / sum(row["outcome"] == "sl" for row in rows) >= 0.8
        and item["permutation_p_two_sided"] is not None
        and item["permutation_p_two_sided"] <= 0.05
        and item["bootstrap_95ci"][0] is not None
        and (
            item["bootstrap_95ci"][0] > 0.0
            or item["bootstrap_95ci"][1] < 0.0
        )
        and abs(item["cliffs_delta_tp_higher"] or 0.0) >= 0.33
    ]
    candidates = []
    for item in eligible:
        field = item["feature"]
        values = sorted({
            float(row[field]) for row in rows
            if row.get(field) not in (None, "")
            and math.isfinite(float(row[field]))
        })
        if len(values) < 2:
            continue
        thresholds = [
            (left + right) / 2.0
            for left, right in zip(values, values[1:])
        ]
        for threshold in thresholds:
            for direction in ("gte", "lte"):
                audit = _classification(rows, field, threshold, direction)
                if audit["used_n"] < MIN_GROUP_N * 2:
                    continue
                if audit["balanced_accuracy"] is None:
                    continue
                candidates.append({
                    "feature": field,
                    "label": item["label"],
                    "provenance": item["provenance"],
                    "rule": (
                        f"{item['label']} {'≥' if direction == 'gte' else '≤'} "
                        f"{threshold:.6g} predicts TP"
                    ),
                    "threshold": round(threshold, 6),
                    "operator": direction,
                    "criterion": {
                        "minimum_group_n": MIN_GROUP_N,
                        "minimum_abs_cliffs_delta": 0.33,
                        "maximum_permutation_p": 0.05,
                        "bootstrap_95ci_excludes_zero": True,
                        "minimum_balanced_accuracy": 0.60,
                        "minimum_feature_coverage_per_outcome": 0.80,
                    },
                    "feature_effect": item,
                    "classification_in_sample": audit,
                })
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item["classification_in_sample"]["balanced_accuracy"],
            item["classification_in_sample"]["accuracy"] or 0.0,
            abs(item["feature_effect"]["cliffs_delta_tp_higher"] or 0.0),
        ),
        reverse=True,
    )
    best = candidates[0]
    if best["classification_in_sample"]["balanced_accuracy"] < 0.60:
        return None
    return best


def build_report(rows: list[dict[str, Any]], events: dict) -> dict[str, Any]:
    enriched = enrich_rows(rows, events)
    report: dict[str, Any] = {
        "config": {
            "strategies": list(TARGET_STRATEGIES),
            "scope": "resolved shadow demo_positions only",
            "outcomes": "recorded demo_positions status tp/sl",
            "r_definition": "directional exit-vs-entry divided by absolute entry-to-SL risk",
            "minimum_group_n": MIN_GROUP_N,
            "read_only": True,
            "in_sample_rule_warning": (
                "Any candidate is selected and evaluated on the same history; "
                "it is not forward evidence."
            ),
        },
        "coverage": {
            "rows_loaded": len(enriched),
            "log_exists": bool(events),
            "log_event_keys": len(events),
            "log_matches": sum(row.get("log_match_ts") is not None for row in enriched),
            "log_match_rate_pct": round(
                100.0 * sum(row.get("log_match_ts") is not None for row in enriched)
                / len(enriched), 2
            ) if enriched else 0.0,
            "last_signal_ts": max(
                (row["ts_open"] for row in enriched), default=None
            ),
            "last_signal_utc": fmt_ts(
                max((row["ts_open"] for row in enriched), default=None)
            ),
        },
        "feature_provenance": FEATURE_META,
        "strategies": {},
        "rows": enriched,
    }
    numeric_fields = list(FEATURE_META)
    for strategy in TARGET_STRATEGIES:
        strategy_rows = [row for row in enriched if row["alert_type"] == strategy]
        directions = sorted({row["direction"] for row in strategy_rows})
        cohorts = {"overall": strategy_rows}
        cohorts.update({
            direction: [row for row in strategy_rows if row["direction"] == direction]
            for direction in directions
        })
        strategy_report = {}
        for cohort_name, cohort_rows in cohorts.items():
            tp_rows = [row for row in cohort_rows if row["outcome"] == "tp"]
            sl_rows = [row for row in cohort_rows if row["outcome"] == "sl"]
            stable_offset = sum(
                (index + 1) * ord(char)
                for index, char in enumerate(f"{strategy}:{cohort_name}")
            )
            rng = random.Random(RANDOM_SEED + stable_offset)
            comparisons = [
                compare_feature(tp_rows, sl_rows, field, rng)
                for field in numeric_fields
            ]
            strategy_report[cohort_name] = {
                "metrics": metrics(cohort_rows),
                "tp_first": len(tp_rows),
                "sl_first": len(sl_rows),
                "comparison_allowed": (
                    len(tp_rows) >= MIN_GROUP_N and len(sl_rows) >= MIN_GROUP_N
                ),
                "feature_comparisons": comparisons,
                "candidate": (
                    find_candidate(cohort_rows, comparisons)
                    if len(tp_rows) >= MIN_GROUP_N and len(sl_rows) >= MIN_GROUP_N
                    else None
                ),
            }
        report["strategies"][strategy] = strategy_report
    return report


def _md_value(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_outputs(
    report: dict[str, Any], output_dir: Path, rows: list[dict[str, Any]]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    fields = [
        "id", "ts_open", "ts_open_utc", "symbol", "direction", "alert_type",
        "status", "outcome", "entry_price", "sl_price", "tp_price", "exit_price",
        "result_r", "risk_pct", "reward_pct", "reward_risk", "signal_price",
        "entry_vs_signal_pct", "log_match_ts", "log_match_delta_sec",
        "ema_gap_pct_log", "overheated_pct24_log", "overheated_rsi_log",
        "confirmation_volume_ratio_log", "confirmation_number_log",
        "confirmation_age_min_log", "confirmation_tp_mult_log",
    ]
    with (output_dir / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# TP vs SL — strong-signal experimental analysis",
        "",
        "**Read-only report. No production logic, filters, score, SL/TP, or SQLite rows were changed.**",
        "",
        f"- Scope: resolved shadow `demo_positions` rows only; loaded **{report['coverage']['rows_loaded']}**.",
        f"- Runtime log matches: **{report['coverage']['log_matches']}** "
        f"({report['coverage']['log_match_rate_pct']}%).",
        f"- Minimum comparison cohort: **{MIN_GROUP_N} TP-first and {MIN_GROUP_N} SL-first**.",
        "- `WR` is TP / (TP + SL); `avg R` uses recorded exit price and original entry-to-SL risk.",
        "- Any rule below is in-sample: the threshold was selected and scored on the same rows.",
        "",
        "## Feature provenance",
        "",
        "| Field | Provenance | Coverage | Meaning |",
        "|---|---|---:|---|",
    ]
    for field, meta in FEATURE_META.items():
        count = sum(
            row.get(field) not in (None, "")
            for row in rows
        )
        coverage = 100.0 * count / len(rows) if rows else 0.0
        lines.append(
            f"| {meta['label']} (`{field}`) | {meta['provenance']} | "
            f"{coverage:.1f}% | {meta['description']} |"
        )
    lines += ["", "## Current strategy performance", ""]
    lines += [
        "| Strategy | Cohort | n | TP | SL | WR resolved | avg R | Status |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for strategy, cohorts in report["strategies"].items():
        for cohort_name, item in cohorts.items():
            metric = item["metrics"]
            status = (
                "ready"
                if item["comparison_allowed"]
                else f"INSUFFICIENT (<{MIN_GROUP_N} in TP or SL)"
            )
            lines.append(
                f"| {strategy} | {cohort_name} | {metric['n']} | {metric['tp']} | "
                f"{metric['sl']} | {_md_value(metric['resolved_wr_pct'], 2)}% | "
                f"{_md_value(metric['avg_r'], 4)} | {status} |"
            )
    lines += ["", "## TP-first vs SL-first comparisons", ""]
    for strategy, cohorts in report["strategies"].items():
        lines += [f"### {strategy}", ""]
        for cohort_name, item in cohorts.items():
            lines += [f"#### {cohort_name}", ""]
            if not item["comparison_allowed"]:
                lines.append(
                    f"**INSUFFICIENT:** TP={item['tp_first']}, SL={item['sl_first']}; "
                    "no feature conclusion or candidate is allowed."
                )
                lines.append("")
                continue
            lines += [
                "| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |",
                "|---|---:|---:|---:|---:|---|---:|",
            ]
            for comparison in item["feature_comparisons"]:
                tp = comparison["tp_first"]
                sl = comparison["sl_first"]
                ci = comparison["bootstrap_95ci"]
                lines.append(
                    f"| {comparison['label']} [{comparison['provenance']}] | "
                    f"{_md_value(tp['median'])} ({tp['n']}) | "
                    f"{_md_value(sl['median'])} ({sl['n']}) | "
                    f"{_md_value(comparison['median_diff_tp_minus_sl'])} | "
                    f"{_md_value(comparison['cliffs_delta_tp_higher'])} | "
                    f"[{_md_value(ci[0])}, {_md_value(ci[1])}] | "
                    f"{_md_value(comparison['permutation_p_two_sided'], 4)} |"
                )
            candidate = item["candidate"]
            lines += ["", "#### Experimental candidate", ""]
            if candidate is None:
                lines.append(
                    "**NO CANDIDATE:** no feature met the predeclared effect, "
                    "permutation, confidence-interval, coverage, and balanced-accuracy criteria."
                )
            else:
                audit = candidate["classification_in_sample"]
                lines += [
                    f"- **{candidate['rule']}**",
                    f"- Provenance: **{candidate['provenance']}**",
                    f"- In-sample accuracy: **{audit['accuracy']}**, balanced accuracy: "
                    f"**{audit['balanced_accuracy']}**",
                    f"- Precision TP: **{audit['precision_tp']}**; precision SL: "
                    f"**{audit['precision_sl']}**",
                    "- This is experimental and requires forward-shadow validation; it is not a production rule.",
                ]
            lines.append("")
    lines += [
        "## Telegram marker decision",
        "",
        "No Telegram marker is enabled by this analysis. A marker may only be added "
        "behind an explicit default-off control after a candidate is deliberately accepted "
        "for forward-shadow testing; it must remain informational and cannot affect signal generation.",
        "",
        "## Guardrails",
        "",
        "- Runtime-log values are rounded at emission time and are not exact raw market snapshots.",
        "- Missing fields are left missing; no current ticker is substituted for historical signal-time data.",
        "- Statistical summaries are exploratory and subject to multiple-comparison bias.",
        "- No candidate is forward-validated by this report.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--log", type=Path, default=Path("bot_debug.log"))
    parser.add_argument(
        "--out", type=Path, default=Path("outcome_tp_vs_sl_experimental")
    )
    args = parser.parse_args()
    rows = load_resolved(args.db)
    events = parse_runtime_log(args.log)
    report = build_report(rows, events)
    enriched = report.pop("rows")
    write_outputs(report, args.out, enriched)
    print(
        f"Wrote {args.out / 'report.md'}, {args.out / 'report.json'}, "
        f"and {args.out / 'rows.csv'} ({len(enriched)} rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())