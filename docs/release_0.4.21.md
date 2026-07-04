# Stocks Trading Assistant 0.4.21

## Strategy Update

- Adds a `narrow_rally` market state for weak breadth with positive short-term
  returns.
- Widens the live observation pool in `narrow_rally` scans:
  - observation score `>= 83`
  - 5-day range `<= 32`
  - 10-day momentum `<= 26`
  - 20-day position `<= 88`
- Keeps automatic buy execution strict:
  - `buy_min_score >= 90`
  - price must still clear both trigger and buffered VWAP
  - low-score observation rows are marked `WATCH_SCORE_ONLY` and exported with
    `buy_enabled=false`
- Desktop candidate watching respects `buy_enabled=false`, so observation-only
  rows cannot be promoted to `BUY_NOW` by the fast quote watcher.
- Default universe still excludes ChiNext `300/301`.

## Scan Evidence

Diagnostic scan for `2026-07-04` using the liquid mainboard pool:

| Metric | Result |
|---|---:|
| Stock pool | 629 |
| Market state | `narrow_rally` |
| Candidates | 8 |
| BUY-eligible watch rows | 3 |
| Observation-only rows | 5 |
| 300/301 rows | 0 |

Output files:

- `output/trading_assistant/diagnostic_v0.4.21_20260704.md`
- `output/trading_assistant/diagnostic_v0.4.21_20260704.csv`

## Strict Backtest Evidence

The strict execution profile is intentionally unchanged from v0.4.20 for actual
BUY eligibility. The v0.4.21 scan widens observation, not automatic execution.

All runs end on `2026-07-03`, exclude `300/301`, and use 10-minute execution,
100-share lots, 0.01 tick prices, T+1 selling, costs, slippage, and
limit-up/down handling.

| Period | Return | Max drawdown | Closed trades | Profit factor |
|---|---:|---:|---:|---:|
| 1M | 7.7299% | 2.3795% | 16 | 2.8784 |
| 3M | 23.3089% | 2.4482% | 36 | 3.2480 |
| 6M | 40.6729% | 5.0155% | 62 | 2.7212 |
| 9M | 35.3606% | 8.7378% | 93 | 1.9713 |
| 12M | 24.5478% | 15.9201% | 120 | 1.5252 |

Validation checks:

- `bad_300_301 = 0`
- `bad_lots = 0`
- `bad_tick = 0`
- total modeled fees: `11866.89`
