---
name: UNIFIED_START_TS and parallel-bot history
description: Two bots ran in parallel (dev-workflow + Reserved VM) until the first clean Publish. All data before UNIFIED_START_TS is from that mixed period.
---

# UNIFIED_START_TS — parallel-bot history boundary

## The rule
`UNIFIED_START_TS` (line ~204 in `app.py`, constants block) is the Unix timestamp of the
first Publish after the dual-process period was discovered (2026-08-15).

Set it to 0 until the Publish happens. After Publish: update to the actual publish timestamp.
Both `/demo` and `/scorestats` show a warning when `UNIFIED_START_TS == 0` or when the
query window includes data before it.

**Why:** Dev-workflow and Reserved VM ran independently since at least 2026-08-13, with:
- Different code versions (VM ran old buggy code including the `check_demo_positions ValueError`)
- Separate SQLite databases (`alerts.db` on VM ≠ dev workspace `alerts.db`)
- Both sending signals to the same Telegram chat → duplicate signals (confirmed: ACEUSDT $0.2415 vs $0.2413)
- All cooldowns resetting every ~58 min (dev restarts) → 22.7% of real closed positions (107/471) were re-entries within 4h; 77 within 15min

## What's invalid before UNIFIED_START_TS
- `OVERSOLD_DURATION_FILTER_TS`, `CONFLUENCE_CAP_CUTOFF_TS`, `MONITOR_FIX_SINCE`,
  `OVERSOLD_SL_CAP_SINCE` — these mark deploys to dev-workflow only. VM ran different
  code at those same moments. Before/after splits using these are meaningless on VM data.
- Win-rate stats by signal type are inflated by duplicate entries (TUTUSDT: 27 positions total)

## DB situation on Publish
- Replit docs confirm: filesystem on Reserved VM is **reset on every Publish**
- Dev workspace `alerts.db` becomes VM's `alerts.db` after Publish (dev data wins)
- Backup saved: `telegram-webhook-bot/alerts_backup_2026-08-15.db`

**How to apply:** After any Publish, immediately update `UNIFIED_START_TS` to current Unix time.
In all WR/scorestats analysis, filter `ts_open >= UNIFIED_START_TS`.
