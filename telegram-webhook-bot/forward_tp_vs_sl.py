"""Independent forward-shadow tracker for the frozen TP-vs-SL candidate.

This module is telemetry only.  It records a copy of qualifying shadow
positions and mirrors their outcome; it never changes signal eligibility,
pricing, execution, or the source demo position.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


FORWARD_EXPERIMENT_KEY = "ema_cross_confirmed_long_small_sl_v1"
FORWARD_STRATEGY = "ema_cross_confirmed"
FORWARD_DIRECTION = "LONG"
FORWARD_SL_THRESHOLD_PCT = 3.55255
# Frozen before the first eligible forward signal for this experiment:
# 2026-08-27 19:40:00 UTC.
FORWARD_FREEZE_TS = 1787859600
FORWARD_MIN_OUTCOME_N = 20
FORWARD_TABLE = "tp_vs_sl_forward_positions"
FORWARD_META_TABLE = "tp_vs_sl_forward_meta"
FORWARD_REPORT_DIR = Path("outcome_tp_vs_sl_forward")


def _utc(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _risk_pct(entry_price: float, sl_price: float) -> float:
    if not all(
        math.isfinite(float(value)) and float(value) > 0
        for value in (entry_price, sl_price)
    ):
        raise ValueError("entry and SL must be finite positive numbers")
    return abs(float(entry_price) - float(sl_price)) / float(entry_price) * 100.0


def classify_risk(risk_pct: float) -> str:
    """Apply the frozen inclusive boundary without re-fitting it."""
    if not math.isfinite(float(risk_pct)):
        raise ValueError("risk_pct must be finite")
    return (
        "tp_candidate"
        if float(risk_pct) <= FORWARD_SL_THRESHOLD_PCT
        else "sl_candidate"
    )


def _validate_meta(row: sqlite3.Row | tuple[Any, ...]) -> None:
    values = tuple(row)
    expected = (
        FORWARD_EXPERIMENT_KEY,
        FORWARD_STRATEGY,
        FORWARD_DIRECTION,
        FORWARD_FREEZE_TS,
        FORWARD_SL_THRESHOLD_PCT,
        FORWARD_MIN_OUTCOME_N,
    )
    if values[0] != expected[0] or values[1] != expected[1] or values[2] != expected[2]:
        raise RuntimeError("forward TP-vs-SL experiment metadata does not match frozen rule")
    if int(values[3]) != expected[3] or not math.isclose(
        float(values[4]), expected[4], rel_tol=0.0, abs_tol=1e-12
    ) or int(values[5]) != expected[5]:
        raise RuntimeError(
            "forward TP-vs-SL frozen timestamp, threshold, or minimum was changed"
        )


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create immutable experiment metadata and the independent tracker."""
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FORWARD_META_TABLE} (
            experiment_key TEXT PRIMARY KEY,
            strategy TEXT NOT NULL,
            direction TEXT NOT NULL,
            freeze_ts INTEGER NOT NULL,
            sl_threshold_pct REAL NOT NULL,
            min_outcome_n INTEGER NOT NULL,
            created_ts INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FORWARD_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_demo_id INTEGER NOT NULL UNIQUE,
            ts_open INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            strategy TEXT NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            tp_price REAL NOT NULL,
            risk_pct REAL NOT NULL,
            rule_prediction TEXT NOT NULL
                CHECK (rule_prediction IN ('tp_candidate', 'sl_candidate')),
            status TEXT NOT NULL DEFAULT 'open',
            ts_close INTEGER,
            exit_price REAL,
            result_r REAL
        )
        """
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{FORWARD_TABLE}_status "
        f"ON {FORWARD_TABLE}(status, rule_prediction)"
    )
    connection.execute(
        f"""
        INSERT OR IGNORE INTO {FORWARD_META_TABLE} (
            experiment_key, strategy, direction, freeze_ts,
            sl_threshold_pct, min_outcome_n, created_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            FORWARD_EXPERIMENT_KEY,
            FORWARD_STRATEGY,
            FORWARD_DIRECTION,
            FORWARD_FREEZE_TS,
            FORWARD_SL_THRESHOLD_PCT,
            FORWARD_MIN_OUTCOME_N,
            FORWARD_FREEZE_TS,
        ),
    )
    row = connection.execute(
        f"""
        SELECT experiment_key, strategy, direction, freeze_ts,
               sl_threshold_pct, min_outcome_n
          FROM {FORWARD_META_TABLE}
         WHERE experiment_key=?
        """,
        (FORWARD_EXPERIMENT_KEY,),
    ).fetchone()
    if row is None:
        raise RuntimeError("forward TP-vs-SL metadata was not persisted")
    _validate_meta(row)
    # Recover qualifying rows created between the freeze and a process restart.
    # This does not import historical rows and remains independent of outcomes.
    connection.execute(
        f"""
        INSERT OR IGNORE INTO {FORWARD_TABLE} (
            source_demo_id, ts_open, symbol, direction, strategy,
            entry_price, sl_price, tp_price, risk_pct, rule_prediction,
            status, ts_close, exit_price, result_r
        )
        SELECT id, ts_open, symbol, direction, alert_type,
               entry_price, sl_price, tp_price,
               ABS(entry_price - sl_price) / entry_price * 100.0,
               CASE WHEN ABS(entry_price - sl_price) / entry_price * 100.0
                         <= ? THEN 'tp_candidate' ELSE 'sl_candidate' END,
               status, ts_close, exit_price,
               CASE
                 WHEN direction='LONG' AND ABS(sl_price-entry_price) > 0
                   THEN (exit_price-entry_price) /
                        ABS(sl_price-entry_price)
                 ELSE NULL
               END
          FROM demo_positions
         WHERE id IS NOT NULL
           AND is_shadow=1
           AND alert_type=?
           AND direction=?
           AND ts_open >= ?
           AND entry_price > 0
           AND sl_price > 0
           AND tp_price > 0
        """,
        (
            FORWARD_SL_THRESHOLD_PCT,
            FORWARD_STRATEGY,
            FORWARD_DIRECTION,
            FORWARD_FREEZE_TS,
        ),
    )


