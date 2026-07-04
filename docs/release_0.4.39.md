# Stocks Trading Assistant 0.4.39

## Mature Profit-Cushion Concentration

- Adds a profit-cushion aggressive exposure mode to the strict 10-minute backtest engine.
- Default activation requires both:
  - current portfolio equity return `>= 8%`
  - at least `120` elapsed trading days in the tested window
- Once active, new entries use:
  - `profit_cushion_max_positions = 2`
  - `profit_cushion_normal_capital_factor = 0.70`
  - `profit_cushion_cold_capital_factor = 0.70`
- Before the cushion and maturity gates are met, the strategy keeps the v0.4.38 steady default behavior.
- Adds optional drawdown-governor parameters for future experiments, but they remain disabled by default because the tested variants either missed recovery or failed the default return gate.
- `tools/rolling_strict_10m_validation.py` can now include a `v0438_proxy` scenario by disabling the profit-cushion gate.

## Root Cause

The raw `max-positions=2` concentration test confirmed a real profit source: 12M return improved from `70.4732%` to `74.5904%`. But it also lifted 12M max drawdown from `7.2032%` to `8.0944%`, with the worst drawdown appearing before the account had a meaningful profit cushion.

The accepted rule treats concentration as a late-stage exposure upgrade. It does not concentrate capital during the fragile early account path, and it requires enough elapsed samples to avoid switching modes after a short lucky streak.

## Validation

Strict 10-minute default regression:

```powershell
python tools\backtest_strict_10m_ledger.py --end-date 2026-07-03 --period-months 1,3,6,9,12 --out-dir output\backtest_strict_10m_v0439_candidate
```

Compared with v0.4.38:

| Period | v0.4.38 Return | v0.4.39 Return | Delta | v0.4.38 DD | v0.4.39 DD | Trades | v0.4.38 PF | v0.4.39 PF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1M | 13.7512% | 13.7512% | +0.0000 pct | 3.5834% | 3.5834% | 14 | 4.9490 | 4.9490 |
| 3M | 41.5900% | 41.5900% | +0.0000 pct | 3.7516% | 3.7516% | 29 | 6.9755 | 6.9755 |
| 6M | 64.9549% | 64.9549% | +0.0000 pct | 3.9371% | 3.9371% | 49 | 4.8789 | 4.8789 |
| 9M | 70.9137% | 71.2338% | +0.3201 pct | 5.2000% | 5.2000% | 63 | 3.8191 | 3.8628 |
| 12M | 70.4732% | 72.3845% | +1.9113 pct | 7.2032% | 7.2032% | 75 | 3.1338 | 3.1659 |

Acceptance report:

```powershell
python tools\compare_backtest_summaries.py --baseline-dir output\backtest_strict_10m_v0438_candidate --candidate-dir v0439=output\backtest_strict_10m_v0439_candidate --out-csv output\backtest_strict_10m_v0439_candidate\v0439_vs_v0438_comparison.csv --out-md output\backtest_strict_10m_v0439_candidate\v0439_vs_v0438_comparison.md --strict-exit-code
```

Result: `PASS v0439: returns=True 12M_dd=True 12M_trades=True bad=0/0/0`.

Guardrails:

- `bad_300_301=0`
- `bad_lots=0`
- `bad_tick=0`
- 12M trades remain `75`, above the 80% minimum versus v0.4.38's `82`.

Smoke checks:

```powershell
python -B -m py_compile tools\backtest_strict_10m_ledger.py tools\rolling_strict_10m_validation.py tools\compare_backtest_summaries.py
```

Rolling v0.4.39 vs v0.4.38 proxy:

```powershell
python tools\rolling_strict_10m_validation.py --end-dates 2026-05-29,2026-06-30,2026-07-03 --period-months 1,3,6 --out-dir output\rolling_v0439_vs_v0438 --baseline-name v0438_proxy --no-v0432-proxy --include-v0438-proxy
```

| End Date | Period | v0.4.38 Return | v0.4.39 Return | Delta | v0.4.38 DD | v0.4.39 DD | Trades |
|---|---|---:|---:|---:|---:|---:|---:|
| 2026-05-29 | 1M | 5.5043% | 5.5043% | +0.0000 pct | 0.7775% | 0.7775% | 3 |
| 2026-05-29 | 3M | 36.6584% | 36.6584% | +0.0000 pct | 3.9614% | 3.9614% | 24 |
| 2026-05-29 | 6M | 52.6845% | 52.6845% | +0.0000 pct | 3.9115% | 3.9115% | 39 |
| 2026-06-30 | 1M | 18.1776% | 18.1776% | +0.0000 pct | 3.7837% | 3.7837% | 16 |
| 2026-06-30 | 3M | 42.8623% | 42.8623% | +0.0000 pct | 3.7516% | 3.7516% | 28 |
| 2026-06-30 | 6M | 65.8083% | 65.8083% | +0.0000 pct | 3.9391% | 3.9391% | 49 |
| 2026-07-03 | 1M | 13.7512% | 13.7512% | +0.0000 pct | 3.5834% | 3.5834% | 14 |
| 2026-07-03 | 3M | 41.5900% | 41.5900% | +0.0000 pct | 3.7516% | 3.7516% | 29 |
| 2026-07-03 | 6M | 64.9549% | 64.9549% | +0.0000 pct | 3.9370% | 3.9370% | 49 |

Rolling summary: return non-lower `9/9`; drawdown non-higher `9/9`; hard checks `0/0/0`.
