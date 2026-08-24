# Polling deadline report

Completed warmed cycles: **1**

| cycle_id | duration (s) | RSI (s) | overheated (s) | stage | skipped tracked strategies |
|---|---:|---:|---:|---|---|
| 996-1787559094938 | 240.0 | 64.6 | 171.4 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |

## Classification

| Strategy | skipped cycles | classification |
|---|---:|---|
| `breakdown_short` | 1/1 | systematic (every cycle) |
| `low_rejection_long` | 1/1 | systematic (every cycle) |
| `pump_24h_fade` | 1/1 | systematic (every cycle) |

The `overheated_early` and `overheated_24h` branches are nested inside `overheated_oversold`; no independent elapsed is claimed for those children.

A 240-second ceiling is an operational limit, not a measured target duration. Snapshot freshness should be evaluated against the time at which each strategy actually starts.
