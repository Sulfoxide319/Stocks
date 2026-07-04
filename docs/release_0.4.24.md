# Stocks Trading Assistant 0.4.24

## Strategy Update

- Adds a normal-market ATR quality floor (`normal_min_atr_pct=4.1`) to avoid low-volatility noise trades that diluted 9M/12M performance.
- Keeps the first-management-line framework in the strict 10-minute backtester:
  - `first_manage_pct = max(4%, 0.4 * target_upper_pct)`.
  - Optional `--partial-take-profit` records `PARTIAL_SELL` rows without double-counting closed trades.
- Leaves partial take-profit disabled by default because the full-window validation underperformed the default trend-holding variant at 1M and 6M.
- Adds signed official event scoring from low-noise sources only (`CNINFO`, `SEC`, exchange/company IR/RSS-style sources). Social signals are excluded from this factor.

## Reporting/UI

- Live monitor CSV/Markdown now includes first management line, market state, official event score, target-upper hit rate, and first-management hit rate.
- Desktop and local reports show `目标上沿`, `第一管理线`, and `历史命中` separately.
- Strict backtest now writes a distribution report with target-upper hit rate, first-management hit rate, partial PnL contribution, remaining-position PnL contribution, and breakdowns by market state, month, exit reason, and return bucket.

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.22 strict 10-minute execution.

| Period | v0.4.22 Return | v0.4.24 Return | Delta | v0.4.22 Max DD | v0.4.24 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 8.9629% | 8.9629% | +0.0000 pct | 2.7847% | 2.7847% | 16 |
| 3M | 24.6547% | 25.9236% | +1.2689 pct | 2.8193% | 2.8172% | 34 |
| 6M | 42.6556% | 43.1870% | +0.5314 pct | 5.4281% | 5.2669% | 58 |
| 9M | 40.7848% | 43.0435% | +2.2587 pct | 7.8990% | 6.5852% | 80 |
| 12M | 31.2809% | 37.2589% | +5.9780 pct | 12.3834% | 9.0827% | 98 |

Acceptance checks:

- `12M return > 31.2809%`: passed (`37.2589%`).
- `12M max_drawdown <= 12.3834%`: passed (`9.0827%`).
- `1M/3M/6M return >= v0.4.22`: passed.
- Trade count >= 80% of v0.4.22 (`113 * 0.8 = 90.4`): passed (`98`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.

## Partial Take-Profit Check

With `--partial-take-profit` enabled, full-window validation produced:

| Period | Return | Max DD | Partial Sells |
|---|---:|---:|---:|
| 1M | 8.1514% | 2.9462% | 7 |
| 3M | 26.9642% | 3.2514% | 19 |
| 6M | 38.9446% | 5.0655% | 27 |
| 9M | 37.9735% | 6.6955% | 31 |
| 12M | 32.2523% | 9.3131% | 33 |

This confirms the mechanism works, but defaulting it on would violate the 1M/6M baseline requirement and reduce the default 12M return. It remains available as an explicit research scenario.
