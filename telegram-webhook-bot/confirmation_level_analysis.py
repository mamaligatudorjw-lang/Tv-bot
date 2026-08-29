#!/usr/bin/env python3
"""Read-only cohort analysis split by continuation confirmation level.

The continuation ladder is persisted indirectly in demo_positions: TP distance
relative to SL distance is 2.0x, 1.5x, or 1.0x for confirmation levels 1/3,
2/3, and 3/3. This script reconstructs that level, joins a lookahead-safe BTC
4h/EMA50 regime, and writes descriptive CSV/JSON/Markdown reports.

It never imports app.py and never writes to SQLite or production state.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from trend_regime_analysis import fetch_btc_history, regime_at_signal
from trailing_stop_analysis import price_r


TARGET_STRATEGIES = ("overheated_confirmed", "ema_cross_confirmed")
DIRECTIONS = ("LONG", "SHORT")
REGIMES = ("bull", "bear", "unknown")
LEVELS = (
    {
        "confirmation_level": "1/3",
        "rr_multiple": 2.0,
        "breakeven_wr_pct": 100.0 / 3.0,
    },
    {
        "confirmation_level": "2/3",
        "rr_multiple": 1.5,
        "breakeven_wr_pct": 40.0,
    },
    {
        "confirmation_level": "3/3",
        "rr_multiple": 1.0,
        "breakeven_wr_pct": 50.0,
    },
)
MIN_GROUP_N = 20
RR_TOLERANCE = 1e-6


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _as_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Invalid non-finite {field}: {value!r}")
    return result


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def infer_level(
    entry_price: float,
    sl_price: float,
    tp_price: float,
    *,
    tolerance: float = RR_TOLERANCE,
) -> dict[str, Any]:
    risk = abs(sl_price - entry_price)
    reward = abs(tp_price - entry_price)
    if risk <= 0:
        raise ValueError("Cannot infer confirmation level with zero SL risk")
    rr_multiple = reward / risk
    selected = min(
        LEVELS,
        key=lambda level: abs(rr_multiple - level["rr_multiple"]),
    )
    if abs(rr_multiple - selected["rr_multiple"]) > tolerance:
        raise ValueError(
            f"TP/SL ratio {rr_multiple} does not match confirmation ladder"
        )
    return {
        "confirmation_level": selected["confirmation_level"],
        "rr_multiple": selected["rr_multiple"],
        "rr_multiple_observed": rr_multiple,
        "breakeven_wr_pct": selected["breakeven_wr_pct"],
    }


def load_resolved(
    db_path: Path,
    strategies: Sequence[str] = TARGET_STRATEGIES,
) -> list[dict[str, Any]]:
    ids = tuple(strategies)
    placeholders = ",".join("?" for _ in ids)
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, status, ts_close, exit_price, alert_type,
                       is_shadow, shadow_reason
                  FROM demo_positions
                 WHERE alert_type IN ({placeholders})
                   AND direction IN ('LONG', 'SHORT')
                   AND status IN ('tp', 'sl')
                   AND ts_close IS NOT NULL
                   AND entry_price > 0
                   AND sl_price > 0
                   AND tp_price > 0
                   AND exit_price > 0
                 ORDER BY ts_open, id
                """,
                ids,
            )
        ]
    finally:
        connection.close()
    return rows


