# Polling deadline report

Completed warmed cycles: **10**

| cycle_id | duration (s) | RSI (s) | overheated (s) | stage | skipped tracked strategies |
|---|---:|---:|---:|---|---|
| 996-1787559094938 | 240.0 | 64.6 | 171.4 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 996-1787559394938 | 240.0 | 64.9 | 172.0 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 996-1787559694941 | 240.0 | 65.5 | 171.3 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 996-1787559994938 | 240.0 | 65.6 | 171.2 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 996-1787560294938 | 240.0 | 65.7 | 171.0 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 996-1787560594938 | 240.0 | 64.9 | 172.0 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 996-1787560894939 | 240.0 | 64.8 | 171.9 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 2349-1787561600471 | 240.0 | 65.3 | 171.6 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 2349-1787561900471 | 240.0 | 65.1 | 171.8 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 2349-1787562200471 | 240.0 | 64.1 | 172.5 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |

## Classification

| Strategy | skipped cycles | classification |
|---|---:|---|
| `breakdown_short` | 10/10 | systematic (every cycle) |
| `low_rejection_long` | 10/10 | systematic (every cycle) |
| `pump_24h_fade` | 10/10 | systematic (every cycle) |

The `overheated_early` and `overheated_24h` branches are nested inside `overheated_oversold`; no independent elapsed is claimed for those children.

A 240-second ceiling is an operational limit, not a measured target duration. Snapshot freshness should be evaluated against the time at which each strategy actually starts.
