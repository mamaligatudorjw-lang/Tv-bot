#!/usr/bin/env python3
"""Read-only forward audit for SHORT ema-cross strategies.

The audit intentionally does not create runtime trackers or SQLite tables.  It
reads already-recorded shadow positions after one persisted UTC cutoff and
produces standalone JSON, Markdown, and CSV reports.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


FORWARD_STRATEGIES = ("ema_cross_confirmed", "ema_cross")
IN_SAMPLE_AVG_R = {
    "ema_cross_confirmed": -0.410295,
    "ema_cross": -0.178233,
}
MIN_FORWARD_N = 20
FORWARD_REPORT_DIR = Path("outcome_forward_short")
CUTOFF_FILENAME = "cutoff.json"


def fmt_ts(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def parse_cutoff(value: str | int | float) -> int:
    """Parse a Unix timestamp or an explicit timezone-aware ISO-8601 value."""
    if isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value).strip()
        try:
            timestamp = float(text)
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("cutoff ISO-8601 value must include a timezone")
            timestamp = parsed.timestamp()
    if not math.isfinite(timestamp):
        raise ValueError("cutoff must be finite")
    if timestamp < 0:
        raise ValueError("cutoff must not be negative")
    return int(timestamp)


def load_or_create_cutoff(
    output_dir: Path,
    *,
    explicit_cutoff: str | int | float | None = None,
    now_ts: int | float | None = None,
) -> int:
    """Reuse a persisted cutoff unless the caller explicitly replaces it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cutoff_path = output_dir / CUTOFF_FILENAME
    if explicit_cutoff is not None:
        cutoff = parse_cutoff(explicit_cutoff)
    elif cutoff_path.exists():
        payload = json.loads(cutoff_path.read_text(encoding="utf-8"))
        if "cutoff_ts" not in payload:
            raise ValueError(f"{cutoff_path} has no cutoff_ts")
        cutoff = parse_cutoff(payload["cutoff_ts"])
    else:
        cutoff = parse_cutoff(time.time() if now_ts is None else now_ts)
    cutoff_path.write_text(
        json.dumps(
            {
                "cutoff_ts": cutoff,
                "cutoff_utc": fmt_ts(cutoff),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return cutoff


def load_forward_rows(db_path: Path, cutoff_ts: int) -> list[dict[str, Any]]:
    """Read only resolved post-cutoff SHORT shadow positions."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type,
                       is_shadow
                  FROM demo_positions
                 WHERE is_shadow=1
                   AND direction='SHORT'
                   AND alert_type IN (?, ?)
                   AND ts_open > ?
                   AND status IN ('tp', 'sl')
                   AND entry_price > 0
                   AND sl_price > 0
                   AND tp_price > 0
                   AND exit_price IS NOT NULL
                   AND exit_price > 0
                 ORDER BY ts_open, id
                """,
                (*FORWARD_STRATEGIES, int(cutoff_ts)),
            )
        ]
    finally:
        connection.close()
    return rows


def result_r(row: dict[str, Any]) -> float | None:
    """Compute the same direction-adjusted exit-vs-entry R as the outcome report."""
    try:
        entry = float(row["entry_price"])
        stop = float(row["sl_price"])
        exit_price = float(row["exit_price"])
    except (KeyError, TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if not all(math.isfinite(value) for value in (entry, stop, exit_price)):
        return None
    if entry <= 0 or risk <= 0:
        return None
    value = (entry - exit_price) / risk
    return value if math.isfinite(value) else None


def metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    tp = sum(row.get("status") == "tp" for row in rows)
    sl = sum(row.get("status") == "sl" for row in rows)
    resolved = tp + sl
    rs = [
        value
        for row in rows
        for value in [row.get("result_r", result_r(row))]
        if value is not None and math.isfinite(float(value))
    ]
    avg_r_raw = mean(float(value) for value in rs) if rs else None
    return {
        "total_n": len(rows),
        "resolved_n": resolved,
        "tp": tp,
        "sl": sl,
        "unresolved_n": len(rows) - resolved,
        "resolved_wr_pct": round(tp / resolved * 100.0, 2) if resolved else None,
        "avg_r": round(avg_r_raw, 6) if avg_r_raw is not None else None,
        "avg_r_unrounded": avg_r_raw,
    }


def classify_verdict(metric: dict[str, Any]) -> tuple[str, str]:
    """Apply the frozen sign-only rule to the unrounded forward average R."""
    if int(metric["resolved_n"]) < MIN_FORWARD_N:
        return (
            "INSUFFICIENT",
            f"Requires at least {MIN_FORWARD_N} resolved forward SHORT trades; "
            f"observed n={metric['resolved_n']}.",
        )
    avg_r = metric.get("avg_r_unrounded")
    if avg_r is None:
        return "INSUFFICIENT", "No finite forward avg R is available."
    if avg_r < 0:
        return (
            "CONFIRMED",
            "Forward avg R is negative, matching the negative in-sample baseline.",
        )
    if avg_r > 0:
        return (
            "REFUTED",
            "Forward avg R is positive, contradicting the negative in-sample baseline.",
        )
    return (
        "AMBIGUOUS",
        "Forward avg R is exactly zero; the sign does not support either verdict.",
    )


def build_report(
    rows: Iterable[dict[str, Any]],
    cutoff_ts: int,
    *,
    generated_ts: int | float | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in rows]
    for row in rows:
        row["result_r"] = result_r(row)

    strategies: dict[str, dict[str, Any]] = {}
    for strategy in FORWARD_STRATEGIES:
        strategy_rows = [row for row in rows if row["alert_type"] == strategy]
        metric = metrics(strategy_rows)
        verdict, reason = classify_verdict(metric)
        strategies[strategy] = {
            "in_sample_avg_r": IN_SAMPLE_AVG_R[strategy],
            "in_sample_avg_r_display": round(IN_SAMPLE_AVG_R[strategy], 2),
            "forward": metric,
            "delta_forward_minus_in_sample": (
                round(metric["avg_r_unrounded"] - IN_SAMPLE_AVG_R[strategy], 6)
                if metric["avg_r_unrounded"] is not None
                else None
            ),
            "verdict": verdict,
            "verdict_reason": reason,
        }

    return {
        "audit": {
            "name": "forward_short_direction_audit",
            "direction": "SHORT",
            "strategies": list(FORWARD_STRATEGIES),
            "minimum_forward_resolved_n": MIN_FORWARD_N,
            "verdict_rule": (
                "n<20 => INSUFFICIENT; n>=20 and unrounded avg R < 0 "
                "=> CONFIRMED; > 0 => REFUTED; == 0 => AMBIGUOUS"
            ),
            "in_sample_baselines": IN_SAMPLE_AVG_R,
        },
        "cutoff_ts": int(cutoff_ts),
        "cutoff_utc": fmt_ts(cutoff_ts),
        "generated_ts": int(generated_ts) if generated_ts is not None else None,
        "generated_utc": fmt_ts(generated_ts),
        "scope": (
            "Resolved is_shadow=1 SHORT demo_positions with alert_type in "
            "ema_cross_confirmed/ema_cross and ts_open > cutoff."
        ),
        "production_changes": False,
        "strategies": strategies,
        "rows": rows,
    }


def write_report(
    db_path: Path,
    output_dir: Path = FORWARD_REPORT_DIR,
    *,
    cutoff: str | int | float | None = None,
    generated_ts: int | float | None = None,
    now_ts: int | float | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cutoff_ts = load_or_create_cutoff(
        output_dir,
        explicit_cutoff=cutoff,
        now_ts=now_ts,
    )
    if generated_ts is None:
        generated_ts = int(time.time())
    rows = load_forward_rows(db_path, cutoff_ts)
    report = build_report(rows, cutoff_ts, generated_ts=generated_ts)

    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    fields = [
        "id",
        "ts_open",
        "symbol",
        "direction",
        "alert_type",
        "entry_price",
        "sl_price",
        "tp_price",
        "status",
        "ts_close",
        "exit_price",
        "result_r",
    ]
    with (output_dir / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["rows"])

    lines = [
        "# Forward-аудит SHORT — ema_cross_confirmed / ema_cross",
        "",
        "**Read-only отчёт. Allowlist, Telegram-видимость, scoring, TP/SL, cooldown "
        "и execution не изменяются.**",
        "",
        f"- Cutoff: **{report['cutoff_utc']}** (`{report['cutoff_ts']}`)",
        f"- Generated: **{report['generated_utc']}**",
        "- Scope: `is_shadow=1`, `direction=SHORT`, resolved `tp/sl`, "
        "`ts_open > cutoff`.",
        f"- Verdict threshold: **{MIN_FORWARD_N} resolved trades per strategy**.",
        "- Rule: negative unrounded forward avg R = **CONFIRMED**; positive = "
        "**REFUTED**; exact zero = **AMBIGUOUS**.",
        "",
        "## Verdict and comparison",
        "",
        "| Strategy | Forward n | TP | SL | WR | Forward avg R | In-sample avg R | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for strategy in FORWARD_STRATEGIES:
        item = report["strategies"][strategy]
        metric = item["forward"]
        lines.append(
            f"| {strategy} | {metric['resolved_n']} | {metric['tp']} | "
            f"{metric['sl']} | {metric['resolved_wr_pct'] if metric['resolved_wr_pct'] is not None else '—'}% | "
            f"{metric['avg_r'] if metric['avg_r'] is not None else '—'} | "
            f"{item['in_sample_avg_r']:.6f} ({item['in_sample_avg_r_display']:.2f}) | "
            f"**{item['verdict']}** |"
        )
    lines += ["", "## Per-strategy reasons", ""]
    for strategy in FORWARD_STRATEGIES:
        item = report["strategies"][strategy]
        metric = item["forward"]
        lines.append(
            f"- **{strategy}: {item['verdict']}** — {item['verdict_reason']} "
            f"Total resolved rows: {metric['resolved_n']}."
        )
    lines += [
        "",
        "## Guardrails",
        "",
        "- The cutoff is persisted in `cutoff.json` and is reused on later runs "
        "unless `--cutoff` explicitly replaces it.",
        "- Unresolved/open/TTL rows are excluded from the forward sample and never "
        "counted in WR or avg R.",
        "- In-sample baselines are displayed for comparison only; they are not a "
        "second verdict threshold.",
        "- No verdict can be `CONFIRMED`, `REFUTED`, or `AMBIGUOUS` before n≥20.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--out", type=Path, default=FORWARD_REPORT_DIR)
    parser.add_argument(
        "--cutoff",
        help="Explicit Unix timestamp or timezone-aware ISO-8601 UTC cutoff.",
    )
    args = parser.parse_args()
    report = write_report(args.db, args.out, cutoff=args.cutoff)
    print(
        f"Wrote {args.out / 'report.md'} "
        f"(cutoff={report['cutoff_utc']}, "
        f"ema_cross_confirmed={report['strategies']['ema_cross_confirmed']['verdict']}, "
        f"ema_cross={report['strategies']['ema_cross']['verdict']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())