def track_position(
    connection: sqlite3.Connection,
    *,
    source_demo_id: int,
    ts_open: int,
    symbol: str,
    direction: str,
    alert_type: str,
    is_shadow: bool,
    entry_price: float,
    sl_price: float,
    tp_price: float,
) -> bool:
    """Record one new qualifying position; return whether a row was inserted."""
    if not (
        is_shadow
        and direction == FORWARD_DIRECTION
        and alert_type == FORWARD_STRATEGY
        and int(ts_open) >= FORWARD_FREEZE_TS
    ):
        return False
    risk_pct = _risk_pct(entry_price, sl_price)
    if not math.isfinite(float(tp_price)) or float(tp_price) <= 0:
        raise ValueError("tp_price must be finite and positive")
    cursor = connection.execute(
        f"""
        INSERT OR IGNORE INTO {FORWARD_TABLE} (
            source_demo_id, ts_open, symbol, direction, strategy,
            entry_price, sl_price, tp_price, risk_pct, rule_prediction
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(source_demo_id),
            int(ts_open),
            symbol,
            direction,
            alert_type,
            float(entry_price),
            float(sl_price),
            float(tp_price),
            risk_pct,
            classify_risk(risk_pct),
        ),
    )
    return cursor.rowcount > 0


def sync_outcome(
    connection: sqlite3.Connection,
    *,
    source_demo_id: int,
    status: str,
    ts_close: int | None,
    exit_price: float | None,
) -> bool:
    """Mirror a source outcome without changing the source demo position."""
    row = connection.execute(
        f"""
        SELECT entry_price, sl_price
          FROM {FORWARD_TABLE}
         WHERE source_demo_id=?
        """,
        (int(source_demo_id),),
    ).fetchone()
    if row is None:
        return False
    result_r = None
    if exit_price is not None and float(row[1]) != float(row[0]):
        result_r = (float(exit_price) - float(row[0])) / abs(
            float(row[1]) - float(row[0])
        )
    connection.execute(
        f"""
        UPDATE {FORWARD_TABLE}
           SET status=?, ts_close=?, exit_price=?, result_r=?
         WHERE source_demo_id=?
        """,
        (status, ts_close, exit_price, result_r, int(source_demo_id)),
    )
    return True


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(row["status"] == "tp" for row in rows)
    sl = sum(row["status"] == "sl" for row in rows)
    unresolved = len(rows) - tp - sl
    resolved = tp + sl
    result_rs = [
        float(row["result_r"])
        for row in rows
        if row.get("result_r") is not None
        and row["status"] in ("tp", "sl")
    ]
    return {
        "n_total": len(rows),
        "tp_first": tp,
        "sl_first": sl,
        "unresolved": unresolved,
        "resolved_n": resolved,
        "resolved_wr_pct": round(tp / resolved * 100.0, 4) if resolved else None,
        "avg_r": round(mean(result_rs), 6) if result_rs else None,
    }


def _confusion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [row for row in rows if row["status"] in ("tp", "sl")]
    matrix = {
        "predicted_tp_actual_tp": 0,
        "predicted_tp_actual_sl": 0,
        "predicted_sl_actual_tp": 0,
        "predicted_sl_actual_sl": 0,
    }
    for row in resolved:
        key = (
            "predicted_tp" if row["rule_prediction"] == "tp_candidate"
            else "predicted_sl"
        ) + "_actual_" + row["status"]
        matrix[key] += 1
    predicted_tp = matrix["predicted_tp_actual_tp"] + matrix["predicted_tp_actual_sl"]
    predicted_sl = matrix["predicted_sl_actual_tp"] + matrix["predicted_sl_actual_sl"]
    correct = matrix["predicted_tp_actual_tp"] + matrix["predicted_sl_actual_sl"]
    return {
        **matrix,
        "resolved_n": len(resolved),
        "accuracy": round(correct / len(resolved), 6) if resolved else None,
        "precision_tp": (
            round(matrix["predicted_tp_actual_tp"] / predicted_tp, 6)
            if predicted_tp else None
        ),
        "precision_sl": (
            round(matrix["predicted_sl_actual_sl"] / predicted_sl, 6)
            if predicted_sl else None
        ),
    }


def _verdict(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[str, str]:
    if (
        baseline["tp_first"] < FORWARD_MIN_OUTCOME_N
        or baseline["sl_first"] < FORWARD_MIN_OUTCOME_N
    ):
        return (
            "insufficient",
            "Wait for n≥20 TP-first and n≥20 SL-first in the forward sample.",
        )
    if candidate["resolved_n"] == 0:
        return "inconclusive", "Enough overall outcomes exist, but no resolved candidate rows exist."
    if (
        candidate["resolved_wr_pct"] is not None
        and baseline["resolved_wr_pct"] is not None
        and candidate["avg_r"] is not None
        and baseline["avg_r"] is not None
    ):
        if (
            candidate["resolved_wr_pct"] > baseline["resolved_wr_pct"]
            and candidate["avg_r"] > baseline["avg_r"]
        ):
            return "confirmed", "Candidate WR and avg R both exceed the no-rule baseline."
        if (
            candidate["resolved_wr_pct"] < baseline["resolved_wr_pct"]
            and candidate["avg_r"] < baseline["avg_r"]
        ):
            return "refuted", "Candidate WR and avg R are both below the no-rule baseline."
    return "inconclusive", "The frozen candidate does not beat or lose to baseline on both metrics."


def load_rows(db_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        meta = connection.execute(
            f"""
            SELECT experiment_key, strategy, direction, freeze_ts,
                   sl_threshold_pct, min_outcome_n
              FROM {FORWARD_META_TABLE}
             WHERE experiment_key=?
            """,
            (FORWARD_EXPERIMENT_KEY,),
        ).fetchone()
        if meta is None:
            raise RuntimeError("forward TP-vs-SL tracker schema is not initialized")
        _validate_meta(meta)
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, source_demo_id, ts_open, symbol, direction, strategy,
                       entry_price, sl_price, tp_price, risk_pct,
                       rule_prediction, status, ts_close, exit_price, result_r
                  FROM {FORWARD_TABLE}
                 ORDER BY ts_open, id
                """
            )
        ]
    finally:
        connection.close()
    return dict(meta), rows


