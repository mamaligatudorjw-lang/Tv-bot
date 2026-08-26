"""Forward-only trailing shadow instrumentation.

This module deliberately has no exchange/history imports.  Its state machine is
advanced only with live prices supplied by app.check_demo_positions(), while
the report reads the persisted source and shadow rows after both sides resolve.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import random
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


logger = logging.getLogger(__name__)

# Freeze once for this forward experiment.  Do not derive this from process
# startup time: restarts must not move the OOS boundary.
TRAILING_SHADOW_FREEZE_TS = 1787745058  # 2026-08-26T11:50:58Z
MIN_FORWARD_PAIRS = 20
BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20260826
REPORT_DIR = Path(__file__).with_name("outcome_trailing_shadow")

TRAILING_SHADOW_CONFIGS: dict[str, dict[str, Any]] = {
    "overheated_24h": {
        "step_pct": 8.0,
        "activation_r": 0.5,
        "activation_label": "+0.5R",
    },
    "ema_cross_confirmed": {
        "step_pct": 6.0,
        "activation_r": None,
        "activation_label": "any_profit",
    },
}

_report_lock = threading.Lock()
_report_running = False


def tracked_strategy(alert_type: str | None, ts_open: int) -> bool:
    return (
        alert_type in TRAILING_SHADOW_CONFIGS
        and int(ts_open) >= TRAILING_SHADOW_FREEZE_TS
    )


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create only the separate shadow tables; never alter demo_positions."""
    cursor = connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trailing_shadow_positions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            source_demo_id     INTEGER NOT NULL UNIQUE,
            ts_open            INTEGER NOT NULL,
            symbol             TEXT NOT NULL,
            direction          TEXT NOT NULL,
            strategy           TEXT NOT NULL,
            source_is_shadow   INTEGER NOT NULL DEFAULT 0,
            entry_price        REAL NOT NULL,
            initial_sl_price   REAL NOT NULL,
            tp_price            REAL NOT NULL,
            step_pct           REAL NOT NULL,
            activation_r       REAL,
            activation_label   TEXT NOT NULL,
            current_stop       REAL NOT NULL,
            favorable_extreme  REAL NOT NULL,
            activated          INTEGER NOT NULL DEFAULT 0,
            activation_ts      INTEGER,
            activation_price   REAL,
            ts_last_update     INTEGER NOT NULL,
            status             TEXT NOT NULL DEFAULT 'open',
            ts_close           INTEGER,
            exit_price         REAL,
            outcome            TEXT,
            exit_method        TEXT,
            result_r           REAL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trailing_shadow_open
        ON trailing_shadow_positions(status, strategy)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trailing_shadow_events (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            tracker_id         INTEGER NOT NULL,
            ts                 INTEGER NOT NULL,
            event              TEXT NOT NULL,
            price              REAL NOT NULL,
            stop_before        REAL NOT NULL,
            stop_after         REAL NOT NULL,
            favorable_extreme  REAL NOT NULL,
            activated          INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trailing_shadow_events_tracker
        ON trailing_shadow_events(tracker_id, ts)
        """
    )


def create_tracker(
    connection: sqlite3.Connection,
    *,
    source_demo_id: int,
    ts_open: int,
    symbol: str,
    direction: str,
    alert_type: str,
    source_is_shadow: bool,
    entry_price: float,
    sl_price: float,
    tp_price: float,
) -> bool:
    """Create one independent tracker for a newly persisted source position."""
    if not tracked_strategy(alert_type, ts_open):
        return False
    config = TRAILING_SHADOW_CONFIGS[alert_type]
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO trailing_shadow_positions (
            source_demo_id, ts_open, symbol, direction, strategy,
            source_is_shadow, entry_price, initial_sl_price, tp_price,
            step_pct, activation_r, activation_label, current_stop,
            favorable_extreme, ts_last_update
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_demo_id,
            int(ts_open),
            symbol,
            direction,
            alert_type,
            1 if source_is_shadow else 0,
            float(entry_price),
            float(sl_price),
            float(tp_price),
            float(config["step_pct"]),
            config["activation_r"],
            config["activation_label"],
            float(sl_price),
            float(entry_price),
            int(ts_open),
        ),
    )
    return cursor.rowcount > 0


def load_open_trackers(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, source_demo_id, ts_open, symbol, direction, strategy,
               entry_price, initial_sl_price, tp_price, step_pct,
               activation_r, activation_label, current_stop,
               favorable_extreme, activated, activation_ts, activation_price
          FROM trailing_shadow_positions
         WHERE status='open'
         ORDER BY ts_open, id
        """
    ).fetchall()
    columns = [
        "id", "source_demo_id", "ts_open", "symbol", "direction", "strategy",
        "entry_price", "initial_sl_price", "tp_price", "step_pct",
        "activation_r", "activation_label", "current_stop",
        "favorable_extreme", "activated", "activation_ts", "activation_price",
    ]
    return [dict(zip(columns, row)) for row in rows]


