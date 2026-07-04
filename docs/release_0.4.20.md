# Stocks Trading Assistant 0.4.20

## Strategy Update

- Default live universe remains the liquid mainboard-style pool:
  `000`, `001`, `002`, `003`, `600`, `601`, `603`, `605`.
- New buy triggers require price above both the signal trigger and buffered VWAP.
- Hot market state skips new entries.
- Strict backtests use realistic execution constraints: 100-share lots, 0.01
  tick prices, T+1 selling, transaction costs, slippage, and limit-up/down
  handling.
- Optimized strict profile:
  - `max_positions=3`
  - `hot_capital_factor=0`
  - `normal_capital_factor=1`
  - `cold_capital_factor=0.75`
  - `vwap_buffer=0.003`
  - `stop_atr_mult=0.45`
  - `stop_min=0.02`
  - `vwap_fail_bars=1`

## Backtest Evidence

All runs end on `2026-07-03`, exclude `300` symbols, and keep the strict
execution constraints above.

| Period | Return | Max drawdown | Closed trades | Profit factor |
|---|---:|---:|---:|---:|
| 1M | 7.7299% | 2.3795% | 16 | 2.8784 |
| 3M | 23.3089% | 2.4482% | 36 | 3.2480 |
| 6M | 40.6729% | 5.0155% | 62 | 2.7212 |
| 9M | 35.3606% | 8.7378% | 93 | 1.9713 |
| 12M | 24.5478% | 15.9201% | 120 | 1.5252 |

Event scores remain disabled in the strict release profile because the current
event files are not a complete daily historical snapshot set. Enabling them in
the historical test without daily point-in-time files would introduce lookahead
risk.
