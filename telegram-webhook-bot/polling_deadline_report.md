# Polling deadline report

Completed warmed cycles: **15**

| cycle_id | duration (s) | RSI (s) | overheated (s) | stage | skipped tracked strategies |
|---|---:|---:|---:|---|---|
| 3939-1787569149216 | 240.0 | 65.4 | 171.3 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787569449219 | 240.0 | 63.8 | 173.1 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787569749216 | 240.0 | 66.9 | 169.8 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787570049216 | 240.0 | 64.3 | 172.6 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787570349217 | 240.0 | 64.7 | 172.2 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787570649218 | 240.0 | 64.3 | 172.6 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787570949216 | 240.0 | 64.0 | 172.9 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787571249219 | 240.0 | 64.0 | 172.9 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787571549217 | 240.0 | 64.5 | 172.3 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787571849217 | 240.0 | 64.8 | 172.1 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 3939-1787572149217 | 240.0 | 64.7 | 172.2 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 5435-1787573179311 | 240.0 | 63.5 | 173.3 | overheated_oversold | breakdown_short, low_rejection_long, pump_24h_fade |
| 5664-1787573929775 | 240.0 | 64.4 | 25.1 | vwap_reversion | low_rejection_long |
| 75-1787574746184 | 240.0 | 64.6 | 21.8 | bollinger_squeeze | low_rejection_long |
| 329-1787575311403 | 240.0 | 65.1 | 19.0 | range_breakout_long | none |

## Classification

| Strategy | skipped cycles | classification |
|---|---:|---|
| `breakdown_short` | 12/15 | near-systematic |
| `low_rejection_long` | 14/15 | near-systematic |
| `pump_24h_fade` | 12/15 | near-systematic |

The `overheated_early` and `overheated_24h` branches are nested inside `overheated_oversold`; no independent elapsed is claimed for those children.

A 240-second ceiling is an operational limit, not a measured target duration. Snapshot freshness should be evaluated against the time at which each strategy actually starts.
