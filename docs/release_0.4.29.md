# Stocks Trading Assistant 0.4.29

## Exit Confirmation Alignment

- Aligns strict 10-minute VWAP-fail exits with the live desktop sell rule.
- The strict backtest now uses `vwap_fail_buffer = 0.0` by default:
  - previous strict model: exit after `close < VWAP * 0.999` and below cost
  - aligned model: exit after `close < VWAP` and below cost
- This avoids an extra 0.1% early-exit buffer that was not present in live holding advice.
- More aggressive alternatives were rejected:
  - requiring 2 or 3 VWAP-fail bars reduced short-window returns
  - delaying entry to 10:00 improved some longer windows but failed 1M/3M gates

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.28 strict 10-minute execution.

| Period | v0.4.28 Return | v0.4.29 Return | Delta | v0.4.28 Max DD | v0.4.29 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 10.0313% | 10.2311% | +0.1998 pct | 2.3046% | 2.3046% | 15 |
| 3M | 28.8205% | 29.0603% | +0.2398 pct | 2.7937% | 2.7937% | 33 |
| 6M | 46.8044% | 47.6551% | +0.8507 pct | 5.2669% | 5.2225% | 57 |
| 9M | 46.0833% | 46.4777% | +0.3944 pct | 6.5852% | 6.5852% | 79 |
| 12M | 41.6642% | 42.2628% | +0.5986 pct | 8.2010% | 8.2010% | 95 |

Acceptance checks:

- `1M/3M/6M/9M/12M return >= v0.4.28`: passed.
- `12M return > 41.6642%`: passed (`42.2628%`).
- `12M max_drawdown <= 8.2010%`: passed (`8.2010%`).
- `12M trades >= 80% of v0.4.28`: passed (`95 >= 76`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.