def price_r(direction: str, entry: float, initial_sl: float, price: float) -> float:
    risk = abs(float(initial_sl) - float(entry))
    if risk <= 0:
        return float("nan")
    if direction == "LONG":
        return (float(price) - float(entry)) / risk
    if direction == "SHORT":
        return (float(entry) - float(price)) / risk
    raise ValueError(f"invalid direction: {direction!r}")


def advance_state(state: dict[str, Any], price: float, ts: int) -> dict[str, Any]:
    """Advance one tracker with one observed live price.

    Barriers are tested before the favorable extreme/stop update, so a newly
    activated trailing level takes effect on the next live snapshot.
    """
    direction = str(state["direction"])
    entry = float(state["entry_price"])
    initial_sl = float(state["initial_sl_price"])
    tp = float(state["tp_price"])
    stop_before = float(state["current_stop"])
    favorable_before = float(state["favorable_extreme"])
    activated = bool(state["activated"])
    observed = float(price)
    if not math.isfinite(observed) or observed <= 0:
        return {"changed": False, "event": None}

    hit_tp = (
        observed >= tp if direction == "LONG" else observed <= tp
    )
    hit_stop = (
        observed <= stop_before if direction == "LONG" else observed >= stop_before
    )
    # A scalar live price cannot describe an intratick path, but keep the
    # conservative stop-first rule if both predicates ever become true.
    if hit_stop or hit_tp:
        outcome = "trail_stop" if activated and hit_stop else "sl" if hit_stop else "tp"
        state.update({
            "status": "closed",
            "ts_close": int(ts),
            "exit_price": observed,
            "outcome": outcome,
            "exit_method": "live_price",
            "result_r": price_r(direction, entry, initial_sl, observed),
            "current_stop": stop_before,
            "favorable_extreme": favorable_before,
        })
        return {
            "changed": True,
            "event": outcome,
            "stop_before": stop_before,
            "stop_after": stop_before,
            "favorable_extreme": favorable_before,
        }

    favorable_move_r = price_r(direction, entry, initial_sl, observed)
    activation_r = state.get("activation_r")
    can_activate = (
        favorable_move_r > 0
        if activation_r is None
        else favorable_move_r >= float(activation_r)
    )
    if not can_activate:
        state["ts_last_update"] = int(ts)
        return {"changed": True, "event": None}

    if direction == "LONG":
        favorable_after = max(favorable_before, observed)
        stop_after = max(
            stop_before, favorable_after * (1.0 - float(state["step_pct"]) / 100.0)
        )
    else:
        favorable_after = min(favorable_before, observed)
        stop_after = min(
            stop_before, favorable_after * (1.0 + float(state["step_pct"]) / 100.0)
        )

    first_activation = not activated
    stop_changed = abs(stop_after - stop_before) > 1e-12
    state.update({
        "activated": 1,
        "activation_ts": int(ts) if first_activation else state.get("activation_ts"),
        "activation_price": observed if first_activation else state.get("activation_price"),
        "current_stop": stop_after,
        "favorable_extreme": favorable_after,
        "ts_last_update": int(ts),
    })
    return {
        "changed": True,
        "event": "activated" if first_activation else "trail_update" if stop_changed else None,
        "stop_before": stop_before,
        "stop_after": stop_after,
        "favorable_extreme": favorable_after,
    }


