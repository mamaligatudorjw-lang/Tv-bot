#!/usr/bin/env python3
"""Read-only historical breakdown of demo-position close reasons.

The input cohort follows the current Telegram notification strategy allowlist,
without importing app.py. Every non-open position is retained in the audit:
TP and SL are outcome reasons, manual closes are admin, and any other status is
other. BTC regime is joined from the existing lookahead-safe snapshot by ID.

This script never writes to SQLite or production state.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from trailing_stop_analysis import price_r


DEFAULT_TELEGRAM_NOTIFICATION_STRATEGIES = (
    "ema_cross_confirmed",
    "overheated_early",
    "ema_cross",
    "overheated_confirmed",
)
DIRECTIONS = ("LONG", "SHORT")
REGIMES = ("bull", "bear", "unknown")
CLOSE_REASONS = ("tp", "sl", "admin", "other")
MIN_GROUP_N = 20


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


def _as_float_or_none(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    return _as_float(value, field)


def parse_strategy_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated allowlist while preserving configured order."""
    if raw is None:
        values = DEFAULT_TELEGRAM_NOTIFICATION_STRATEGIES
    else:
        values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ValueError("Telegram notification strategy allowlist is empty")
    if len(set(values)) != len(values):
        raise ValueError("Telegram notification strategy allowlist has duplicates")
    return tuple(values)


def configured_strategies(cli_value: str | None = None) -> tuple[str, ...]:
    if cli_value is not None:
        return parse_strategy_list(cli_value)
    return parse_strategy_list(os.environ.get("TELEGRAM_NOTIFICATION_STRATEGIES"))


def load_resolved(
    db_path: Path,
    strategies: Sequence[str],
) -> list[dict[str, Any]]:
    """Load every non-open position for the configured Telegram strategies."""
    if not strategies:
        raise ValueError("At least one strategy is required")
    placeholders = ",".join("?" for _ in strategies)
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, ts_open, symbol, direction, entry_price, sl_price,
                       tp_price, size_usd, status, ts_close, exit_price, pnl_usd,
                       is_shadow, shadow_reason, alert_type, exit_method,
                       wick_close
                  FROM demo_positions
                 WHERE alert_type IN ({placeholders})
                   AND direction IN ('LONG', 'SHORT')
                   AND status IS NOT NULL
                   AND status != 'open'
                   AND entry_price > 0
                   AND sl_price > 0
                 ORDER BY ts_open, id
                """,
                tuple(strategies),
            )
        ]
    finally:
        connection.close()
    return rows


def load_regime_snapshot(path: Path) -> dict[str, dict[str, str]]:
    """Load the existing frozen regime snapshot keyed by signal ID."""
    snapshot: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        signal_id = str(row.get("id", ""))
        if not signal_id:
            raise ValueError(f"Regime snapshot row has no id: {row!r}")
        if signal_id in snapshot:
            raise ValueError(f"Duplicate id in regime snapshot: {signal_id}")
        regime = row.get("trend_regime", "")
        if regime not in REGIMES:
            raise ValueError(
                f"Invalid trend_regime for id={signal_id}: {regime!r}"
            )
        snapshot[signal_id] = row
    return snapshot


def classify_close_reason(row: dict[str, Any]) -> str:
    """Normalize persisted status/exit method into four report categories."""
    status = str(row.get("status") or "").strip().lower()
    exit_method = str(row.get("exit_method") or "").strip().lower()
    if status == "tp":
        return "tp"
    if status == "sl":
        return "sl"
    if status == "manual" or exit_method == "manual":
        return "admin"
    return "other"


def annotate_rows(
    positions: Iterable[dict[str, Any]],
    regime_snapshot: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Attach normalized close reason, directional R, and frozen regime."""
    annotated: list[dict[str, Any]] = []
    for source in positions:
        row = dict(source)
        close_reason = classify_close_reason(row)
        snapshot_row = regime_snapshot.get(str(row["id"]))
        if snapshot_row is None:
            regime = {
                "trend_regime": "unknown",
                "regime_reason": "snapshot_missing",
                "btc_candle_ts": "",
                "btc_close": "",
                "btc_ema50": "",
            }
        else:
            regime = {
                "trend_regime": snapshot_row["trend_regime"],
                "regime_reason": snapshot_row.get(
                    "regime_reason", "snapshot_join"
                ),
                "btc_candle_ts": snapshot_row.get("btc_candle_ts", ""),
                "btc_close": snapshot_row.get("btc_close", ""),
                "btc_ema50": snapshot_row.get("btc_ema50", ""),
            }

        result_r: float | None = None
        if close_reason in ("tp", "sl") and row.get("exit_price") is not None:
            result_r = price_r(
                str(row["direction"]),
                _as_float(row["entry_price"], "entry_price"),
                _as_float(row["sl_price"], "sl_price"),
                _as_float(row["exit_price"], "exit_price"),
            )
            if not math.isfinite(result_r):
                result_r = None

        row.update(
            {
                "strategy": str(row.get("alert_type") or "unknown"),
                "close_reason": close_reason,
                "result_r": result_r,
                "r_included": result_r is not None,
                "outcome": close_reason if close_reason in ("tp", "sl") else "",
                "trend_regime": regime["trend_regime"],
                "regime_reason": regime["regime_reason"],
                "btc_candle_ts": regime["btc_candle_ts"] or "",
                "btc_close": regime["btc_close"] or "",
                "btc_ema50": regime["btc_ema50"] or "",
            }
        )
        annotated.append(row)
    return annotated


