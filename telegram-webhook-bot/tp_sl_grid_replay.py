#!/usr/bin/env python3
"""Read-only TP/SL distance-grid replay for the frozen whitelist cohort.

This analysis intentionally does not import app.py and never writes to the
production SQLite database.  It freezes the 127-signal cohort from the
specified timestamps, reconstructs each signal's baseline barriers from the
persisted row or its parent signal, then replays first-touch outcomes on
Gate.io 1-minute futures candles.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from shadow_outcome_report import fetch_candles
from tp_vs_sl_analysis import parse_runtime_log


FREEZE_TS = int(datetime.fromisoformat("2026-09-01T07:34:48+00:00").timestamp())
OVERHEATED_START_TS = int(
    datetime.fromisoformat("2026-08-29T21:00:38+00:00").timestamp()
)
CONFIRMED_START_TS = int(
    datetime.fromisoformat("2026-08-30T09:20:33+00:00").timestamp()
)
INTERVAL = "1m"
INTERVAL_SEC = 60
GRID = (("baseline", 1.0), ("narrow-1", 0.75), ("narrow-2", 0.50))
EXPECTED_COUNTS = {
    "overheated_24h": 53,
    "overheated_confirmed": 40,
    "ema_cross_confirmed": 34,
}


def utc_iso(ts: float | int | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def rel_close(left: float, right: float, tolerance: float = 1e-6) -> bool:
    return abs(left - right) <= tolerance * max(1.0, abs(left), abs(right))


def valid_barriers(direction: str, entry: float, sl: float, tp: float) -> bool:
    if not all(math.isfinite(float(v)) and float(v) > 0 for v in (entry, sl, tp)):
        return False
    return (
        direction == "LONG" and sl < entry < tp
    ) or (
        direction == "SHORT" and tp < entry < sl
    )


def load_frozen_cohort(db_path: Path, log_path: Path) -> list[dict[str, Any]]:
    """Load exactly 53 + 40 + 34 signals, without expanding the cohort."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    cohort: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT id, ts_open, symbol, direction, entry_price, sl_price, tp_price,
                   status, ts_close, exit_price, alert_type, signal_price
              FROM demo_positions
             WHERE is_shadow=1
               AND alert_type='overheated_24h'
               AND ts_open>=? AND ts_open<?
               AND direction IN ('LONG', 'SHORT')
               AND entry_price>0 AND sl_price>0 AND tp_price>0
             ORDER BY ts_open, id
            """,
            (OVERHEATED_START_TS, FREEZE_TS),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item.update(
                signal_id=f"overheated_24h-db-{item['id']}",
                source="persisted_overheated_24h",
                event_ts=float(item["ts_open"]),
                baseline_source="persisted_confirmed_or_parent",
            )
            cohort.append(item)

        events = parse_runtime_log(log_path)
        for strategy in ("overheated_confirmed", "ema_cross_confirmed"):
            for (event_strategy, symbol, direction), values in events.items():
                if event_strategy != strategy:
                    continue
                for event in values:
                    if not (
                        CONFIRMED_START_TS <= event["ts"] < FREEZE_TS
                        and event["confirmation_number_log"] == 1
                    ):
                        continue
                    event_ts = float(event["ts"])
                    cohort.append(
                        {
                            "id": None,
                            "signal_id": (
                                f"{strategy}-log-{event_ts:.3f}-{symbol}-{direction}"
                            ),
                            "ts_open": int(event_ts),
                            "event_ts": event_ts,
                            "symbol": symbol,
                            "direction": direction,
                            "entry_price": float(event["confirmed_entry_log"]),
                            "signal_price": float(event["confirmed_signal_log"]),
                            "sl_price": None,
                            "tp_price": None,
                            "status": None,
                            "ts_close": None,
                            "exit_price": None,
                            "alert_type": strategy,
                            "source": "runtime_cont_confirmed_number_1",
                            "log_event": event,
                            "baseline_source": "unresolved",
                        }
                    )
    finally:
        conn.close()

    counts = Counter(item["alert_type"] for item in cohort)
    if dict(counts) != EXPECTED_COUNTS:
        raise RuntimeError(
            f"Frozen cohort mismatch: got {dict(counts)}, expected {EXPECTED_COUNTS}"
        )
    return sorted(cohort, key=lambda item: (item["event_ts"], item["signal_id"]))


def _db_rows(
    conn: sqlite3.Connection,
    alert_type: str,
    symbol: str,
    direction: str,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
          FROM demo_positions
         WHERE is_shadow=1
           AND alert_type=? AND symbol=? AND direction=?
           AND ts_open>=? AND ts_open<?
           AND entry_price>0 AND sl_price>0 AND tp_price>0
         ORDER BY ts_open, id
        """,
        (alert_type, symbol, direction, start_ts, end_ts),
    ).fetchall()
    return [dict(row) for row in rows]


