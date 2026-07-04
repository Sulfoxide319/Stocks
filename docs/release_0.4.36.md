# Stocks Trading Assistant 0.4.36

## Sell Path Audit and Hit-Rate Calibration

- Adds strict 10-minute sell-path audit outputs:
  - target upper touched at any time after entry
  - target upper hit on a T+1 sellable bar
  - first management line touched and sellable hit
  - trailing-stop activation and trigger details
  - hard-stop, VWAP-fail, runup, drawdown, and target-gap-at-exit fields
- Adds `config/hit_rate_calibration.default.json` from the validated 12M strict backtest ending `2026-07-03`.
- Live and desktop scan outputs now display:
  - sellable target-upper hit rate
  - target-upper touch rate
  - first-management-line hit rate
  - calibration sample size
- The buy-side note now states that target upper is an upper bound, not a promised auto-sell price.

This release does not change trading rules or default execution parameters.

## Root Cause

The UI previously showed one generic historical hit value, which made the target-upper number look more actionable than it really was.

The strict 10-minute path audit shows the actual 12M distribution:

| Period | Trades | Target Upper Touch | Target Upper Sellable | First Management | Trail Active | Hard Stop | VWAP Fail |
|---|---:|---:|---:|---:|---:|---:|---:|
| 12M | 82 | 8.5366% | 8.5366% | 39.0244% | 40.2439% | 28.0488% | 25.6098% |

So the target upper should be treated as an upside boundary. The practical management path is usually first management line plus trailing/VWAP/stop handling.

## Validation

Strict 10-minute regression:

```powershell
python tools\backtest_strict_10m_ledger.py --end-date 2026-07-03 --period-months 1,3,6,9,12 --out-dir output\backtest_strict_10m_v0436_path_audit
```

Result, unchanged from the current default baseline:

| Period | Return | Max DD | Trades | Profit Factor |
|---|---:|---:|---:|---:|
| 1M | 13.7512% | 3.5834% | 14 | 4.9490 |
| 3M | 37.6401% | 3.8198% | 29 | 6.0905 |
| 6M | 59.7816% | 3.9371% | 49 | 4.5010 |
| 9M | 65.1715% | 5.2000% | 68 | 3.5786 |
| 12M | 63.0575% | 7.2032% | 82 | 2.8959 |

Guardrails:

- `bad_300_301=0`
- `bad_lots=0`
- `bad_tick=0`

Smoke checks:

```powershell
python -m py_compile tools\backtest_strict_10m_ledger.py short_term_live_monitor.py local_trading_assistant.py desktop_app.py trading_assistant_app.py
```

Default calibration smoke:

```text
normal 8.0 8.0 40.0 50 calibration_12M_normal
cold 8.3333 8.3333 33.3333 24 calibration_12M_cold
narrow_rally 12.5 12.5 50.0 8 calibration_12M_narrow_rally
hot 8.5366 8.5366 39.0244 82 calibration_12M_overall
```
