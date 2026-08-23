#!/usr/bin/env python3
"""Summarize snapshot-to-delivery price measurements from alerts.db."""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("alerts.db"))
    parser.add_argument("--since", type=float, default=0.0, help="Unix timestamp lower bound")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        rows = conn.execute(
            """
            SELECT snapshot_age_sec, snapshot_gap_pct
              FROM alerts
             WHERE ts >= ?
               AND snapshot_ts IS NOT NULL
               AND delivery_ts IS NOT NULL
            """,
            (args.since,),
        ).fetchall()
    finally:
        conn.close()

    ages = [float(age) for age, _ in rows if age is not None and math.isfinite(float(age))]
    gaps = [float(gap) for _, gap in rows if gap is not None and math.isfinite(float(gap))]
    unavailable = len(rows) - len(gaps)
    print(f"rows_with_snapshot_context={len(rows)}")
    print(f"measured_gap_rows={len(gaps)}")
    print(f"unavailable_gap_rows={unavailable}")
    if not gaps:
        print("No measured snapshot-to-delivery gaps yet.")
        return
    print(f"gap_pct_signed_median={statistics.median(gaps):.6f}")
    print(f"gap_pct_signed_p90={percentile(gaps, 0.90):.6f}")
    print(f"gap_pct_signed_p95={percentile(gaps, 0.95):.6f}")
    print(f"gap_pct_signed_min={min(gaps):.6f}")
    print(f"gap_pct_signed_max={max(gaps):.6f}")
    absolute = [abs(gap) for gap in gaps]
    print(f"gap_pct_absolute_median={statistics.median(absolute):.6f}")
    print(f"gap_pct_absolute_p95={percentile(absolute, 0.95):.6f}")
    if ages:
        print(f"snapshot_age_sec_median={statistics.median(ages):.3f}")
        print(f"snapshot_age_sec_p95={percentile(ages, 0.95):.3f}")
        print(f"snapshot_age_sec_max={max(ages):.3f}")


if __name__ == "__main__":
    main()