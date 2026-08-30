#!/usr/bin/env python3
"""Read-only forward validation of the frozen ema_cross_confirmed SL rule.

This audit deliberately reads source demo positions through a read-only
SQLite connection.  It does not create a tracker, change source rows, or
import the production signal path.  The threshold and cutoff are frozen from
the exploratory report before this forward window and must not be tuned here.
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
from statistics import mean
from typing import Any, Iterable


FORWARD_STRATEGY = "ema_cross_confirmed"
FORWARD_THRESHOLD_PCT = 3.567173
# Last ema_cross_confirmed signal in the exploratory snapshot:
# outcome_tp_vs_sl_multihour/report.json, generated from
# alerts_20260830_171817.db.
EXPLORATORY_CUTOFF_TS = 1788107484
EXPLORATORY_CUTOFF_UTC = "2026-08-30T16:31:24+00:00"
MIN_GROUP_N = 20
BOOTSTRAP_ITERATIONS = 800
PERMUTATION_ITERATIONS = 800
RANDOM_SEED = 171
FORWARD_REPORT_DIR = Path("outcome_forward_ema_confirmed_sl")


def utc(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_forward_rows(
    db_path: Path,
    *,
    cutoff_ts: int = EXPLORATORY_CUTOFF_TS,
) -> list[dict[str, Any]]:
    """Load only new resolved shadow rows after the frozen cutoff."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, ts_open, ts_close, symbol, direction, alert_type,
                       entry_price, sl_price, tp_price, status, exit_price,
                       is_shadow
                  FROM demo_positions
                 WHERE is_shadow=1
                   AND alert_type=?
                   AND ts_open>?
                   AND status IN ('tp', 'sl')
                   AND entry_price>0
                   AND sl_price>0
                   AND tp_price>0
                   AND exit_price IS NOT NULL
                   AND exit_price>0
                 ORDER BY ts_open, id
                """,
                (FORWARD_STRATEGY, int(cutoff_ts)),
            )
        ]
    finally:
        connection.close()
    return rows


def risk_pct(row: dict[str, Any]) -> float | None:
    entry = _finite_float(row.get("entry_price"))
    stop = _finite_float(row.get("sl_price"))
    if entry is None or stop is None or entry <= 0:
        return None
    risk = abs(entry - stop) / entry * 100.0
    return risk if math.isfinite(risk) and risk > 0 else None


def result_r(row: dict[str, Any]) -> float | None:
    entry = _finite_float(row.get("entry_price"))
    stop = _finite_float(row.get("sl_price"))
    exit_price = _finite_float(row.get("exit_price"))
    if entry is None or stop is None or exit_price is None:
        return None
    denominator = abs(entry - stop)
    if entry <= 0 or denominator <= 0:
        return None
    direction = row.get("direction")
    if direction == "LONG":
        value = (exit_price - entry) / denominator
    elif direction == "SHORT":
        value = (entry - exit_price) / denominator
    else:
        return None
    return value if math.isfinite(value) else None


def classify_rule(row: dict[str, Any]) -> str | None:
    value = risk_pct(row)
    if value is None:
        return None
    return "candidate_small_sl" if value <= FORWARD_THRESHOLD_PCT else "control_large_sl"


def _bootstrap_mean_diff_ci(
    tp_values: list[float],
    sl_values: list[float],
    rng: random.Random,
) -> tuple[float | None, float | None]:
    if not tp_values or not sl_values:
        return (None, None)
    differences = []
    for _ in range(BOOTSTRAP_ITERATIONS):
        tp_sample = [rng.choice(tp_values) for _ in tp_values]
        sl_sample = [rng.choice(sl_values) for _ in sl_values]
        differences.append(mean(tp_sample) - mean(sl_sample))
    differences.sort()
    low_index = int(0.025 * len(differences))
    high_index = int(0.975 * len(differences))
    return (round(differences[low_index], 6), round(differences[high_index], 6))


def _cliffs_delta(tp_values: list[float], sl_values: list[float]) -> float:
    wins = sum(
        1
        for tp_value in tp_values
        for sl_value in sl_values
        if tp_value > sl_value
    )
    losses = sum(
        1
        for tp_value in tp_values
        for sl_value in sl_values
        if tp_value < sl_value
    )
    denominator = len(tp_values) * len(sl_values)
    return (wins - losses) / denominator if denominator else float("nan")


def _permutation_p_value(
    tp_values: list[float],
    sl_values: list[float],
    rng: random.Random,
) -> float | None:
    if not tp_values or not sl_values:
        return None
    observed = abs(_cliffs_delta(tp_values, sl_values))
    combined = tp_values + sl_values
    tp_n = len(tp_values)
    extreme = 0
    for _ in range(PERMUTATION_ITERATIONS):
        shuffled = list(combined)
        rng.shuffle(shuffled)
        if abs(_cliffs_delta(shuffled[:tp_n], shuffled[tp_n:])) >= observed - 1e-12:
            extreme += 1
    return round((extreme + 1) / (PERMUTATION_ITERATIONS + 1), 6)


def outcome_effect(
    rows: Iterable[dict[str, Any]],
    *,
    rng: random.Random,
) -> dict[str, Any]:
    """Compare exact derived SL distance in TP and SL outcome groups."""
    rows = list(rows)
    tp_values = [
        value
        for row in rows
        if row.get("status") == "tp"
        for value in [risk_pct(row)]
        if value is not None
    ]
    sl_values = [
        value
        for row in rows
        if row.get("status") == "sl"
        for value in [risk_pct(row)]
        if value is not None
    ]
    mean_diff = (
        mean(tp_values) - mean(sl_values)
        if tp_values and sl_values
        else None
    )
    return {
        "tp_n": len(tp_values),
        "sl_n": len(sl_values),
        "tp_mean_risk_pct": round(mean(tp_values), 6) if tp_values else None,
        "sl_mean_risk_pct": round(mean(sl_values), 6) if sl_values else None,
        "mean_diff_tp_minus_sl": (
            round(mean_diff, 6) if mean_diff is not None else None
        ),
        "bootstrap_mean_diff_95ci": list(
            _bootstrap_mean_diff_ci(tp_values, sl_values, rng)
        ),
        "permutation_p_two_sided": _permutation_p_value(
            tp_values, sl_values, rng
        ),
    }


def cohort_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    tp = sum(row.get("status") == "tp" for row in rows)
    sl = sum(row.get("status") == "sl" for row in rows)
    resolved = tp + sl
    rs = [
        value
        for row in rows
        for value in [result_r(row)]
        if value is not None
    ]
    return {
        "n": len(rows),
        "tp": tp,
        "sl": sl,
        "resolved_n": resolved,
        "tp_rate_pct": round(100.0 * tp / resolved, 6) if resolved else None,
        "avg_r": round(mean(rs), 6) if rs else None,
    }


def classify_verdict(effect: dict[str, Any]) -> tuple[str, str]:
    if effect["tp_n"] < MIN_GROUP_N or effect["sl_n"] < MIN_GROUP_N:
        return (
            "INSUFFICIENT",
            f"Requires at least {MIN_GROUP_N} TP and {MIN_GROUP_N} SL "
            f"forward outcomes; observed TP={effect['tp_n']}, SL={effect['sl_n']}.",
        )
    mean_diff = effect["mean_diff_tp_minus_sl"]
    interval = effect["bootstrap_mean_diff_95ci"]
    p_value = effect["permutation_p_two_sided"]
    if mean_diff is None or interval[0] is None or p_value is None:
        return "INSUFFICIENT", "The frozen risk comparison has no finite statistics."
    if mean_diff < 0 and interval[1] < 0 and p_value <= 0.05:
        return (
            "CONFIRMED",
            "TP signals have lower mean SL distance than SL outcomes; "
            "the bootstrap CI excludes zero in the exploratory direction and "
            "the permutation p-value is ≤ 0.05.",
        )
    return (
        "NO_SUPPORT",
        "The forward TP-vs-SL risk comparison does not reproduce the frozen "
        "exploratory direction with a CI excluding zero and p≤0.05.",
    )


def build_report(
    rows: Iterable[dict[str, Any]],
    *,
    cutoff_ts: int = EXPLORATORY_CUTOFF_TS,
    generated_ts: int | float | None = None,
) -> dict[str, Any]:
    enriched = []
    for source_row in rows:
        row = dict(source_row)
        row["risk_pct"] = risk_pct(row)
        row["result_r"] = result_r(row)
        row["rule_group"] = classify_rule(row)
        enriched.append(row)

    cohorts: dict[str, dict[str, Any]] = {}
    for cohort_name, cohort_rows in (
        ("overall", enriched),
        ("LONG", [row for row in enriched if row.get("direction") == "LONG"]),
    ):
        rng = random.Random(RANDOM_SEED + (1 if cohort_name == "LONG" else 0))
        candidate_rows = [
            row for row in cohort_rows if row["rule_group"] == "candidate_small_sl"
        ]
        control_rows = [
            row for row in cohort_rows if row["rule_group"] == "control_large_sl"
        ]
        effect = outcome_effect(cohort_rows, rng=rng)
        verdict, reason = classify_verdict(effect)
        candidate = cohort_metrics(candidate_rows)
        control = cohort_metrics(control_rows)
        tp_rate_diff = None
        if candidate["tp_rate_pct"] is not None and control["tp_rate_pct"] is not None:
            tp_rate_diff = round(
                candidate["tp_rate_pct"] - control["tp_rate_pct"], 6
            )
        cohorts[cohort_name] = {
            "n_total": len(cohort_rows),
            "candidate_small_sl": candidate,
            "control_large_sl": control,
            "tp_rate_diff_candidate_minus_control_pp": tp_rate_diff,
            "outcome_risk_effect": effect,
            "verdict": verdict,
            "verdict_reason": reason,
        }

    return {
        "audit": {
            "name": "forward_ema_confirmed_sl_audit",
            "strategy": FORWARD_STRATEGY,
            "cohorts": ["overall", "LONG"],
            "threshold_pct": FORWARD_THRESHOLD_PCT,
            "rule": (
                f"risk_pct <= {FORWARD_THRESHOLD_PCT:.6f}% is the frozen "
                "small-SL candidate; larger SL is control"
            ),
            "minimum_n_per_outcome": MIN_GROUP_N,
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "permutation_iterations": PERMUTATION_ITERATIONS,
            "seed": RANDOM_SEED,
            "verdict_rule": (
                "INSUFFICIENT unless TP and SL each have n>=20; CONFIRMED only "
                "when TP mean risk is lower, its 95% bootstrap CI excludes zero, "
                "and permutation p<=0.05; otherwise NO_SUPPORT"
            ),
        },
        "cutoff": {
            "ts": int(cutoff_ts),
            "utc": utc(cutoff_ts),
            "exploratory_cutoff_utc": EXPLORATORY_CUTOFF_UTC,
            "source": (
                "last ema_cross_confirmed ts_open in the exploratory "
                "snapshot; post-cutoff rows only"
            ),
        },
        "generated_ts": int(generated_ts) if generated_ts is not None else None,
        "generated_utc": utc(generated_ts),
        "scope": (
            "Resolved is_shadow=1 ema_cross_confirmed demo_positions with "
            "ts_open > frozen cutoff; overall and LONG only. SHORT is excluded "
            "because the exploratory SHORT cohort was insufficient."
        ),
        "production_changes": False,
        "cohorts": cohorts,
        "rows": enriched,
    }


def write_report(
    db_path: Path,
    output_dir: Path = FORWARD_REPORT_DIR,
    *,
    generated_ts: int | float | None = None,
) -> dict[str, Any]:
    if generated_ts is None:
        import time

        generated_ts = int(time.time())
    rows = load_forward_rows(db_path)
    report = build_report(rows, generated_ts=generated_ts)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    fields = [
        "id",
        "ts_open",
        "ts_close",
        "symbol",
        "direction",
        "alert_type",
        "entry_price",
        "sl_price",
        "tp_price",
        "risk_pct",
        "rule_group",
        "status",
        "exit_price",
        "result_r",
    ]
    with (output_dir / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["rows"])

    lines = [
        "# Forward-shadow — frozen `ema_cross_confirmed` narrow-SL rule",
        "",
        "**Read-only audit. No production scoring, gating, whitelist, TP/SL, "
        "execution, or Telegram behavior is changed.**",
        "",
        f"- Frozen threshold: **SL distance ≤ {FORWARD_THRESHOLD_PCT:.6f}%**.",
        f"- Cutoff: **{report['cutoff']['utc']}** (`{report['cutoff']['ts']}`); "
        "only `ts_open > cutoff` is included.",
        "- Exploratory SHORT cohort is excluded; cohorts are overall and LONG.",
        f"- Sufficiency rule: **at least {MIN_GROUP_N} TP and {MIN_GROUP_N} SL "
        "outcomes** per cohort.",
        "- The threshold and cutoff are inherited, not re-selected on forward data.",
        f"- Generated: **{report['generated_utc']}**",
        "",
        "## Verdict",
        "",
        "| Cohort | Forward n | Candidate n | Control n | TP mean risk | SL mean risk | TP−SL mean | 95% CI | p | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for cohort_name in ("overall", "LONG"):
        item = report["cohorts"][cohort_name]
        effect = item["outcome_risk_effect"]
        ci = effect["bootstrap_mean_diff_95ci"]
        lines.append(
            f"| {cohort_name} | {item['n_total']} | "
            f"{item['candidate_small_sl']['n']} | {item['control_large_sl']['n']} | "
            f"{effect['tp_mean_risk_pct'] if effect['tp_mean_risk_pct'] is not None else '—'} | "
            f"{effect['sl_mean_risk_pct'] if effect['sl_mean_risk_pct'] is not None else '—'} | "
            f"{effect['mean_diff_tp_minus_sl'] if effect['mean_diff_tp_minus_sl'] is not None else '—'} | "
            f"[{ci[0] if ci[0] is not None else '—'}, "
            f"{ci[1] if ci[1] is not None else '—'}] | "
            f"{effect['permutation_p_two_sided'] if effect['permutation_p_two_sided'] is not None else '—'} | "
            f"**{item['verdict']}** |"
        )
    lines += ["", "## Candidate vs control descriptive metrics", ""]
    lines += [
        "| Cohort | Candidate TP/SL | Candidate TP-rate | Control TP/SL | Control TP-rate | Δ TP-rate pp |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cohort_name in ("overall", "LONG"):
        item = report["cohorts"][cohort_name]
        candidate = item["candidate_small_sl"]
        control = item["control_large_sl"]
        lines.append(
            f"| {cohort_name} | {candidate['tp']}/{candidate['sl']} | "
            f"{candidate['tp_rate_pct'] if candidate['tp_rate_pct'] is not None else '—'}% | "
            f"{control['tp']}/{control['sl']} | "
            f"{control['tp_rate_pct'] if control['tp_rate_pct'] is not None else '—'}% | "
            f"{item['tp_rate_diff_candidate_minus_control_pp'] if item['tp_rate_diff_candidate_minus_control_pp'] is not None else '—'} |"
        )
    lines += [
        "",
        "## Verdict details",
        "",
    ]
    for cohort_name in ("overall", "LONG"):
        item = report["cohorts"][cohort_name]
        lines.append(f"- **{cohort_name}: {item['verdict']}** — {item['verdict_reason']}")
    lines += [
        "",
        "## Guardrails",
        "",
        "- The forward window contains only rows after the persisted exploratory cutoff; "
        "exploratory rows are not counted twice.",
        "- Open, invalid, and unresolved rows are excluded from the resolved outcome "
        "statistics.",
        "- `risk_pct` is derived from persisted entry and SL prices; it is not a "
        "reconstructed candle proxy.",
        "- Even a CONFIRMED result is not permission to change production trading. "
        "Applying a rule requires a separate decision and checklist item.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--out", type=Path, default=FORWARD_REPORT_DIR)
    args = parser.parse_args()
    report = write_report(args.db, args.out)
    print(
        f"Wrote {args.out / 'report.md'} "
        f"(overall={report['cohorts']['overall']['verdict']}, "
        f"LONG={report['cohorts']['LONG']['verdict']}, "
        f"n={report['cohorts']['overall']['n_total']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())