def historical_atr_4h(
    symbol: str,
    asof_ts: float,
) -> float | None:
    """Reconstruct the completed-candle 4h ATR-14 used by continuation."""
    interval_sec = 4 * 60 * 60
    start = max(0, int(asof_ts) - 320 * interval_sec)
    end = int(asof_ts) + 1
    try:
        with requests.Session() as session:
            raw = fetch_candles(
                session,
                symbol,
                start,
                end,
                interval="4h",
                interval_sec=interval_sec,
                retries=4,
            )
    except Exception:
        return None
    completed = [
        row for row in raw
        if int(row["t"]) + interval_sec <= asof_ts
    ]
    if len(completed) < 15:
        return None
    highs = [float(row["h"]) for row in completed]
    lows = [float(row["l"]) for row in completed]
    closes = [float(row["c"]) for row in completed]
    true_ranges = [
        max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        for index in range(1, len(closes))
    ]
    if len(true_ranges) < 14:
        return None
    atr = sum(true_ranges[:14]) / 14.0
    for true_range in true_ranges[14:]:
        atr = (atr * 13.0 + true_range) / 14.0
    return atr if math.isfinite(atr) and atr > 0 else None


def confirmed_barriers_from_parent(
    item: dict[str, Any],
    parent: dict[str, Any],
    parent_type: str,
) -> tuple[float, float, float, str]:
    """Rebuild continuation #1 levels from the parent's runtime ATR snapshot."""
    entry = float(item["entry_price"])
    parent_entry = float(parent["entry_price"])
    parent_risk = abs(float(parent["sl_price"]) - parent_entry)
    parent_risk_pct = parent_risk / parent_entry * 100.0

    # overheated_24h can use the generic no-ATR fallback. EMA Cross requires
    # an ATR value before it opens the parent position.
    parent_fallback = (
        parent_type == "overheated_24h"
        and rel_close(parent_risk_pct, 2.5, 1e-5)
    )
    if parent_fallback:
        risk = entry * (0.025 if item["direction"] == "LONG" else 0.15)
        source = "parent_persisted_fallback_reconstructed"
    else:
        parent_atr_multiplier = (
            1.5 if parent_type == "overheated_24h" else 1.0
        )
        parent_atr = parent_risk / parent_atr_multiplier
        if item["direction"] == "LONG":
            risk = 1.5 * parent_atr
        else:
            risk = max(2.0 * parent_atr, entry * 0.025)
        source = "parent_persisted_reconstructed"

    if item["direction"] == "LONG":
        sl = entry - risk
        tp = entry + risk * 2.0
    else:
        sl = entry + risk
        tp = entry - risk * 2.0
    if not valid_barriers(item["direction"], entry, sl, tp):
        raise RuntimeError(f"Invalid parent-derived barriers for {item['signal_id']}")
    return sl, tp, parent_risk_pct, source


