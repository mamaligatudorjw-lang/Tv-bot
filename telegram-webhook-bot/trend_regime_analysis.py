#!/usr/bin/env python3
"""Describe resolved signal outcomes by the BTC 4h EMA50 regime.

This is intentionally an offline/read-only analysis.  It does not import
app.py, write to the SQLite database, or change signal generation.  BTC regime
labels use only the last completed BTC 4h candle available at each signal
timestamp.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from shadow_outcome_report import fetch_candles

BTC_SYMBOL = "BTCUSDT"
BTC_INTERVAL = "4h"
BTC_INTERVAL_SEC = 4 * 60 * 60
EMA_PERIOD = 50
MIN_GROUP_N = 20
SPECIAL_COHORTS = (
    ("bb_squeeze", "SHORT"),
    ("high_rejection_short", "SHORT"),
)


def fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""


def price_r(direction: str, entry: float, stop: float, price: float) -> float:
    """Return directional outcome in units of original entry-to-SL risk."""
    risk = abs(stop - entry)
    if risk <= 0:
        return float("nan")
    return (
        (price - entry) / risk
        if direction == "LONG"
        else (entry - price) / risk
    )


def load_resolved(db_path: Path) -> list[dict[str, Any]]:
    """Load the complete resolved demo-position cohort without writing."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type,
                       is_shadow
                  FROM demo_positions
                 WHERE direction IN ('LONG', 'SHORT')
                   AND status IN ('tp', 'sl')
                   AND ts_close IS NOT NULL
                   AND exit_price IS NOT NULL
                   AND exit_price > 0
                   AND entry_price > 0
                   AND sl_price > 0
                   AND tp_price > 0
                   AND ABS(sl_price - entry_price) > 0
                   AND ABS(tp_price - entry_price) > 0
                 ORDER BY ts_open, id
                """
            )
        ]
    finally:
        conn.close()
    return rows


def _normalise_candles(candles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return valid, chronologically ordered candles with unique timestamps."""
    by_ts: dict[int, dict[str, Any]] = {}
    for candle in candles:
        try:
            ts = int(candle["t"])
            close = float(candle["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            by_ts[ts] = {"t": ts, "c": close}
    return [by_ts[ts] for ts in sorted(by_ts)]


def ema_values(
    candles: Sequence[dict[str, Any]], period: int = EMA_PERIOD
) -> list[float | None]:
    """Calculate a standard EMA, with None during its warm-up period."""
    if period <= 0:
        raise ValueError("EMA period must be positive")
    closes = [float(candle["c"]) for candle in candles]
    values: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return values
    ema = sum(closes[:period]) / period
    values[period - 1] = ema
    multiplier = 2.0 / (period + 1)
    for index in range(period, len(closes)):
        ema = closes[index] * multiplier + ema * (1.0 - multiplier)
        values[index] = ema
    return values


def classify_regime(close: float, ema: float) -> str:
    """Classify exactly at the boundary as unknown, never as bull or bear."""
    if close > ema:
        return "bull"
    if close < ema:
        return "bear"
    return "unknown"


def regime_at_signal(
    candles: Sequence[dict[str, Any]],
    ts_open: int,
    *,
    period: int = EMA_PERIOD,
) -> dict[str, Any]:
    """Return a lookahead-safe BTC regime for one signal timestamp.

    A candle is completed only when candle_open + 4h <= signal timestamp.
    Consequently a candle that is still forming at signal time cannot affect
    either the selected close or the EMA.
    """
    normalised = _normalise_candles(candles)
    completed_indices = [
        index
        for index, candle in enumerate(normalised)
        if int(candle["t"]) + BTC_INTERVAL_SEC <= int(ts_open)
    ]
    if not completed_indices:
        return {
            "trend_regime": "unknown",
            "regime_reason": "no_completed_candle",
            "btc_candle_ts": None,
            "btc_close": None,
            "btc_ema50": None,
        }

    last_index = completed_indices[-1]
    ema = ema_values(normalised, period)[last_index]
    candle = normalised[last_index]
    if ema is None:
        return {
            "trend_regime": "unknown",
            "regime_reason": "insufficient_ema_history",
            "btc_candle_ts": int(candle["t"]),
            "btc_close": float(candle["c"]),
            "btc_ema50": None,
        }

    close = float(candle["c"])
    return {
        "trend_regime": classify_regime(close, float(ema)),
        "regime_reason": "close_vs_ema50",
        "btc_candle_ts": int(candle["t"]),
        "btc_close": close,
        "btc_ema50": float(ema),
    }


def annotate_rows(
    rows: Iterable[dict[str, Any]], btc_candles: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach BTC regime and recorded economic R to every resolved row."""
    annotated: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        ts_open = int(row["ts_open"])
        regime = regime_at_signal(btc_candles, ts_open)
        result_r = price_r(
            str(row["direction"]),
            float(row["entry_price"]),
            float(row["sl_price"]),
            float(row["exit_price"]),
        )
        row.update(
            {
                "strategy": row.get("alert_type") or "unknown",
                "ts_open_utc": fmt_ts(ts_open),
                "ts_close_utc": fmt_ts(
                    int(row["ts_close"]) if row.get("ts_close") is not None else None
                ),
                "result_r": result_r if math.isfinite(result_r) else None,
                "outcome": "win" if row["status"] == "tp" else "loss",
                "btc_candle_ts_utc": fmt_ts(regime["btc_candle_ts"]),
                **regime,
            }
        )
        annotated.append(row)
    return annotated


def _empty_metrics(n: int = 0) -> dict[str, Any]:
    return {
        "n": n,
        "wins": 0,
        "losses": 0,
        "resolved_wr_pct": None,
        "avg_r": None,
        "sample_status": "insufficient_sample",
    }


def metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate resolved rows; n is the resolved sample size."""
    wins = sum(row["status"] == "tp" for row in rows)
    losses = sum(row["status"] == "sl" for row in rows)
    rs = [
        float(row["result_r"])
        for row in rows
        if row.get("result_r") is not None
        and math.isfinite(float(row["result_r"]))
    ]
    n = wins + losses
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "resolved_wr_pct": round(100.0 * wins / n, 2) if n else None,
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
        "sample_status": "ready" if n >= MIN_GROUP_N else "insufficient_sample",
    }


def build_summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build overall and strategy-level direction/regime groups."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        regime = str(row.get("trend_regime") or "unknown")
        direction = str(row["direction"])
        strategy = str(row.get("strategy") or row.get("alert_type") or "unknown")
        pairs.add((strategy, direction))
        groups[("ALL", direction, regime)].append(row)
        groups[(strategy, direction, regime)].append(row)

    # Keep the requested bull/bear comparison explicit even when a cohort has
    # no observations in one regime.  Unknown is emitted only when it occurs.
    for direction in {direction for _, direction in pairs}:
        for regime in ("bull", "bear"):
            groups.setdefault(("ALL", direction, regime), [])
    for strategy, direction in pairs:
        for regime in ("bull", "bear"):
            groups.setdefault((strategy, direction, regime), [])

    summary: list[dict[str, Any]] = []
    for (strategy, direction, regime), items in sorted(groups.items()):
        item = metrics(items)
        summary.append(
            {
                "scope": "overall" if strategy == "ALL" else "strategy",
                "strategy": strategy,
                "direction": direction,
                "trend_regime": regime,
                **item,
            }
        )
    return summary


def special_summary(summary: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the requested special cohorts in a stable, auditable order."""
    wanted = set(SPECIAL_COHORTS)
    return [
        row
        for row in summary
        if row["scope"] == "strategy"
        and (row["strategy"], row["direction"]) in wanted
    ]


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        for row in rows:
            output = {}
            for field in fields:
                value = row.get(field, "")
                if value is None:
                    value = ""
                output[field] = value
            writer.writerow(output)


def _display_metric(value: Any, *, percent: bool = False) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):.1f}%" if percent else f"{float(value):.3f}"


def _summary_table(rows: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| Strategy | Direction | Regime | n | WR resolved | avg R | Sample |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        status = (
            "ready"
            if row["sample_status"] == "ready"
            else f"INSUFFICIENT (<{MIN_GROUP_N}; n={row['n']})"
        )
        lines.append(
            f"| {row['strategy']} | {row['direction']} | {row['trend_regime']} | "
            f"{row['n']} | {_display_metric(row['resolved_wr_pct'], percent=True)} | "
            f"{_display_metric(row['avg_r'])} | {status} |"
        )
    return lines


def write_report(
    output_dir: Path,
    annotated: Sequence[dict[str, Any]],
    summary: Sequence[dict[str, Any]],
    coverage: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    signal_fields = (
        "id",
        "ts_open",
        "ts_open_utc",
        "symbol",
        "direction",
        "alert_type",
        "strategy",
        "is_shadow",
        "status",
        "ts_close",
        "ts_close_utc",
        "entry_price",
        "sl_price",
        "tp_price",
        "exit_price",
        "result_r",
        "outcome",
        "btc_candle_ts",
        "btc_candle_ts_utc",
        "btc_close",
        "btc_ema50",
        "trend_regime",
        "regime_reason",
    )
    summary_fields = (
        "scope",
        "strategy",
        "direction",
        "trend_regime",
        "n",
        "wins",
        "losses",
        "resolved_wr_pct",
        "avg_r",
        "sample_status",
    )
    _write_csv(output_dir / "signal_regimes.csv", annotated, signal_fields)
    _write_csv(output_dir / "regime_summary.csv", summary, summary_fields)

    report = {
        "config": {
            "btc_symbol": BTC_SYMBOL,
            "btc_interval": BTC_INTERVAL,
            "ema_period": EMA_PERIOD,
            "completed_candle_rule": "candle_open + 4h <= signal ts_open",
            "regime_rule": "bull if BTC close > EMA50; bear if BTC close < EMA50; equal is unknown",
            "minimum_group_n": MIN_GROUP_N,
            "outcome_source": "demo_positions status tp/sl and recorded exit_price",
            "r_definition": "directional (exit - entry) / absolute(entry - original SL)",
            "read_only": True,
            "lookahead_safe": True,
        },
        "coverage": coverage,
        "summary": list(summary),
        "special_cohorts": special_summary(summary),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    overall = [row for row in summary if row["scope"] == "overall"]
    strategy_rows = [row for row in summary if row["scope"] == "strategy"]
    lines = [
        "# Historical signal outcomes by BTC regime",
        "",
        "**Read-only descriptive analysis. Production logic and the SQLite database were not changed.**",
        "",
        "Regime is based on BTC Futures 4h. For each signal, only the last candle "
        "with `candle_open + 4h <= ts_open` is used. EMA50 is calculated from the "
        "chronological candle history available up to that completed candle.",
        "",
        "`bull` means BTC close > EMA50; `bear` means BTC close < EMA50; equality "
        "or insufficient candle history is reported as `unknown` and is not silently assigned.",
        "",
        "Outcomes are resolved `demo_positions` rows with status `tp` or `sl`. "
        "WR is `tp / (tp + sl)`. avg R uses the recorded exit price and the original "
        "entry-to-SL risk. It is not a reconstruction of intrabar order.",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(coverage, indent=2, ensure_ascii=False, allow_nan=False),
        "```",
        "",
        "## Overall by direction and regime",
        "",
        *_summary_table(overall),
        "",
        "## By strategy, direction and regime",
        "",
        *_summary_table(strategy_rows),
        "",
        "## Special cohorts",
        "",
        "The requested cohorts are repeated below so their regime comparison is easy to audit.",
        "",
        *_summary_table(
            [
                row
                for row in strategy_rows
                if (row["strategy"], row["direction"]) in set(SPECIAL_COHORTS)
            ]
        ),
        "",
        "## Interpretation guardrails",
        "",
        f"- Groups with fewer than {MIN_GROUP_N} resolved rows are marked **INSUFFICIENT**; their percentages are descriptive only.",
        "- The report compares cohorts and does not establish causation.",
        "- Results are subject to multiple comparisons, strategy heterogeneity, fees/slippage, and the recorded-position cohort.",
        "- No signal was filtered, replayed into production, or changed because of this report.",
        "- `unknown` rows remain in the audit CSV and coverage counts; they are not dropped silently.",
        "",
    ]
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def fetch_btc_history(
    signals: Sequence[dict[str, Any]],
    *,
    session: requests.Session | None = None,
    warmup_bars: int = EMA_PERIOD * 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one bounded BTC history covering every signal timestamp."""
    if not signals:
        return [], {"status": "no_signals"}
    min_ts = min(int(row["ts_open"]) for row in signals)
    max_ts = max(int(row["ts_open"]) for row in signals)
    start = min_ts - max(EMA_PERIOD, warmup_bars) * BTC_INTERVAL_SEC
    end = max_ts
    own_session = session is None
    client = session or requests.Session()
    try:
        candles = fetch_candles(
            client,
            BTC_SYMBOL,
            start,
            end,
            interval=BTC_INTERVAL,
            interval_sec=BTC_INTERVAL_SEC,
        )
    finally:
        if own_session:
            client.close()
    normalised = _normalise_candles(candles)
    return normalised, {
        "status": "ok",
        "requested_start_ts": start,
        "requested_end_ts": end,
        "requested_start_utc": fmt_ts(start),
        "requested_end_utc": fmt_ts(end),
        "candles_received": len(normalised),
        "candle_first_ts": normalised[0]["t"] if normalised else None,
        "candle_last_ts": normalised[-1]["t"] if normalised else None,
        "candle_first_utc": fmt_ts(normalised[0]["t"]) if normalised else "",
        "candle_last_utc": fmt_ts(normalised[-1]["t"]) if normalised else "",
    }


def build_coverage(
    signals: Sequence[dict[str, Any]],
    annotated: Sequence[dict[str, Any]],
    candle_info: dict[str, Any],
) -> dict[str, Any]:
    reasons = Counter(str(row["regime_reason"]) for row in annotated)
    regimes = Counter(str(row["trend_regime"]) for row in annotated)
    return {
        "resolved_rows_loaded": len(signals),
        "resolved_rows_reported": len(annotated),
        "signals_by_direction": dict(
            Counter(str(row["direction"]) for row in annotated)
        ),
        "signals_by_strategy": dict(
            Counter(str(row["strategy"]) for row in annotated)
        ),
        "trend_regime_counts": dict(regimes),
        "regime_reason_counts": dict(reasons),
        "btc_fetch": candle_info,
        "signal_min_ts": min((int(row["ts_open"]) for row in signals), default=None),
        "signal_max_ts": max((int(row["ts_open"]) for row in signals), default=None),
        "signal_min_utc": fmt_ts(
            min((int(row["ts_open"]) for row in signals), default=None)
        ),
        "signal_max_utc": fmt_ts(
            max((int(row["ts_open"]) for row in signals), default=None)
        ),
        "analysis_run_utc": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=Path(__file__).with_name("alerts.db")
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("trend_regime_analysis"),
    )
    parser.add_argument(
        "--warmup-bars",
        type=int,
        default=EMA_PERIOD * 5,
        help="BTC 4h candles fetched before the first signal for EMA warm-up.",
    )
    args = parser.parse_args()
    if args.warmup_bars < EMA_PERIOD:
        parser.error("--warmup-bars must be at least the EMA period")

    signals = load_resolved(args.db)
    if not signals:
        print("No resolved signals found.")
        return 1

    try:
        btc_candles, candle_info = fetch_btc_history(
            signals, warmup_bars=args.warmup_bars
        )
    except Exception as exc:
        btc_candles = []
        candle_info = {
            "status": "fetch_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    annotated = annotate_rows(signals, btc_candles)
    summary = build_summary(annotated)
    coverage = build_coverage(signals, annotated, candle_info)
    write_report(args.out, annotated, summary, coverage)
    print(
        f"Wrote {args.out / 'signal_regimes.csv'}, "
        f"{args.out / 'regime_summary.csv'}, "
        f"{args.out / 'report.json'}, {args.out / 'report.md'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())