def _pct(numerator: int, denominator: int) -> float | None:
    return 100.0 * numerator / denominator if denominator else None


def _sample_status(n: int, minimum_n: int) -> str:
    return "ready" if n >= minimum_n else "insufficient"


def metrics(rows: Sequence[dict[str, Any]], *, minimum_n: int) -> dict[str, Any]:
    counts = Counter(str(row["close_reason"]) for row in rows)
    n = len(rows)
    tp_n = counts["tp"]
    sl_n = counts["sl"]
    admin_n = counts["admin"]
    other_n = counts["other"]
    outcome_n = tp_n + sl_n
    r_values = [
        _as_float(row["result_r"], "result_r")
        for row in rows
        if row.get("result_r") is not None
    ]
    return {
        "n": n,
        "tp_n": tp_n,
        "sl_n": sl_n,
        "admin_n": admin_n,
        "other_n": other_n,
        "tp_share_pct": _pct(tp_n, n),
        "sl_share_pct": _pct(sl_n, n),
        "admin_share_pct": _pct(admin_n, n),
        "other_share_pct": _pct(other_n, n),
        "outcome_n": outcome_n,
        "wr_pct": _pct(tp_n, outcome_n),
        "r_n": len(r_values),
        "avg_r": sum(r_values) / len(r_values) if r_values else None,
        "sample_status": _sample_status(n, minimum_n),
    }