def attach_baselines(cohort: list[dict[str, Any]], db_path: Path) -> None:
    """Attach exact persisted barriers or reconstruct confirmed #1 barriers.

    Confirmed #1 uses the current continuation formula: LONG 1.5x ATR risk
    with 2R reward; SHORT max(2x ATR, 2.5% floor) risk with 2R reward.  When
    the confirmed position was duplicate-blocked and therefore has no own
    row, the parent signal's persisted risk recovers the ATR.  This is
    recorded per signal so provenance stays visible.
    """
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    used_confirmed_ids: set[int] = set()
    try:
        for item in cohort:
            if item["alert_type"] == "overheated_24h":
                item["baseline_source"] = "persisted_overheated_24h"
                continue

            # Prefer the actual confirmed row when it exists.  The log rounds
            # the emitted price, so use a tight relative price match and the
            # nearest timestamp.
            candidates = _db_rows(
                conn,
                item["alert_type"],
                item["symbol"],
                item["direction"],
                CONFIRMED_START_TS,
                FREEZE_TS,
            )
            candidates = [
                row
                for row in candidates
                if row["id"] not in used_confirmed_ids
                and rel_close(float(row["entry_price"]), float(item["entry_price"]))
            ]
            if candidates:
                row = min(
                    candidates,
                    key=lambda candidate: abs(
                        float(candidate["ts_open"]) - float(item["event_ts"])
                    ),
                )
                if valid_barriers(
                    item["direction"],
                    float(row["entry_price"]),
                    float(row["sl_price"]),
                    float(row["tp_price"]),
                ):
                    used_confirmed_ids.add(int(row["id"]))
                    item["sl_price"] = float(row["sl_price"])
                    item["tp_price"] = float(row["tp_price"])
                    item["baseline_source"] = "persisted_confirmed"
                    item["matched_db_id"] = int(row["id"])
                    continue

            parent_type = (
                "overheated_24h"
                if item["alert_type"] == "overheated_confirmed"
                else "ema_cross"
            )
            all_parent_candidates = _db_rows(
                conn,
                parent_type,
                item["symbol"],
                item["direction"],
                max(0, int(item["event_ts"]) - 6 * 3600),
                int(item["event_ts"]),
            )
            signal_price = float(item["signal_price"])
            parent_candidates = [
                row
                for row in all_parent_candidates
                if row.get("signal_price") is not None
                and rel_close(float(row["signal_price"]), signal_price, 0.005)
            ]
            entry = float(item["entry_price"])
            estimated_parent_ts = (
                float(item["event_ts"])
                - float(item["log_event"]["confirmation_age_min_log"]) * 60.0
            )
            parent = None
            if parent_candidates:
                parent = min(
                    parent_candidates,
                    key=lambda row: abs(
                        float(row["ts_open"]) - estimated_parent_ts
                    ),
                )
            nearest_parent = (
                min(
                    all_parent_candidates,
                    key=lambda row: abs(
                        float(row["ts_open"]) - estimated_parent_ts
                    ),
                )
                if all_parent_candidates
                else None
            )

            # Keep an independent comparison field even when the parent is
            # only the nearest prior row and cannot be used as the baseline.
            if nearest_parent is not None:
                check_sl, check_tp, _, _ = confirmed_barriers_from_parent(
                    item, nearest_parent, parent_type
                )
                item["parent_check_sl_pct"] = abs(check_sl - entry) / entry * 100.0
                item["parent_check_tp_pct"] = abs(check_tp - entry) / entry * 100.0
                item["parent_check_db_id"] = int(nearest_parent["id"])
                item["parent_check_match"] = (
                    "signal_price_within_0.5pct"
                    if parent is not None
                    else "nearest_prior_parent_only"
                )

            # If the parent row is available, its persisted risk is the best
            # independent record of the runtime ATR snapshot.  Prefer it over
            # recomputing ATR from later API history.
            if parent is not None:
                sl, tp, parent_risk_pct, source = confirmed_barriers_from_parent(
                    item, parent, parent_type
                )
                item["parent_check_sl_pct"] = abs(sl - entry) / entry * 100.0
                item["parent_check_tp_pct"] = abs(tp - entry) / entry * 100.0
                item["parent_check_db_id"] = int(parent["id"])
                item["parent_check_match"] = "signal_price_within_0.5pct"
                item["sl_price"] = sl
                item["tp_price"] = tp
                item["baseline_source"] = source
                continue

            # A parent can itself be absent from demo_positions because the
            # duplicate guard blocked its paper row.  In that case reconstruct
            # ATR from completed historical 4h candles, avoiding a generic
            # global distance.
            parent_atr = historical_atr_4h(item["symbol"], estimated_parent_ts)
            if parent_atr is not None:
                if item["direction"] == "LONG":
                    risk = entry * 0.0 + 1.5 * parent_atr
                    sl = entry - risk
                    tp = entry + risk * 2.0
                else:
                    risk = max(2.0 * parent_atr, entry * 0.025)
                    sl = entry + risk
                    tp = entry - risk * 2.0
                if valid_barriers(item["direction"], entry, sl, tp):
                    item["sl_price"] = sl
                    item["tp_price"] = tp
                    item["baseline_source"] = "historical_4h_atr_reconstructed"
                    continue

            raise RuntimeError(
                "Cannot reconstruct confirmed baseline for "
                f"{item['signal_id']} ({item['symbol']})"
            )
    finally:
        conn.close()


def load_cached_candles(cache_dir: Path, symbol: str) -> list[dict[str, Any]] | None:
    path = cache_dir / f"{symbol}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("interval") != INTERVAL:
            return None
        return payload.get("candles")
    except (OSError, ValueError, TypeError):
        return None


