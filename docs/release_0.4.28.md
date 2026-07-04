# Stocks Trading Assistant 0.4.28

## Condition Diagnostics

- Strict 10-minute backtests now write condition diagnostics:
  - `*_condition_diagnostics.csv`
  - `*_condition_diagnostics.md`
- The diagnostics bucket closed trades by:
  - score
  - market state
  - setup type
  - entry time
  - entry gap
  - entry VWAP distance
  - traded value ratio
  - ATR
  - 5-day range
  - 10-day momentum
  - 20-day close position
  - sector momentum
  - sector breadth
  - exit reason
- Each bucket reports win rate, average return, median return, realized PnL, profit factor, target-upper hit rate, first-management-line hit rate, hard-stop rate, VWAP-fail rate, and lift versus same-period baseline.

## Strategy Update

- Raises high-gap volume confirmation from `1.3x` to `1.5x`.
- Adds a cold-market quality floor: `10-day momentum >= 5%`.
- Desktop live scanning now uses the same defaults as the strict 10-minute validation.
- Rejected more aggressive cold-market filters because they improved longer windows but failed the 1M gate.

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.27 strict 10-minute execution.

| Period | v0.4.27 Return | v0.4.28 Return | Delta | v0.4.27 Max DD | v0.4.28 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 8.9629% | 10.0313% | +1.0684 pct | 2.7847% | 2.3046% | 15 |
| 3M | 25.9236% | 28.8205% | +2.8969 pct | 2.8172% | 2.7937% | 33 |
| 6M | 43.1870% | 46.8044% | +3.6174 pct | 5.2669% | 5.2669% | 57 |
| 9M | 43.0435% | 46.0833% | +3.0398 pct | 6.5852% | 6.5852% | 79 |
| 12M | 37.2589% | 41.6642% | +4.4053 pct | 9.0827% | 8.2010% | 95 |

Acceptance checks:

- `1M/3M/6M/9M/12M return >= v0.4.27`: passed.
- `12M return > 37.2589%`: passed (`41.6642%`).
- `12M max_drawdown <= 9.0827%`: passed (`8.2010%`).
- `12M trades >= 80% of v0.4.27`: passed (`95 >= 78.4`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.