def annotate_rows(
    positions: Sequence[dict[str, Any]],
    btc_candles: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for source in positions:
        row = dict(source)
        entry = _as_float(row["entry_price"], "entry_price")
        sl = _as_float(row["sl_price"], "sl_price")
        tp = _as_float(row["tp_price"], "tp_price")
        exit_price = _as_float(row["exit_price"], "exit_price")
        level = infer_level(entry, sl, tp)
        regime = regime_at_signal(
            btc_candles,
            _as_int(row["ts_open"], "ts_open"),
        )
        result_r = price_r(str(row["direction"]), entry, sl, exit_price)
        if not math.isfinite(result_r):
            raise ValueError(f"Non-finite result R for id={row['id']}")
        row.update(
            {
                "strategy": str(row["alert_type"]),
                "confirmation_level": level["confirmation_level"],
                "rr_multiple": level["rr_multiple"],
                "rr_multiple_observed": level["rr_multiple_observed"],
                "breakeven_wr_pct": level["breakeven_wr_pct"],
                "result_r": result_r,
                "outcome": "win" if row["status"] == "tp" else "loss",
                "trend_regime": regime["trend_regime"],
                "regime_reason": regime["regime_reason"],
                "btc_candle_ts": regime["btc_candle_ts"] or "",
                "btc_close": regime["btc_close"] or "",
                "btc_ema50": regime["btc_ema50"] or "",
            }
        )
        annotated.append(row)
    return annotated


def _status(n: int, minimum_n: int) -> str:
    return "ready" if n >= minimum_n else "insufficient"


def _stats(
    rows: Sequence[dict[str, Any]],
    *,
    minimum_n: int,
    level: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wins = sum(row["status"] == "tp" for row in rows)
    losses = sum(row["status"] == "sl" for row in rows)
    n = wins + losses
    wr_pct = 100.0 * wins / n if n else None
    avg_r = (
        sum(_as_float(row["result_r"], "result_r") for row in rows) / n
        if n
        else None
    )
    result: dict[str, Any] = {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr_pct": wr_pct,
        "avg_r": avg_r,
        "sample_status": _status(n, minimum_n),
    }
    if level is None:
        result.update(
            {
                "rr_multiple": None,
                "breakeven_wr_pct": None,
                "delta_wr_minus_breakeven_pp": None,
            }
        )
    else:
        result.update(
            {
                "rr_multiple": level["rr_multiple"],
                "breakeven_wr_pct": level["breakeven_wr_pct"],
                "delta_wr_minus_breakeven_pp": (
                    wr_pct - level["breakeven_wr_pct"] if wr_pct is not None else None
                ),
            }
        )
    return result


def build_cohort_rows(
    rows: Sequence[dict[str, Any]],
    *,
    minimum_n: int = MIN_GROUP_N,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for strategy in TARGET_STRATEGIES:
        for direction in DIRECTIONS:
            for regime in REGIMES:
                blended = [
                    row
                    for row in rows
                    if row["strategy"] == strategy
                    and row["direction"] == direction
                    and row["trend_regime"] == regime
                ]
                blended_stats = _stats(blended, minimum_n=minimum_n)
                output.append(
                    {
                        "sample": "blended",
                        "strategy": strategy,
                        "direction": direction,
                        "regime": regime,
                        "confirmation_level": "all",
                        "cohort": f"{strategy}|{direction}|{regime}",
                        **blended_stats,
                        "levels_present": ",".join(
                            level["confirmation_level"]
                            for level in LEVELS
                            if any(
                                row["confirmation_level"]
                                == level["confirmation_level"]
                                for row in blended
                            )
                        ),
                    }
                )
                for level in LEVELS:
                    level_rows = [
                        row
                        for row in blended
                        if row["confirmation_level"] == level["confirmation_level"]
                    ]
                    stats = _stats(
                        level_rows,
                        minimum_n=minimum_n,
                        level=level,
                    )
                    output.append(
                        {
                            "sample": "confirmation_level",
                            "strategy": strategy,
                            "direction": direction,
                            "regime": regime,
                            "confirmation_level": level["confirmation_level"],
                            "cohort": (
                                f"{strategy}|{direction}|{regime}|"
                                f"{level['confirmation_level']}"
                            ),
                            **stats,
                            "levels_present": level["confirmation_level"]
                            if level_rows
                            else "",
                        }
                    )
    return output


def build_strategy_hypotheses(
    cohort_rows: Sequence[dict[str, Any]],
    *,
    minimum_n: int = MIN_GROUP_N,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for strategy in TARGET_STRATEGIES:
        level_stats: dict[str, dict[str, Any]] = {}
        for level in ("1/3", "2/3", "3/3"):
            matches = [
                row
                for row in cohort_rows
                if row["sample"] == "confirmation_level"
                and row["strategy"] == strategy
                and row["confirmation_level"] == level
            ]
            total_n = sum(row["n"] for row in matches)
            total_wins = sum(row["wins"] for row in matches)
            total_losses = sum(row["losses"] for row in matches)
            total_r = sum(
                row["avg_r"] * row["n"]
                for row in matches
                if row["avg_r"] is not None
            )
            level_stats[level] = {
                "n": total_n,
                "wins": total_wins,
                "losses": total_losses,
                "wr_pct": 100.0 * total_wins / total_n if total_n else None,
                "avg_r": total_r / total_n if total_n else None,
                "breakeven_wr_pct": next(
                    level_row["breakeven_wr_pct"]
                    for level_row in LEVELS
                    if level_row["confirmation_level"] == level
                ),
            }
            level_stats[level]["delta_wr_minus_breakeven_pp"] = (
                level_stats[level]["wr_pct"]
                - level_stats[level]["breakeven_wr_pct"]
                if level_stats[level]["wr_pct"] is not None
                else None
            )

        late_ready = [
            level_stats[level]
            for level in ("2/3", "3/3")
            if level_stats[level]["n"] >= minimum_n
        ]
        negative_late = [
            level
            for level in ("2/3", "3/3")
            if level_stats[level]["n"] >= minimum_n
            and level_stats[level]["delta_wr_minus_breakeven_pp"] < 0
            and level_stats[level]["avg_r"] < 0
        ]
        if not late_ready:
            verdict = "insufficient"
        elif len(negative_late) == 2:
            verdict = "supported"
        elif negative_late:
            verdict = "partial_support"
        else:
            verdict = "not_supported"
        results.append(
            {
                "strategy": strategy,
                "blended_n": sum(
                    row["n"]
                    for row in cohort_rows
                    if row["sample"] == "blended" and row["strategy"] == strategy
                ),
                "level_1_3_n": level_stats["1/3"]["n"],
                "level_1_3_avg_r": level_stats["1/3"]["avg_r"],
                "level_1_3_delta_pp": level_stats["1/3"][
                    "delta_wr_minus_breakeven_pp"
                ],
                "level_2_3_n": level_stats["2/3"]["n"],
                "level_2_3_avg_r": level_stats["2/3"]["avg_r"],
                "level_2_3_delta_pp": level_stats["2/3"][
                    "delta_wr_minus_breakeven_pp"
                ],
                "level_3_3_n": level_stats["3/3"]["n"],
                "level_3_3_avg_r": level_stats["3/3"]["avg_r"],
                "level_3_3_delta_pp": level_stats["3/3"][
                    "delta_wr_minus_breakeven_pp"
                ],
                "late_ready_levels": ",".join(
                    level
                    for level in ("2/3", "3/3")
                    if level_stats[level]["n"] >= minimum_n
                ),
                "negative_late_levels": ",".join(negative_late),
                "hypothesis_verdict": verdict,
                "verdict_rule": (
                    "supported if both late levels have n>=minimum_n, "
                    "negative WR delta, and avg R<0; partial_support if "
                    "some but not all ready late levels meet that rule"
                ),
            }
        )
    return results


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def write_markdown(
    path: Path,
    report: dict[str, Any],
) -> None:
    lines = [
        "# Confirmation-level cohort analysis",
        "",
        "**Read-only descriptive analysis. Production, alert gates, alert format, and SQLite were not changed.**",
        "",
        "The confirmation level is reconstructed from persisted prices: TP/SL distance 2.0x = 1/3, 1.5x = 2/3, and 1.0x = 3/3. Result R uses the persisted entry, SL, and resolved exit. BTC regime uses the last completed BTC 4h candle available at signal time and its EMA50.",
        "",
        f"- Resolved rows: `{report['coverage']['resolved_rows']}`.",
        f"- Strategies: `{', '.join(TARGET_STRATEGIES)}`.",
        f"- Regime coverage: `{report['coverage']['regime_rows_missing']}` rows missing regime data; missing rows remain `unknown`.",
        f"- Minimum cohort size: `{report['config']['minimum_group_n']}`; smaller cells are **insufficient**.",
        "",
        "## Blended vs level-split cohorts",
        "",
        "| Sample | Strategy | Direction | Regime | Level | n | Wins | Losses | WR | avg R | R:R | BE WR | WR − BE | Status |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["cohort_rows"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["sample"]),
                    str(row["strategy"]),
                    str(row["direction"]),
                    str(row["regime"]),
                    str(row["confirmation_level"]),
                    str(row["n"]),
                    str(row["wins"]),
                    str(row["losses"]),
                    f"{row['wr_pct']:.4f}%" if row["wr_pct"] is not None else "",
                    fmt(row["avg_r"]),
                    fmt(row["rr_multiple"]),
                    (
                        f"{row['breakeven_wr_pct']:.4f}%"
                        if row["breakeven_wr_pct"] is not None
                        else ""
                    ),
                    (
                        f"{row['delta_wr_minus_breakeven_pp']:.4f} pp"
                        if row["delta_wr_minus_breakeven_pp"] is not None
                        else ""
                    ),
                    str(row["sample_status"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Hypothesis by strategy",
            "",
            "The verdict is deliberately per strategy. `supported` requires both late levels (2/3 and 3/3) to have at least 20 resolved trades, WR below their own breakeven, and negative avg R. `partial_support` means only some ready late levels meet that rule.",
            "",
            "| Strategy | Blended n | 1/3 n | 1/3 avg R | 1/3 Δ pp | 2/3 n | 2/3 avg R | 2/3 Δ pp | 3/3 n | 3/3 avg R | 3/3 Δ pp | Negative late levels | Verdict |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in report["hypotheses"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["strategy"]),
                    str(row["blended_n"]),
                    str(row["level_1_3_n"]),
                    fmt(row["level_1_3_avg_r"]),
                    fmt(row["level_1_3_delta_pp"]),
                    str(row["level_2_3_n"]),
                    fmt(row["level_2_3_avg_r"]),
                    fmt(row["level_2_3_delta_pp"]),
                    str(row["level_3_3_n"]),
                    fmt(row["level_3_3_avg_r"]),
                    fmt(row["level_3_3_delta_pp"]),
                    str(row["negative_late_levels"]),
                    str(row["hypothesis_verdict"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation guardrails",
            "",
            "- This is descriptive in-sample history, not a causal test and not a production gate recommendation.",
            "- Blended rows can hide level-specific negative expectancy because their breakeven WR is not a single number when R:R differs by level.",
            "- Empty LONG/SHORT or bull/bear cells are retained as `insufficient`, not dropped.",
            "- `unknown` regime rows are retained and are not silently treated as bull or bear.",
            "- The report excludes `open`, `ttl_expired`, and other unresolved statuses by design.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    db_path: Path,
    output_dir: Path,
    *,
    minimum_n: int = MIN_GROUP_N,
) -> dict[str, Any]:
    if minimum_n <= 0:
        raise ValueError("minimum_n must be positive")
    positions = load_resolved(db_path)
    if not positions:
        raise ValueError("No resolved confirmed-strategy positions found")

    try:
        btc_candles, btc_info = fetch_btc_history(positions)
    except Exception as exc:
        btc_candles = []
        btc_info = {
            "status": "fetch_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    annotated = annotate_rows(positions, btc_candles)
    cohort_rows = build_cohort_rows(annotated, minimum_n=minimum_n)
    hypotheses = build_strategy_hypotheses(cohort_rows, minimum_n=minimum_n)
    missing_regime = sum(
        row["regime_reason"] != "close_vs_ema50" for row in annotated
    )
    coverage = {
        "resolved_rows": len(annotated),
        "resolved_by_strategy": dict(
            Counter(row["strategy"] for row in annotated)
        ),
        "resolved_by_direction": dict(
            Counter(row["direction"] for row in annotated)
        ),
        "resolved_by_level": dict(
            Counter(row["confirmation_level"] for row in annotated)
        ),
        "status_counts": dict(Counter(row["status"] for row in annotated)),
        "regime_counts": dict(
            Counter(row["trend_regime"] for row in annotated)
        ),
        "regime_reason_counts": dict(
            Counter(row["regime_reason"] for row in annotated)
        ),
        "regime_rows_missing": missing_regime,
        "btc_fetch": btc_info,
        "analysis_run_utc": datetime.now(timezone.utc).isoformat(),
    }
    report = {
        "config": {
            "analysis": "confirmation_level_cohort_wr_avg_r",
            "target_strategies": list(TARGET_STRATEGIES),
            "resolved_statuses": ["tp", "sl"],
            "confirmation_level_source": "persisted TP/SL distance ratio",
            "level_mapping": [
                {
                    "confirmation_level": level["confirmation_level"],
                    "rr_multiple": level["rr_multiple"],
                    "breakeven_wr_pct": level["breakeven_wr_pct"],
                }
                for level in LEVELS
            ],
            "regime_source": "lookahead-safe BTC 4h close vs EMA50",
            "minimum_group_n": minimum_n,
            "read_only": True,
            "production_changes": False,
        },
        "coverage": coverage,
        "cohort_rows": cohort_rows,
        "hypotheses": hypotheses,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    audit_fields = [
        "id",
        "ts_open",
        "symbol",
        "direction",
        "strategy",
        "is_shadow",
        "status",
        "ts_close",
        "entry_price",
        "sl_price",
        "tp_price",
        "exit_price",
        "confirmation_level",
        "rr_multiple",
        "rr_multiple_observed",
        "breakeven_wr_pct",
        "result_r",
        "outcome",
        "trend_regime",
        "regime_reason",
        "btc_candle_ts",
        "btc_close",
        "btc_ema50",
        "shadow_reason",
    ]
    cohort_fields = [
        "sample",
        "strategy",
        "direction",
        "regime",
        "confirmation_level",
        "cohort",
        "n",
        "wins",
        "losses",
        "wr_pct",
        "avg_r",
        "rr_multiple",
        "breakeven_wr_pct",
        "delta_wr_minus_breakeven_pp",
        "sample_status",
        "levels_present",
    ]
    hypothesis_fields = [
        "strategy",
        "blended_n",
        "level_1_3_n",
        "level_1_3_avg_r",
        "level_1_3_delta_pp",
        "level_2_3_n",
        "level_2_3_avg_r",
        "level_2_3_delta_pp",
        "level_3_3_n",
        "level_3_3_avg_r",
        "level_3_3_delta_pp",
        "late_ready_levels",
        "negative_late_levels",
        "hypothesis_verdict",
        "verdict_rule",
    ]
    write_csv(output_dir / "resolved_level_audit.csv", annotated, audit_fields)
    write_csv(output_dir / "cohort_summary.csv", cohort_rows, cohort_fields)
    write_csv(output_dir / "hypothesis_summary.csv", hypotheses, hypothesis_fields)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "report.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).with_name("alerts.db"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("outcome_confirmation_level_analysis"),
    )
    parser.add_argument("--minimum-n", type=int, default=MIN_GROUP_N)
    args = parser.parse_args()
    report = run_analysis(args.db, args.out, minimum_n=args.minimum_n)
    print(
        json.dumps(
            {
                "output": str(args.out),
                "resolved_rows": report["coverage"]["resolved_rows"],
                "regime_rows_missing": report["coverage"]["regime_rows_missing"],
                "hypotheses": report["hypotheses"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())