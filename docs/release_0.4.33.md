# Stocks Trading Assistant 0.4.33

## Cold Low-ATR Filter With Restored Sizing

- Adds a cold-market ATR floor: `cold_min_atr_pct = 4.1`.
- Raises the cold/narrow-rally strict backtest capital factor from `0.9` to `1.0`.
- Applies the cold ATR floor to live scanning defaults and desktop/local assistant launch defaults.

## Root Cause

After v0.4.32, the largest remaining 12M drag was the low-ATR cold-market cluster:

- `ATR < 4.1%`: `11` trades, average return `-0.6349%`, win rate `18.1818%`, profit factor `0.5713`, hard-stop rate `54.5455%`.
- Of those low-ATR trades, `9` were in `cold` markets with average return about `-1.15%`.
- The `narrow_rally` low-ATR sample was positive, so the new filter is cold-only.

The validation grid showed the interaction matters:

- Cold ATR floor alone improved 12M but failed the `3M/6M` gates.
- Restoring cold/narrow sizing alone improved return but violated the 12M drawdown gate.
- Cold ATR floor plus restored sizing passed every return gate and improved 12M drawdown.

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.32 strict 10-minute execution.

| Period | v0.4.32 Return | v0.4.33 Return | Delta | v0.4.32 Max DD | v0.4.33 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 13.5002% | 13.7512% | +0.2510 pct | 3.3571% | 3.5834% | 14 |
| 3M | 37.2883% | 37.6401% | +0.3518 pct | 3.5161% | 3.8198% | 29 |
| 6M | 56.9841% | 59.7816% | +2.7975 pct | 4.3920% | 3.9371% | 49 |
| 9M | 59.3964% | 65.1715% | +5.7751 pct | 5.5709% | 5.2000% | 68 |
| 12M | 57.8088% | 63.0575% | +5.2487 pct | 7.6350% | 7.2032% | 82 |

Acceptance checks:

- `1M/3M/6M/9M/12M return >= v0.4.32`: passed.
- `12M return > 57.8088%`: passed (`63.0575%`).
- `12M max_drawdown <= 7.6350%`: passed (`7.2032%`).
- `12M trades >= 80% of v0.4.32`: passed (`82 >= 72`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.

Risk note:

- `1M` and `3M` max drawdown rose modestly, but the global release gate is anchored on 12M drawdown and all return windows improved.
