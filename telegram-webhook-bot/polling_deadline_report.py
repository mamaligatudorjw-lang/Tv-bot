#!/usr/bin/env python3
"""Summarize completed bounded polling cycles from bot_debug.log.

Only cycles with both expensive strategy timings and a structured terminal
``Cycle aborted`` record are included.  This intentionally excludes startup
and incomplete cycles from the warmed-cycle sample.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


ENTER_RE = re.compile(
    r"polling_strategy_enter cycle_id=(\S+) strategy=(\S+) entry_ts=([0-9.]+)"
)
TIMING_RE = re.compile(
    r"polling_strategy cycle_id=(\S+) strategy=(\S+) .*?elapsed=([0-9.]+)s"
)
TIMEOUT_RE = re.compile(
    r"polling_strategy_timeout cycle_id=(\S+) strategy=(\S+) "
    r"elapsed=([0-9.]+)s"
)
ABORT_RE = re.compile(
    r"Cycle aborted cycle_id=(\S+) duration=([0-9.]+)s reason=(\S+) "
    r"stage=(\S+) skipped_strategies=(.*)$"
)


def parse_cycles(path: Path) -> list[dict]:
    cycles: dict[str, dict] = {}
    order: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = ENTER_RE.search(line)
            if match:
                cycle_id, strategy, entry_ts = match.groups()
                if cycle_id not in cycles:
                    cycles[cycle_id] = {
                        "cycle_id": cycle_id,
                        "start_ts": float(entry_ts),
                        "strategies": {},
                    }
                    order.append(cycle_id)
                cycles[cycle_id]["strategies"].setdefault(strategy, None)
                continue

            match = TIMING_RE.search(line)
            if match:
                cycle_id, strategy, elapsed = match.groups()
                if cycle_id in cycles:
                    cycles[cycle_id]["strategies"][strategy] = float(elapsed)
                continue

            match = TIMEOUT_RE.search(line)
            if match:
                cycle_id, strategy, elapsed = match.groups()
                if cycle_id in cycles:
                    cycles[cycle_id]["strategies"][strategy] = float(elapsed)
                continue

            match = ABORT_RE.search(line)
            if match:
                cycle_id, duration, reason, stage, skipped = match.groups()
                if cycle_id in cycles:
                    cycles[cycle_id].update(
                        abort=True,
                        duration=float(duration),
                        reason=reason,
                        abort_stage=stage,
                        skipped_strategies=[
                            item for item in skipped.strip().split(",") if item
                        ],
                    )

    # A report row is valid only when the two expensive stages returned and the
    # outer wrapper wrote the terminal abort record.
    rows = [
        cycles[cycle_id]
        for cycle_id in order
        if cycles[cycle_id].get("abort")
        and cycles[cycle_id]["strategies"].get("rsi_and_spikes") is not None
        and cycles[cycle_id]["strategies"].get("overheated_oversold") is not None
    ]
    return rows


def render(rows: list[dict]) -> str:
    tracked = ["breakdown_short", "low_rejection_long", "pump_24h_fade"]
    lines = [
        "# Polling deadline report",
        "",
        f"Completed warmed cycles: **{len(rows)}**",
        "",
        "| cycle_id | duration (s) | RSI (s) | overheated (s) | stage | skipped tracked strategies |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        skipped = set(row["skipped_strategies"])
        tracked_skipped = ", ".join(
            name for name in tracked if name in skipped
        ) or "none"
        lines.append(
            f"| {row['cycle_id']} | {row['duration']:.1f} | "
            f"{row['strategies']['rsi_and_spikes']:.1f} | "
            f"{row['strategies']['overheated_oversold']:.1f} | "
            f"{row['abort_stage']} | {tracked_skipped} |"
        )

    counts = Counter(
        name
        for row in rows
        for name in tracked
        if name in row["skipped_strategies"]
    )
    lines.extend(
        [
            "",
            "## Classification",
            "",
            "| Strategy | skipped cycles | classification |",
            "|---|---:|---|",
        ]
    )
    for name in tracked:
        count = counts[name]
        if not rows:
            classification = "no sample"
        elif count == len(rows):
            classification = "systematic (every cycle)"
        elif count >= len(rows) * 0.8:
            classification = "near-systematic"
        else:
            classification = "intermittent"
        lines.append(f"| `{name}` | {count}/{len(rows)} | {classification} |")

    lines.extend(
        [
            "",
            "The `overheated_early` and `overheated_24h` branches are nested "
            "inside `overheated_oversold`; no independent elapsed is claimed "
            "for those children.",
            "",
            "A 240-second ceiling is an operational limit, not a measured "
            "target duration. Snapshot freshness should be evaluated against "
            "the time at which each strategy actually starts.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="bot_debug.log")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--out", default="polling_deadline_report")
    args = parser.parse_args()

    rows = parse_cycles(Path(args.log))[-args.limit :]
    out = Path(args.out)
    out.with_suffix(".json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    out.with_suffix(".md").write_text(render(rows), encoding="utf-8")
    print(f"Wrote {out.with_suffix('.md')} and {out.with_suffix('.json')}")


if __name__ == "__main__":
    main()