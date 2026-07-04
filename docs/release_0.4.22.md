# Stocks Trading Assistant 0.4.22

## Strategy Update

- Adds a market-state entry clock:
  - normal market new entries stop at `10:40`
  - cold and narrow-rally scans keep the broader `11:20` observation window
- Raises cold/narrow-rally strict backtest sizing from `0.75` to `0.90`.
- Keeps the buyable universe on liquid mainboard prefixes only; ChiNext `300/301`
  remains excluded.
- Keeps hot-market behavior defensive: no new hot-market entries.

## Root-Cause Finding

The 12M ledger showed that normal-market entries after `10:40` were a persistent
loss cluster. These trades had delayed VWAP/trigger confirmation, then exited
mostly by hard stop or VWAP failure. Cutting only this late normal-market window
improved 6M/9M/12M without changing 1M/3M.

Cold-market low-score open filtering looked attractive for 9M/12M, but it hurt
1M/3M/6M because recent cold and narrow-rally rebounds were profitable. The
release therefore adjusts sizing to `0.90` instead of adding a cold-market hard
filter.

## Strict Backtest Evidence

All runs end on `2026-07-03`, exclude `300/301`, and use 10-minute execution,
100-share lots, 0.01 tick prices, T+1 selling, costs, slippage, and
limit-up/down handling.

| Period | v0.4.21 return | v0.4.22 return | Improvement | Max drawdown |
|---|---:|---:|---:|---:|
| 1M | 7.7299% | 8.9629% | +1.2330 pct | 2.3795% -> 2.7847% |
| 3M | 23.3089% | 24.6547% | +1.3458 pct | 2.4482% -> 2.8193% |
| 6M | 40.6729% | 42.6556% | +1.9827 pct | 5.0155% -> 5.4281% |
| 9M | 35.3606% | 40.7848% | +5.4242 pct | 8.7378% -> 7.8990% |
| 12M | 24.5478% | 31.2809% | +6.7331 pct | 15.9201% -> 12.3834% |

Validation checks:

- `bad_300_301 = 0`
- `bad_lots = 0`
- `bad_tick = 0`

