---
name: Streak signal guards
description: Two guards added to streak LONG/SHORT to prevent firing on exhausted or already-reversing moves.
---

# Streak signal guards

## Constants (app.py near STREAK_1H_* block)
- `STREAK_1H_LONG_MAX_GAIN = 15.0` — skip LONG if coin already gained >15% during the streak window
- `STREAK_1H_SHORT_MAX_LOSS = 15.0` — skip SHORT if coin already lost >15% during the streak window
- `STREAK_1H_REVERSAL_PCT = 0.5` — skip if live price is already >0.5% against the last closed candle

## Why
CATIUSDT case: bot sent LONG on +18.6% 6h streak, but live price was already falling. The streak check used only completed candles; the current (live) candle was already red.

Two failure modes prevented:
1. **Exhaustion**: coin pumped too much already (>15%) → late entry, catches the tail
2. **Reversal**: current price already below last close → trend ended before signal fired