def build_report(
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    generated_ts: int | None = None,
) -> dict[str, Any]:
    baseline = _metrics(rows)
    candidate_rows = [
        row for row in rows if row["rule_prediction"] == "tp_candidate"
    ]
    control_rows = [
        row for row in rows if row["rule_prediction"] == "sl_candidate"
    ]
    candidate = _metrics(candidate_rows)
    control = _metrics(control_rows)
    verdict, verdict_reason = _verdict(baseline, candidate)
    return {
        "experiment": {
            "key": meta["experiment_key"],
            "strategy": meta["strategy"],
            "direction": meta["direction"],
            "freeze_ts": int(meta["freeze_ts"]),
            "freeze_utc": _utc(meta["freeze_ts"]),
            "sl_threshold_pct": float(meta["sl_threshold_pct"]),
            "rule": "risk_pct <= 3.55255% predicts TP-first; risk_pct > 3.55255% is control",
            "minimum_per_outcome_n": int(meta["min_outcome_n"]),
        },
        "generated_ts": generated_ts,
        "generated_utc": _utc(generated_ts),
        "scope": (
            "Only new is_shadow=1 ema_cross_confirmed LONG rows with "
            "ts_open >= frozen freeze_ts; historical Task #143 rows excluded."
        ),
        "baseline_no_rule": baseline,
        "candidate_small_sl": {
            **candidate,
            "confusion": _confusion(candidate_rows),
        },
        "control_large_sl": control,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "verdict_is_allowed": (
            baseline["tp_first"] >= FORWARD_MIN_OUTCOME_N
            and baseline["sl_first"] >= FORWARD_MIN_OUTCOME_N
        ),
        "rows": rows,
    }


