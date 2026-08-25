#!/usr/bin/env python3
"""Read-only high_rejection_short cohort and condition analysis."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from shadow_outcome_report import fetch_candles

BAR_SEC = 15 * 60
LOG_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+).*"
    r"high_rejection SHORT shadow (?P<symbol>\S+): "
    r"range=(?P<range>[-\d.]+)% dist_high=(?P<dist>[-\d.]+)% "
    r"vol=(?P<vol>[-\d.]+)x"
)


def parse_log_values(path: Path) -> dict[str, list[dict]]:
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return by_symbol
    for line in path.open(encoding="utf-8", errors="replace"):
        match = LOG_RE.search(line)
        if not match:
            continue
        stamp = datetime.strptime(match["stamp"], "%Y-%m-%d %H:%M:%S,%f")
        ts = stamp.replace(tzinfo=timezone.utc).timestamp()
        by_symbol[match["symbol"]].append({
            "ts": ts,
            "range_pct_log": float(match["range"]),
            "dist_from_high_pct_log": float(match["dist"]),
            "volume_ratio_log": float(match["vol"]),
        })
    return by_symbol


def simple_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(delta, 0.0) for delta in deltas[-period:]]
    losses = [max(-delta, 0.0) for delta in deltas[-period:]]
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + sum(gains) / period / avg_loss)


def candle_features(candles: list[dict], ts_open: int) -> dict:
    completed = [
        c for c in candles
        if int(c["t"]) + BAR_SEC <= ts_open
    ]
    if not completed:
        return {}
    last = completed[-1]
    opened = float(last["o"])
    close = float(last["c"])
    high = float(last["h"])
    low = float(last["l"])
    body_pct = (opened - close) / opened * 100.0 if opened else None
    range_pct = (high - low) / low * 100.0 if low else None
    window = completed[-96:]
    high24 = max(float(c["h"]) for c in window)
    low24 = min(float(c["l"]) for c in window)
    return {
        "candle_ts": int(last["t"]),
        "bearish_body_pct": body_pct,
        "candle_range_pct": range_pct,
        "body_to_range_pct": (
            abs(opened - close) / (high - low) * 100.0
            if high > low else None
        ),
        "rsi_15m_reconstructed": simple_rsi(
            [float(c["c"]) for c in completed]
        ),
        "range_pct_reconstructed": (
            (high24 - low24) / low24 * 100.0 if low24 > 0 else None
        ),
        "dist_from_high_pct_reconstructed": (
            (high24 - close) / high24 * 100.0 if high24 > 0 else None
        ),
    }


def cohort(rows: list[dict]) -> dict:
    tp = sum(row["outcome"] == "tp_first" for row in rows)
    sl = sum(row["outcome"] == "sl_first" for row in rows)
    resolved = tp + sl
    return {
        "n": len(rows),
        "tp_first": tp,
        "sl_first": sl,
        "unresolved": sum(row["outcome"] == "unresolved" for row in rows),
        "ambiguous": sum(row["outcome"] == "ambiguous" for row in rows),
        "resolved": resolved,
        "wr_resolved_pct": round(100 * tp / resolved, 2) if resolved else None,
        "meets_n20": len(rows) >= 20,
    }


def describe(rows: list[dict], field: str) -> dict:
    values = [
        float(row[field]) for row in rows
        if row.get(field) not in (None, "")
    ]
    if not values:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None}
    return {
        "n": len(values),
        "median": round(median(values), 6),
        "mean": round(sum(values) / len(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument(
        "--outcome-csv", type=Path,
        default=Path("outcome_high_rejection_current/shadow_outcomes.csv"),
    )
    parser.add_argument("--log", type=Path, default=Path("bot_debug.log"))
    parser.add_argument("--out", type=Path, default=Path("outcome_high_rejection_analysis"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    with args.outcome_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["ts_open"] = int(row["ts_open"])
        row["is_shadow"] = int(row["is_shadow"])
    logs = parse_log_values(args.log)

    symbols = sorted({row["symbol"] for row in rows})
    now = int(datetime.now(timezone.utc).timestamp())
    def fetch_one(symbol: str):
        signal_rows = [row for row in rows if row["symbol"] == symbol]
        start = min(row["ts_open"] for row in signal_rows) - 2 * 86400
        end = min(now, max(row["ts_open"] for row in signal_rows) + 3600)
        try:
            return symbol, fetch_candles(
                __import__("requests").Session(), symbol, start, end
            ), None
        except Exception as exc:
            return symbol, [], str(exc)

    candles_by_symbol: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(fetch_one, symbol) for symbol in symbols]
        for future in as_completed(futures):
            symbol, candles, error = future.result()
            candles_by_symbol[symbol] = candles
            if error:
                failures[symbol] = error

    fields = [
        "id", "ts_open", "symbol", "direction", "entry_price", "outcome",
        "range_pct_log", "dist_from_high_pct_log", "volume_ratio_log",
        "rsi_15m_reconstructed", "bearish_body_pct", "candle_range_pct",
        "body_to_range_pct", "range_pct_reconstructed",
        "dist_from_high_pct_reconstructed", "candle_ts",
    ]
    for row in rows:
        log_candidates = logs.get(row["symbol"], [])
        nearest = min(
            log_candidates,
            key=lambda item: abs(item["ts"] - row["ts_open"]),
            default={},
        )
        row.update(nearest)
        row.update(candle_features(
            candles_by_symbol.get(row["symbol"], []), row["ts_open"]
        ))

    eligible = [
        row for row in rows
        if row["outcome"] in ("tp_first", "sl_first", "unresolved", "ambiguous")
    ]
    ordered = sorted(eligible, key=lambda row: (row["ts_open"], int(row["id"])))
    weekly: dict[str, list[dict]] = defaultdict(list)
    for row in ordered:
        dt = datetime.fromtimestamp(row["ts_open"], timezone.utc)
        iso = dt.isocalendar()
        weekly[f"{iso.year}-W{iso.week:02d}"].append(row)
    quartiles = {
        f"Q{index + 1}": ordered[
            len(ordered) * index // 4:len(ordered) * (index + 1) // 4
        ]
        for index in range(4)
    }
    compare_fields = [
        "range_pct_log", "dist_from_high_pct_log", "volume_ratio_log",
        "rsi_15m_reconstructed", "bearish_body_pct",
    ]
    winners = [row for row in eligible if row["outcome"] == "tp_first"]
    losers = [row for row in eligible if row["outcome"] == "sl_first"]
    comparison = {
        field: {
            "tp_first": describe(winners, field),
            "sl_first": describe(losers, field),
        }
        for field in compare_fields
    }
    coverage = {
        "signals_loaded": len(rows),
        "eligible": len(eligible),
        "outcomes": cohort(eligible),
        "tp_first_n": len(winners),
        "sl_first_n": len(losers),
        "symbols_loaded": len(candles_by_symbol),
        "candle_fetch_failures": failures,
        "runtime_log_matches": sum(
            row.get("range_pct_log") is not None for row in rows
        ),
        "runtime_log_values_are_rounded": True,
        "rsi_is_active_gate": False,
        "exact_condition_values_persisted_in_demo_positions": False,
    }
    result = {
        "coverage": coverage,
        "weekly": {key: cohort(value) for key, value in sorted(weekly.items())},
        "quartiles": {key: cohort(value) for key, value in quartiles.items()},
        "tp_vs_sl": {
            "comparison_allowed_at_n20": len(winners) >= 20 and len(losers) >= 20,
            "fields": comparison,
        },
        "field_provenance": {
            "range_pct_log": "runtime log, rounded to 0.1 percentage point",
            "dist_from_high_pct_log": "runtime log, rounded to 0.1 percentage point",
            "volume_ratio_log": "runtime log, rounded to 0.1x",
            "rsi_15m_reconstructed": "reconstructed diagnostic; not an active gate",
            "bearish_body_pct": "reconstructed from last completed 15m candle",
            "candle_range_pct": "reconstructed from last completed 15m candle",
            "reconstructed_range_and_dist": "15m candle proxy, not exact ticker snapshot",
        },
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "analysis.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (args.out / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    md = [
        "# high_rejection_short condition analysis",
        "",
        "Read-only analysis. No production code, thresholds, or database rows were changed.",
        "",
        "## Reconciliation",
        "",
        f"- Loaded **{len(rows)}** rows; eligible fixed-window rows: **{len(eligible)}**.",
        f"- TP-first: **{len(winners)}**; SL-first: **{len(losers)}**; unresolved/ambiguous: **{len(eligible)-len(winners)-len(losers)}**.",
        f"- Runtime log matches: **{coverage['runtime_log_matches']}/{len(rows)}**.",
        "- `range_pct`, `% from high`, and volume are available only as rounded runtime log values; they are not persisted in `demo_positions`.",
        "- The current implementation has no RSI gate for `high_rejection_short`; RSI below is diagnostic reconstruction only.",
        "- Exact TP-vs-SL statistical comparison is **not permitted yet** because TP-first has fewer than 20 rows.",
        "",
        "## Weekly resolved WR",
        "",
        "| ISO week | n | TP | SL | unresolved | ambiguous | resolved WR | n≥20 |",
        "|---|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for key, item in sorted(weekly.items()):
        c = cohort(item)
        md.append(
            f"| {key} | {c['n']} | {c['tp_first']} | {c['sl_first']} | "
            f"{c['unresolved']} | {c['ambiguous']} | "
            f"{c['wr_resolved_pct'] if c['wr_resolved_pct'] is not None else '—'}% | "
            f"{'yes' if c['meets_n20'] else 'no'} |"
        )
    md += [
        "",
        "## Time quartiles",
        "",
        "| Quartile | n | TP | SL | unresolved | ambiguous | resolved WR | n≥20 |",
        "|---|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for key, item in quartiles.items():
        c = cohort(item)
        md.append(
            f"| {key} | {c['n']} | {c['tp_first']} | {c['sl_first']} | "
            f"{c['unresolved']} | {c['ambiguous']} | "
            f"{c['wr_resolved_pct'] if c['wr_resolved_pct'] is not None else '—'}% | "
            f"{'yes' if c['meets_n20'] else 'no'} |"
        )
    md += [
        "",
        "## TP-first vs SL-first condition values",
        "",
        "| Field | TP-first n / median (mean) | SL-first n / median (mean) | valid n≥20 comparison |",
        "|---|---:|---:|:---:|",
    ]
    for field, values in comparison.items():
        tp, sl = values["tp_first"], values["sl_first"]
        tp_text = f"{tp['n']} / {tp['median']} ({tp['mean']})"
        sl_text = f"{sl['n']} / {sl['median']} ({sl['mean']})"
        md.append(
            f"| {field} | {tp_text} | {sl_text} | "
            f"{'yes' if len(winners) >= 20 and len(losers) >= 20 else 'no'} |"
        )
    md += [
        "",
        "## Decision",
        "",
        "No production filter or threshold change is justified. Collect more outcomes until both TP-first and SL-first cohorts meet n≥20, then rerun this exact analysis with persisted/runtime provenance kept separate.",
        "",
    ]
    (args.out / "analysis.md").write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {args.out / 'analysis.md'} and {args.out / 'analysis.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())