def save_cached_candles(
    cache_dir: Path,
    symbol: str,
    start: int,
    end: int,
    candles: list[dict[str, Any]],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{symbol}.json").write_text(
        json.dumps(
            {
                "symbol": symbol,
                "start": start,
                "end": end,
                "interval": INTERVAL,
                "candles": candles,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def fetch_symbol_history(
    symbol: str,
    start: int,
    end: int,
    cache_dir: Path,
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    cached = load_cached_candles(cache_dir, symbol)
    if cached:
        covered = {int(row["t"]) for row in cached}
        if min(covered) <= start - INTERVAL_SEC and max(covered) >= end:
            return symbol, cached, "cache"
    try:
        with requests.Session() as session:
            candles = fetch_candles(
                session,
                symbol,
                start,
                end,
                interval=INTERVAL,
                interval_sec=INTERVAL_SEC,
                retries=4,
            )
        if not candles:
            return symbol, None, "empty"
        save_cached_candles(cache_dir, symbol, start, end, candles)
        return symbol, candles, "gateio"
    except Exception as exc:
        return symbol, None, f"{type(exc).__name__}: {exc}"


def classify_first_touch(
    item: dict[str, Any],
    candles: list[dict[str, Any]],
    scale: float,
) -> tuple[str, int | None, str | None]:
    entry = float(item["entry_price"])
    sl0 = float(item["sl_price"])
    tp0 = float(item["tp_price"])
    direction = item["direction"]
    sl = entry + (sl0 - entry) * scale
    tp = entry + (tp0 - entry) * scale
    start = float(item["event_ts"])

    for candle in candles:
        candle_start = int(candle["t"])
        if candle_start + INTERVAL_SEC <= start:
            continue
        high = float(candle["h"])
        low = float(candle["l"])
        if direction == "LONG":
            hit_tp = high >= tp
            hit_sl = low <= sl
        else:
            hit_tp = low <= tp
            hit_sl = high >= sl
        if hit_tp and hit_sl:
            return "ambiguous_same_candle", candle_start, "both_barriers"
        if hit_tp:
            return "WIN", candle_start, "tp"
        if hit_sl:
            return "LOSS", candle_start, "sl"
    return "no_outcome_yet", None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--log", type=Path, default=Path("bot_debug.log"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outcome_tp_sl_grid_127"),
    )
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    cohort = load_frozen_cohort(args.db, args.log)
    attach_baselines(cohort, args.db)
    if len(cohort) != 127:
        raise RuntimeError(f"Expected 127 rows, got {len(cohort)}")

    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in cohort:
        by_symbol[item["symbol"]].append(item)
    history_start = min(int(item["event_ts"]) for item in cohort) - INTERVAL_SEC
    history_end = FREEZE_TS
    cache_dir = args.output_dir / "candle_cache"
    histories: dict[str, list[dict[str, Any]]] = {}
    fetch_meta: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                fetch_symbol_history,
                symbol,
                min(int(item["event_ts"]) for item in items) - INTERVAL_SEC,
                history_end,
                cache_dir,
            ): symbol
            for symbol, items in by_symbol.items()
        }
        for future in as_completed(futures):
            symbol, candles, status = future.result()
            fetch_meta[symbol] = status or "unknown"
            if candles:
                histories[symbol] = candles

    rows: list[dict[str, Any]] = []
    for item in cohort:
        candles = histories.get(item["symbol"])
        if candles is not None:
            # The freeze can fall inside a live 1m candle.  Paper tracking
            # only observes a candle after it closes, so do not use that
            # partial candle as historical evidence.
            candles = [
                candle for candle in candles
                if int(candle["t"]) + INTERVAL_SEC <= FREEZE_TS
            ]
        for variant, scale in GRID:
            if candles is None:
                outcome, outcome_ts, reason = "missing_coverage", None, fetch_meta.get(
                    item["symbol"], "not_fetched"
                )
            else:
                outcome, outcome_ts, reason = classify_first_touch(item, candles, scale)
            rows.append(
                {
                    "signal_id": item["signal_id"],
                    "strategy": item["alert_type"],
                    "symbol": item["symbol"],
                    "direction": item["direction"],
                    "event_ts": item["event_ts"],
                    "event_utc": utc_iso(item["event_ts"]),
                    "entry_price": item["entry_price"],
                    "baseline_sl_price": item["sl_price"],
                    "baseline_tp_price": item["tp_price"],
                    "baseline_risk_pct": abs(
                        float(item["sl_price"]) - float(item["entry_price"])
                    )
                    / float(item["entry_price"])
                    * 100.0,
                    "baseline_reward_pct": abs(
                        float(item["tp_price"]) - float(item["entry_price"])
                    )
                    / float(item["entry_price"])
                    * 100.0,
                    "baseline_source": item["baseline_source"],
                    "variant": variant,
                    "scale": scale,
                    "outcome": outcome,
                    "outcome_ts": outcome_ts,
                    "outcome_utc": utc_iso(outcome_ts),
                    "outcome_reason": reason,
                    "candle_source": "Gate.io futures 1m",
                }
            )

    # A missing symbol is not silently turned into no_outcome_yet.
    coverage = {
        "symbols_total": len(by_symbol),
        "symbols_with_history": len(histories),
        "symbols_missing_history": sorted(set(by_symbol) - set(histories)),
        "fetch_status": fetch_meta,
        "history_start_utc": utc_iso(history_start),
        "history_end_utc": utc_iso(history_end),
        "interval": INTERVAL,
    }

    summary: list[dict[str, Any]] = []
    for strategy in EXPECTED_COUNTS:
        for variant, scale in GRID:
            selected = [
                row
                for row in rows
                if row["strategy"] == strategy and row["variant"] == variant
            ]
            counts = Counter(row["outcome"] for row in selected)
            resolved = counts["WIN"] + counts["LOSS"]
            wins = counts["WIN"]
            wr = wins / resolved * 100.0 if resolved else None
            summary.append(
                {
                    "strategy": strategy,
                    "variant": variant,
                    "scale": scale,
                    "total_signals": len(selected),
                    "resolved": resolved,
                    "WIN": wins,
                    "LOSS": counts["LOSS"],
                    "no_outcome_yet": counts["no_outcome_yet"],
                    "ambiguous_same_candle": counts["ambiguous_same_candle"],
                    "missing_coverage": counts["missing_coverage"],
                    "WR_pct": wr,
                }
            )

    baseline_wr = {
        row["strategy"]: row["WR_pct"]
        for row in summary
        if row["variant"] == "baseline"
    }
    for row in summary:
        base = baseline_wr[row["strategy"]]
        row["delta_WR_pp_vs_baseline"] = (
            row["WR_pct"] - base
            if row["WR_pct"] is not None and base is not None
            else None
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "per_signal.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.output_dir / "summary.json").write_text(
        json.dumps(
            {
                "analysis": "frozen_127_tp_sl_distance_grid",
                "read_only": True,
                "freeze_utc": utc_iso(FREEZE_TS),
                "cohort_counts": dict(Counter(item["alert_type"] for item in cohort)),
                "coverage": coverage,
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        "# TP/SL distance grid — frozen 127 signals",
        "",
        f"- Freeze: `{utc_iso(FREEZE_TS)}`",
        "- Source: Gate.io USDT futures, completed 1m candle history",
        "- Baseline: each signal's actual persisted or parent-reconstructed TP/SL distance",
        "- narrow-1: 75% of each signal's baseline distance",
        "- narrow-2: 50% of each signal's baseline distance",
        "- WR denominator: `resolved = WIN + LOSS` only",
        "- Same-candle TP+SL: `ambiguous_same_candle`, excluded from WR",
        "",
        "## Coverage",
        "",
        f"- Symbols: {coverage['symbols_with_history']}/{coverage['symbols_total']} with history",
        f"- Missing symbols: {', '.join(coverage['symbols_missing_history']) or 'none'}",
        "",
        "## Results",
        "",
        "| Strategy | Variant | Total | Resolved | WIN | LOSS | No outcome yet | Ambiguous | WR | ΔWR vs baseline |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        wr = "n/a" if row["WR_pct"] is None else f"{row['WR_pct']:.2f}%"
        delta = (
            "n/a"
            if row["delta_WR_pp_vs_baseline"] is None
            else f"{row['delta_WR_pp_vs_baseline']:+.2f} pp"
        )
        lines.append(
            f"| {row['strategy']} | {row['variant']} | {row['total_signals']} | "
            f"{row['resolved']} | {row['WIN']} | {row['LOSS']} | "
            f"{row['no_outcome_yet']} | {row['ambiguous_same_candle']} | "
            f"{wr} | {delta} |"
        )
    (args.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"coverage": coverage, "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())