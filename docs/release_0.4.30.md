# Stocks Trading Assistant 0.4.30

## Hard-Stop Floor Tuning

- Lowers the strict dynamic hard-stop floor from `2.0%` to `1.5%`.
- Aligns live monitor hard-stop display with the strict backtest dynamic-exit parameters:
  - target: ATR/range dynamic upper, capped at `18%`
  - hard stop: `max(1.5%, 0.45 * ATR%)`, capped at `7%`
  - trailing stop: `max(2.5%, 0.25 * ATR%)`, capped at `6%`
- More aggressive alternatives were rejected:
  - broad early-entry filters improved 12M in some cases but failed 1M/3M gates
  - lower ATR stop multipliers hurt short-window returns
  - higher stop floors increased drawdown or reduced short-window return

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.29 strict 10-minute execution.

| Period | v0.4.29 Return | v0.4.30 Return | Delta | v0.4.29 Max DD | v0.4.30 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 10.2311% | 10.2311% | +0.0000 pct | 2.3046% | 2.3046% | 15 |
| 3M | 29.0603% | 29.1512% | +0.0909 pct | 2.7937% | 2.7915% | 33 |
| 6M | 47.6551% | 48.1821% | +0.5270 pct | 5.2225% | 5.1574% | 57 |
| 9M | 46.4777% | 47.2864% | +0.8087 pct | 6.5852% | 6.5300% | 79 |
| 12M | 42.2628% | 42.5919% | +0.3291 pct | 8.2010% | 8.0107% | 95 |

Acceptance checks:

- `1M/3M/6M/9M/12M return >= v0.4.29`: passed.
- `12M return > 42.2628%`: passed (`42.5919%`).
- `12M max_drawdown <= 8.2010%`: passed (`8.0107%`).
- `12M trades >= 80% of v0.4.29`: passed (`95 >= 76`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.