def build_summary(
    rows: Sequence[dict[str, Any]],
    strategies: Sequence[str],
    *,
    minimum_n: int = MIN_GROUP_N,
) -> list[dict[str, Any]]:
    """Build explicit overall and strategy × direction × regime cohorts."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        direction = str(row["direction"])
        regime = str(row.get("trend_regime") or "unknown")
        strategy = str(row.get("strategy") or row.get("alert_type") or "unknown")
        grouped[("ALL", direction, regime)].append(row)
        grouped[(strategy, direction, regime)].append(row)

    output: list[dict[str, Any]] = []
    for direction in DIRECTIONS:
        for regime in REGIMES:
            output.append(
                {
                    "scope": "overall",
                    "strategy": "ALL",
                    "direction": direction,
                    "trend_regime": regime,
                    **metrics(
                        grouped.get(("ALL", direction, regime), []),
                        minimum_n=minimum_n,
                    ),
                }
            )
    for strategy in strategies:
        for direction in DIRECTIONS:
            for regime in REGIMES:
                output.append(
                    {
                        "scope": "strategy",
                        "strategy": strategy,
                        "direction": direction,
                        "trend_regime": regime,
                        **metrics(
                            grouped.get((strategy, direction, regime), []),
                            minimum_n=minimum_n,
                        ),
                    }
                )
    return output


def build_coverage(
    rows: Sequence[dict[str, Any]],
    strategies: Sequence[str],
    *,
    snapshot_path: Path,
    snapshot_rows: int,
    strategy_source: str,
) -> dict[str, Any]:
    regime_reasons = Counter(str(row["regime_reason"]) for row in rows)
    regimes = Counter(str(row["trend_regime"]) for row in rows)
    return {
        "resolved_rows": len(rows),
        "resolved_by_strategy": dict(
            Counter(str(row["strategy"]) for row in rows)
        ),
        "resolved_by_direction": dict(
            Counter(str(row["direction"]) for row in rows)
        ),
        "close_reason_counts": dict(
            Counter(str(row["close_reason"]) for row in rows)
        ),
        "persisted_status_counts": dict(
            Counter(str(row["status"]) for row in rows)
        ),
        "regime_counts": dict(regimes),
        "regime_reason_counts": dict(regime_reasons),
        "regime_snapshot_path": str(snapshot_path),
        "regime_snapshot_rows": snapshot_rows,
        "regime_snapshot_joined_rows": len(rows) - regime_reasons["snapshot_missing"],
        "regime_snapshot_missing_rows": regime_reasons["snapshot_missing"],
        "telegram_notification_strategies": list(strategies),
        "strategy_selection_source": strategy_source,
        "analysis_run_utc": datetime.now(timezone.utc).isoformat(),
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: (
                        ""
                        if row.get(field) is None
                        else row.get(field)
                    )
                    for field in fields
                }
            )


def _display(value: Any, *, percent: bool = False) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):.2f}%" if percent else f"{float(value):.4f}"


def _summary_table(
    rows: Sequence[dict[str, Any]],
    *,
    minimum_n: int,
) -> list[str]:
    lines = [
        "| Strategy | Direction | Regime | n | TP | SL | Admin | Other | TP share | SL share | Admin share | Other share | WR (TP/(TP+SL)) | avg R | Sample |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        status = (
            "ready"
            if row["sample_status"] == "ready"
            else f"INSUFFICIENT (<{minimum_n}; n={row['n']})"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["strategy"]),
                    str(row["direction"]),
                    str(row["trend_regime"]),
                    str(row["n"]),
                    str(row["tp_n"]),
                    str(row["sl_n"]),
                    str(row["admin_n"]),
                    str(row["other_n"]),
                    _display(row["tp_share_pct"], percent=True),
                    _display(row["sl_share_pct"], percent=True),
                    _display(row["admin_share_pct"], percent=True),
                    _display(row["other_share_pct"], percent=True),
                    _display(row["wr_pct"], percent=True),
                    _display(row["avg_r"]),
                    status,
                ]
            )
            + " |"
        )
    return lines


def write_report(
    output_dir: Path,
    annotated: Sequence[dict[str, Any]],
    summary: Sequence[dict[str, Any]],
    coverage: dict[str, Any],
    *,
    minimum_n: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_fields = [
        "id",
        "ts_open",
        "symbol",
        "direction",
        "strategy",
        "alert_type",
        "is_shadow",
        "shadow_reason",
        "status",
        "exit_method",
        "ts_close",
        "entry_price",
        "sl_price",
        "tp_price",
        "exit_price",
        "pnl_usd",
        "close_reason",
        "result_r",
        "r_included",
        "outcome",
        "trend_regime",
        "regime_reason",
        "btc_candle_ts",
        "btc_close",
        "btc_ema50",
    ]
    summary_fields = [
        "scope",
        "strategy",
        "direction",
        "trend_regime",
        "n",
        "tp_n",
        "sl_n",
        "admin_n",
        "other_n",
        "tp_share_pct",
        "sl_share_pct",
        "admin_share_pct",
        "other_share_pct",
        "outcome_n",
        "wr_pct",
        "r_n",
        "avg_r",
        "sample_status",
    ]
    write_csv(output_dir / "close_reason_audit.csv", annotated, audit_fields)
    write_csv(output_dir / "summary.csv", summary, summary_fields)

    report = {
        "analysis": "telegram_close_reason_historical",
        "read_only": True,
        "production_changes": False,
        "config": {
            "minimum_group_n": minimum_n,
            "close_reason_mapping": {
                "tp": "status=tp",
                "sl": "status=sl",
                "admin": "status=manual or exit_method=manual",
                "other": "any other non-open status",
            },
            "wr_definition": "tp / (tp + sl); admin and other excluded",
            "avg_r_definition": (
                "mean directional result R for TP/SL rows with an exit price; "
                "admin and other excluded"
            ),
        },
        "coverage": coverage,
        "summary": list(summary),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    overall = [row for row in summary if row["scope"] == "overall"]
    strategy_rows = [row for row in summary if row["scope"] == "strategy"]
    lines = [
        "# Historical close-reason analysis",
        "",
        "**Read-only analysis. Production logic and the SQLite database were not changed.**",
        "",
        "The cohort includes every non-open `demo_positions` row whose strategy is in the current Telegram notification allowlist. TP and SL are outcome reasons; `manual` is `admin`; all other non-open statuses are `other`.",
        "",
        "WR is calculated as `TP / (TP + SL)`, so admin and other closures remain visible in counts and shares but do not inflate or depress WR. avg R is calculated only for TP/SL rows with a persisted exit price.",
        "",
        f"- Minimum cohort size: `{minimum_n}`; smaller cells are **insufficient**.",
        f"- Frozen regime join: `{coverage['regime_snapshot_joined_rows']}` joined, `{coverage['regime_snapshot_missing_rows']}` missing and retained as `unknown`.",
        f"- Strategies: `{', '.join(coverage['telegram_notification_strategies'])}`.",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(coverage, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Close-reason mapping",
        "",
        "| Report reason | Persisted condition | Included in WR/avg R |",
        "|---|---|---|",
        "| `tp` | `status=tp` | Yes |",
        "| `sl` | `status=sl` | Yes |",
        "| `admin` | `status=manual` or `exit_method=manual` | No |",
        "| `other` | Any other non-open status | No |",
        "",
        "## Overall by direction and regime",
        "",
        *_summary_table(overall, minimum_n=minimum_n),
        "",
        "## By strategy, direction and regime",
        "",
        *_summary_table(strategy_rows, minimum_n=minimum_n),
        "",
        "## Guardrails",
        "",
        f"- Every configured strategy has explicit LONG and SHORT rows for bull, bear, and unknown regimes; empty or sub-{minimum_n} cohorts stay **INSUFFICIENT**.",
        "- Missing regime IDs are retained in the audit as `unknown` with `snapshot_missing`; the snapshot is not refreshed.",
        "- This is descriptive historical analysis, not a gate/filter recommendation or a forward test.",
        "",
    ]
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_analysis(
    db_path: Path,
    snapshot_path: Path,
    output_dir: Path,
    strategies: Sequence[str],
    *,
    minimum_n: int = MIN_GROUP_N,
    strategy_source: str = "configured",
) -> dict[str, Any]:
    if minimum_n <= 0:
        raise ValueError("minimum_n must be positive")
    positions = load_resolved(db_path, strategies)
    snapshot = load_regime_snapshot(snapshot_path)
    annotated = annotate_rows(positions, snapshot)
    summary = build_summary(annotated, strategies, minimum_n=minimum_n)
    coverage = build_coverage(
        annotated,
        strategies,
        snapshot_path=snapshot_path,
        snapshot_rows=len(snapshot),
        strategy_source=strategy_source,
    )
    write_report(
        output_dir,
        annotated,
        summary,
        coverage,
        minimum_n=minimum_n,
    )
    return {
        "output": str(output_dir),
        "resolved_rows": coverage["resolved_rows"],
        "close_reason_counts": coverage["close_reason_counts"],
        "regime_snapshot_missing_rows": coverage["regime_snapshot_missing_rows"],
        "strategies": list(strategies),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).with_name("alerts.db"),
    )
    parser.add_argument(
        "--regime-snapshot",
        type=Path,
        default=Path(__file__).with_name("trend_regime_analysis")
        / "signal_regimes.csv",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("outcome_exit_reason_analysis"),
    )
    parser.add_argument("--minimum-n", type=int, default=MIN_GROUP_N)
    parser.add_argument(
        "--strategies",
        help="Comma-separated Telegram notification strategy allowlist override",
    )
    args = parser.parse_args()
    try:
        strategies = configured_strategies(args.strategies)
        strategy_source = (
            "cli --strategies override"
            if args.strategies is not None
            else (
                "TELEGRAM_NOTIFICATION_STRATEGIES environment override"
                if os.environ.get("TELEGRAM_NOTIFICATION_STRATEGIES") is not None
                else "app.py default Telegram notification allowlist"
            )
        )
        result = run_analysis(
            args.db,
            args.regime_snapshot,
            args.out,
            strategies,
            minimum_n=args.minimum_n,
            strategy_source=strategy_source,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())