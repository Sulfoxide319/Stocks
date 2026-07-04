# Stocks Trading Assistant 0.4.38

## Normal-Market Trailing Winner Preservation

- Adds market-state-specific trailing ATR overrides:
  - `normal`: `--normal-trail-atr-mult 0.34`
  - `cold`: unchanged, falls back to global `--trail-atr-mult 0.25`
  - `narrow_rally`: unchanged, falls back to global `--trail-atr-mult 0.25`
- The strict 10-minute ledger records `dynamic_trail_atr_mult` and `dynamic_trailing_stop_pct` for auditability.
- The live monitor uses the same state-specific trailing multiplier, so desktop sell-management prompts stay aligned with strict backtests.
- Hard stop, VWAP failure, target upper, first management line, T+1, lot size, tick size, and mainboard filters remain unchanged.

## Root Cause

The v0.4.37 sell-path audit showed that target upper sellable hits remain rare, while trailing exits are the main profit engine. A global wider trailing stop improved longer windows but slightly hurt recent `1M/3M/6M` because narrow-rally trades should not receive extra room.

The profitable conflict was narrower: normal-market trend winners were sometimes stopped by trailing before reaching the target upper. Applying a wider trailing ATR only in `normal` markets preserved those winners without changing cold-market loss controls or narrow-rally behavior.

## Validation

Strict 10-minute default regression:

```powershell
python tools\backtest_strict_10m_ledger.py --end-date 2026-07-03 --period-months 1,3,6,9,12 --out-dir output\backtest_strict_10m_v0438_candidate
```

Compared with v0.4.37:

| Period | v0.4.37 Return | v0.4.38 Return | Delta | v0.4.37 DD | v0.4.38 DD | Trades | v0.4.37 PF | v0.4.38 PF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1M | 13.7512% | 13.7512% | +0.0000 pct | 3.5834% | 3.5834% | 14 | 4.9490 | 4.9490 |
| 3M | 41.5900% | 41.5900% | +0.0000 pct | 3.7516% | 3.7516% | 29 | 6.9755 | 6.9755 |
| 6M | 64.9549% | 64.9549% | +0.0000 pct | 3.9371% | 3.9371% | 49 | 4.8789 | 4.8789 |
| 9M | 69.4801% | 70.9137% | +1.4336 pct | 5.2000% | 5.2000% | 68 | 3.8083 | 3.8191 |
| 12M | 68.4631% | 70.4732% | +2.0101 pct | 7.2032% | 7.2032% | 82 | 3.0923 | 3.1338 |

Guardrails:

- `bad_300_301=0`
- `bad_lots=0`
- `bad_tick=0`
- 12M trades remain `82`, above the 80% minimum.

Smoke checks:

```powershell
python -B -m py_compile tools\backtest_strict_10m_ledger.py short_term_live_monitor.py local_trading_assistant.py desktop_app.py trading_assistant_app.py
```

Rolling v0.4.38 vs v0.4.37 proxy:

| End Date | Period | v0.4.37 Return | v0.4.38 Return | Delta | v0.4.37 DD | v0.4.38 DD | Trades |
|---|---|---:|---:|---:|---:|---:|---:|
| 2026-05-29 | 1M | 5.5043% | 5.5043% | +0.0000 pct | 0.7775% | 0.7775% | 3 |
| 2026-05-29 | 3M | 36.6584% | 36.6584% | +0.0000 pct | 3.9614% | 3.9614% | 24 |
| 2026-05-29 | 6M | 51.3161% | 52.6845% | +1.3684 pct | 3.8778% | 3.9115% | 39 |
| 2026-06-30 | 1M | 18.1776% | 18.1776% | +0.0000 pct | 3.7837% | 3.7837% | 16 |
| 2026-06-30 | 3M | 42.8623% | 42.8623% | +0.0000 pct | 3.7516% | 3.7516% | 28 |
| 2026-06-30 | 6M | 65.8083% | 65.8083% | +0.0000 pct | 3.9391% | 3.9391% | 49 |
| 2026-07-03 | 1M | 13.7512% | 13.7512% | +0.0000 pct | 3.5834% | 3.5834% | 14 |
| 2026-07-03 | 3M | 41.5900% | 41.5900% | +0.0000 pct | 3.7516% | 3.7516% | 29 |
| 2026-07-03 | 6M | 64.9549% | 64.9549% | +0.0000 pct | 3.9370% | 3.9370% | 49 |

Rolling summary: return non-lower `9/9`; drawdown non-higher `8/9`. The only drawdown increase is `+0.0337 pct` in the `2026-05-29 6M` window, where return improves by `+1.3684 pct` because `600118` reaches target upper instead of exiting earlier by trailing stop.