def advance_open_trackers(
    connection: sqlite3.Connection,
    trackers: list[dict[str, Any]],
    prices: dict[str, float],
    ts: int,
) -> int:
    """Advance all open trackers from the same live snapshot as production."""
    resolved = 0
    for original in trackers:
        price = prices.get(str(original["symbol"]))
        if price is None:
            continue
        state = dict(original)
        result = advance_state(state, price, ts)
        if not result["changed"]:
            continue
        connection.execute(
            """
            UPDATE trailing_shadow_positions
               SET current_stop=?, favorable_extreme=?, activated=?,
                   activation_ts=?, activation_price=?, ts_last_update=?,
                   status=?, ts_close=?, exit_price=?, outcome=?,
                   exit_method=?, result_r=?
             WHERE id=? AND status='open'
            """,
            (
                state["current_stop"],
                state["favorable_extreme"],
                1 if state["activated"] else 0,
                state.get("activation_ts"),
                state.get("activation_price"),
                state.get("ts_last_update", int(ts)),
                state.get("status", "open"),
                state.get("ts_close"),
                state.get("exit_price"),
                state.get("outcome"),
                state.get("exit_method"),
                state.get("result_r"),
                int(original["id"]),
            ),
        )
        event = result.get("event")
        if event:
            connection.execute(
                """
                INSERT INTO trailing_shadow_events (
                    tracker_id, ts, event, price, stop_before, stop_after,
                    favorable_extreme, activated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(original["id"]),
                    int(ts),
                    event,
                    float(price),
                    float(result["stop_before"]),
                    float(result["stop_after"]),
                    float(result["favorable_extreme"]),
                    1 if state["activated"] else 0,
                ),
            )
        if state.get("status") == "closed":
            resolved += 1
    return resolved


def _quantile(sorted_values: list[float], q: float) -> float:
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def paired_bootstrap(
    differences: list[float],
    *,
    seed: int,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> tuple[float, float, float]:
    if not differences:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(differences)
    estimates = sorted(
        mean(differences[rng.randrange(n)] for _ in range(n))
        for _ in range(iterations)
    )
    return mean(differences), _quantile(estimates, 0.025), _quantile(estimates, 0.975)


def _fmt_ts(timestamp: int | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_report(
    db_path: str | Path,
    output_dir: str | Path = REPORT_DIR,
) -> dict[str, Any]:
    """Write the rolling forward report from live tracker rows only."""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT t.id, t.source_demo_id, t.ts_open, t.symbol, t.direction,
                   t.strategy, t.source_is_shadow, t.entry_price,
                   t.initial_sl_price, t.tp_price, t.step_pct,
                   t.activation_r, t.activation_label, t.activated,
                   t.activation_ts, t.activation_price, t.status AS shadow_status,
                   t.ts_close AS shadow_ts_close, t.exit_price AS shadow_exit_price,
                   t.outcome AS shadow_outcome, t.exit_method AS shadow_exit_method,
                   t.result_r AS shadow_r,
                   d.status AS source_status, d.ts_close AS source_ts_close,
                   d.exit_price AS source_exit_price
              FROM trailing_shadow_positions t
              JOIN demo_positions d ON d.id=t.source_demo_id
             WHERE t.status != 'open'
               AND t.result_r IS NOT NULL
               AND d.status IN ('tp', 'sl')
               AND d.ts_close IS NOT NULL
               AND d.exit_price IS NOT NULL
             ORDER BY t.ts_open, t.id
            """
        )
    ]
    connection.close()

    forward_rows: list[dict[str, Any]] = []
    for row in rows:
        baseline_r = price_r(
            row["direction"],
            row["entry_price"],
            row["initial_sl_price"],
            row["source_exit_price"],
        )
        shadow_r = float(row["shadow_r"])
        forward_rows.append({
            "tracker_id": row["id"],
            "source_demo_id": row["source_demo_id"],
            "ts_open_utc": _fmt_ts(row["ts_open"]),
            "source_ts_close_utc": _fmt_ts(row["source_ts_close"]),
            "shadow_ts_close_utc": _fmt_ts(row["shadow_ts_close"]),
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "direction": row["direction"],
            "source_is_shadow": row["source_is_shadow"],
            "entry_price": row["entry_price"],
            "initial_sl_price": row["initial_sl_price"],
            "tp_price": row["tp_price"],
            "step_pct": row["step_pct"],
            "activation_label": row["activation_label"],
            "activated": row["activated"],
            "shadow_outcome": row["shadow_outcome"],
            "shadow_exit_method": row["shadow_exit_method"],
            "baseline_outcome": row["source_status"],
            "baseline_r": round(baseline_r, 8),
            "shadow_r": round(shadow_r, 8),
            "delta_r": round(shadow_r - baseline_r, 8),
        })

    summaries: list[dict[str, Any]] = []
    bootstrap_rows: list[dict[str, Any]] = []
    for strategy, config in TRAILING_SHADOW_CONFIGS.items():
        items = [row for row in forward_rows if row["strategy"] == strategy]
        differences = [float(row["delta_r"]) for row in items]
        ready = len(items) >= MIN_FORWARD_PAIRS
        if ready:
            ci_mean, ci_low, ci_high = paired_bootstrap(
                differences, seed=BOOTSTRAP_SEED + (0 if strategy == "overheated_24h" else 1)
            )
            ci_status = "ready"
        else:
            ci_mean = ci_low = ci_high = ""
            ci_status = "insufficient"
        summaries.append({
            "strategy": strategy,
            "step_pct": config["step_pct"],
            "activation": config["activation_label"],
            "n_pairs": len(items),
            "minimum_pairs": MIN_FORWARD_PAIRS,
            "ready_for_bootstrap": ready,
            "baseline_total_r": round(sum(float(row["baseline_r"]) for row in items), 8),
            "shadow_total_r": round(sum(float(row["shadow_r"]) for row in items), 8),
            "delta_total_r": round(sum(differences), 8),
            "baseline_avg_r": round(
                mean(float(row["baseline_r"]) for row in items), 8
            ) if items else "",
            "shadow_avg_r": round(
                mean(float(row["shadow_r"]) for row in items), 8
            ) if items else "",
            "delta_avg_r": round(mean(differences), 8) if differences else "",
            "baseline_tp_n": sum(row["baseline_outcome"] == "tp" for row in items),
            "baseline_sl_n": sum(row["baseline_outcome"] == "sl" for row in items),
            "shadow_tp_n": sum(row["shadow_outcome"] == "tp" for row in items),
            "shadow_trail_stop_n": sum(
                row["shadow_outcome"] == "trail_stop" for row in items
            ),
            "shadow_sl_n": sum(row["shadow_outcome"] == "sl" for row in items),
            "ci_status": ci_status,
            "mean_ci95": (
                f"[{round(ci_low, 8)}, {round(ci_high, 8)}]"
                if ready else ""
            ),
        })
        bootstrap_rows.append({
            "strategy": strategy,
            "step_pct": config["step_pct"],
            "activation": config["activation_label"],
            "n_pairs": len(items),
            "minimum_pairs": MIN_FORWARD_PAIRS,
            "status": ci_status,
            "mean_delta_r": round(ci_mean, 8) if ready else "",
            "mean_ci95_low": round(ci_low, 8) if ready else "",
            "mean_ci95_high": round(ci_high, 8) if ready else "",
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS if ready else "",
            "bootstrap_seed": (
                BOOTSTRAP_SEED + (0 if strategy == "overheated_24h" else 1)
                if ready else ""
            ),
        })

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    generated_utc = datetime.now(timezone.utc).isoformat()
    coverage = {
        "generated_utc": generated_utc,
        "freeze_utc": _fmt_ts(TRAILING_SHADOW_FREEZE_TS),
        "source": "live_price_snapshots_from_check_demo_positions",
        "historical_candles_used": False,
        "rolling_update": True,
        "minimum_forward_pairs_per_strategy": MIN_FORWARD_PAIRS,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "total_resolved_pairs": len(forward_rows),
        "by_strategy": {
            row["strategy"]: {
                "n_pairs": row["n_pairs"],
                "ready_for_bootstrap": row["ready_for_bootstrap"],
            }
            for row in summaries
        },
        "all_strategies_ready": all(
            row["ready_for_bootstrap"] for row in summaries
        ),
    }
    _write_csv(output / "forward_rows.csv", forward_rows)
    _write_csv(output / "forward_summary.csv", summaries)
    _write_csv(output / "paired_bootstrap.csv", bootstrap_rows)
    (output / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Forward trailing shadow report",
        "",
        "**Forward-only rolling report. Production execution was not changed.**",
        "",
        f"Freeze: **{coverage['freeze_utc']}**.",
        "Source path: **the same live price snapshots used by "
        "`check_demo_positions`**, not historical Gate.io 5m candles.",
        "The original `demo_positions` row remains the baseline; shadow rows are "
        "independent and never change its barriers or status.",
        "",
        "## Frozen configurations",
        "",
        "| Strategy | Step | Activation | Pairs | Minimum | Bootstrap | Δ avg R | Mean CI 95% |",
        "|---|---:|---|---:|---:|---|---:|---|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['strategy']} | {row['step_pct']}% | {row['activation']} | "
            f"{row['n_pairs']} | {row['minimum_pairs']} | {row['ci_status']} | "
            f"{row['delta_avg_r']} | {row['mean_ci95']} |"
        )
    lines += [
        "",
        "Bootstrap is recomputed as a rolling update after new resolved pairs. "
        "No step or activation re-selection is performed.",
        "",
        "Before both strategies reach the minimum sample, the report is explicitly "
        "insufficient and no CI is presented. A ready CI is a paired resampling "
        "interval for `shadow_r - baseline_r`; it is not a production approval.",
        "",
        "```json",
        json.dumps(coverage, indent=2, sort_keys=True),
        "```",
    ]
    (output / "forward_analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return coverage


def schedule_report(db_path: str | Path) -> bool:
    """Coalesce report jobs so rolling bootstrap never blocks position polling."""
    global _report_running
    with _report_lock:
        if _report_running:
            return False
        _report_running = True

    def worker() -> None:
        global _report_running
        try:
            generate_report(db_path)
        except Exception:
            # Report generation must never affect position execution, but the
            # failure remains visible for diagnosis.
            logger.exception("trailing shadow report generation failed")
        finally:
            with _report_lock:
                _report_running = False

    threading.Thread(
        target=worker,
        daemon=True,
        name="trailing-shadow-report",
    ).start()
    return True