def write_report(
    db_path: Path,
    output_dir: Path = FORWARD_REPORT_DIR,
    *,
    generated_ts: int | None = None,
) -> dict[str, Any]:
    if generated_ts is None:
        import time

        generated_ts = int(time.time())
    meta, rows = load_rows(db_path)
    report = build_report(meta, rows, generated_ts=generated_ts)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    fields = [
        "id", "source_demo_id", "ts_open", "symbol", "direction", "strategy",
        "entry_price", "sl_price", "tp_price", "risk_pct", "rule_prediction",
        "status", "ts_close", "exit_price", "result_r",
    ]
    with (output_dir / "rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    baseline = report["baseline_no_rule"]
    candidate = report["candidate_small_sl"]
    control = report["control_large_sl"]
    lines = [
        "# Forward TP-vs-SL — frozen `ema_cross_confirmed LONG` experiment",
        "",
        "**Forward-shadow telemetry only. No trading filter, score, SL/TP, execution, or Telegram behavior is changed.**",
        "",
        f"- Frozen start: **{report['experiment']['freeze_utc']}** (`{report['experiment']['freeze_ts']}`)",
        f"- Frozen rule: **SL distance ≤ {report['experiment']['sl_threshold_pct']:.5f}%** predicts TP-first.",
        "- Direction: **LONG only**; `ema_cross_confirmed SHORT` is excluded.",
        f"- Minimum verdict sample: **{report['experiment']['minimum_per_outcome_n']} TP-first and "
        f"{report['experiment']['minimum_per_outcome_n']} SL-first**.",
        f"- Generated: **{report['generated_utc']}**",
        "",
        "## Current verdict",
        "",
        f"**{report['verdict'].upper()}** — {report['verdict_reason']}",
        "",
        "| Cohort | Total | TP-first | SL-first | Unresolved | Resolved WR | avg R |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| No-rule baseline | {baseline['n_total']} | {baseline['tp_first']} | "
        f"{baseline['sl_first']} | {baseline['unresolved']} | "
        f"{baseline['resolved_wr_pct'] if baseline['resolved_wr_pct'] is not None else '—'}% | "
        f"{baseline['avg_r'] if baseline['avg_r'] is not None else '—'} |",
        f"| SL ≤ threshold (candidate) | {candidate['n_total']} | {candidate['tp_first']} | "
        f"{candidate['sl_first']} | {candidate['unresolved']} | "
        f"{candidate['resolved_wr_pct'] if candidate['resolved_wr_pct'] is not None else '—'}% | "
        f"{candidate['avg_r'] if candidate['avg_r'] is not None else '—'} |",
        f"| SL > threshold (control) | {control['n_total']} | {control['tp_first']} | "
        f"{control['sl_first']} | {control['unresolved']} | "
        f"{control['resolved_wr_pct'] if control['resolved_wr_pct'] is not None else '—'}% | "
        f"{control['avg_r'] if control['avg_r'] is not None else '—'} |",
        "",
        "## Candidate confusion / precision",
        "",
    ]
    confusion = candidate["confusion"]
    lines += [
        f"- Accuracy on resolved rows: **{confusion['accuracy'] if confusion['accuracy'] is not None else '—'}**",
        f"- Precision TP: **{confusion['precision_tp'] if confusion['precision_tp'] is not None else '—'}**",
        f"- Precision SL: **{confusion['precision_sl'] if confusion['precision_sl'] is not None else '—'}**",
        f"- Matrix: TP→TP={confusion['predicted_tp_actual_tp']}, TP→SL={confusion['predicted_tp_actual_sl']}, "
        f"SL→TP={confusion['predicted_sl_actual_tp']}, SL→SL={confusion['predicted_sl_actual_sl']}.",
        "",
        "## Guardrails",
        "",
        "- `unresolved` includes open/non-TP/SL rows and is never counted in WR or avg R.",
        "- No verdict is allowed until both overall forward outcome classes have n≥20.",
        "- The threshold and freeze timestamp are persisted in experiment metadata; startup fails if they drift.",
        "- The tracker mirrors source outcomes and is independent from `demo_positions` decision-making.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("alerts.db"))
    parser.add_argument("--out", type=Path, default=FORWARD_REPORT_DIR)
    args = parser.parse_args()
    report = write_report(args.db, args.out)
    print(
        f"Wrote {args.out / 'report.md'} "
        f"(verdict={report['verdict']}, n={report['baseline_no_rule']['n_total']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())