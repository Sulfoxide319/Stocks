# Stocks Trading Assistant 0.4.37

## Managed VWAP Weakness Confirmation

- Changes strict 10-minute exits after the first management line:
  - before first management: VWAP fail still exits after `1` weak 10-minute bar
  - after first management: VWAP fail now requires `2` weak 10-minute bars
- Adds `--vwap-fail-after-first-manage-bars`, default `2`.
- Live holding advice mirrors the same idea:
  - first scan below VWAP after first management emits `VWAP_WEAK_CONFIRM`
  - a repeated weak scan emits `REDUCE_PROFIT`
- Keeps hard stop, target upper, trailing stop, T+1, lot size, tick size, and mainboard filters unchanged.

## Root Cause

The v0.4.36 sell-path audit showed that a global two-bar VWAP fail improved 3M/6M/9M/12M but hurt 1M. The loss came from trades that had never reached the first management line. The long-window gain came from trades that had already reached the first management line but were sold too early on the first VWAP weakness.

So the fix is conditional:

- weak before first management remains a fast failure signal
- weak after first management gets one confirmation bar to avoid selling recoverable winners too early

## Validation

Strict 10-minute default regression:

```powershell
python tools\backtest_strict_10m_ledger.py --end-date 2026-07-03 --period-months 1,3,6,9,12 --out-dir output\backtest_strict_10m_v0437_candidate
```

Compared with v0.4.36:

| Period | v0.4.36 Return | v0.4.37 Return | Delta | v0.4.36 DD | v0.4.37 DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 13.7512% | 13.7512% | +0.0000 pct | 3.5834% | 3.5834% | 14 |
| 3M | 37.6401% | 41.5900% | +3.9499 pct | 3.8198% | 3.7516% | 29 |
| 6M | 59.7816% | 64.9549% | +5.1733 pct | 3.9371% | 3.9371% | 49 |
| 9M | 65.1715% | 69.4801% | +4.3086 pct | 5.2000% | 5.2000% | 68 |
| 12M | 63.0575% | 68.4631% | +5.4056 pct | 7.2032% | 7.2032% | 82 |

Guardrails:

- `bad_300_301=0`
- `bad_lots=0`
- `bad_tick=0`
- 12M trades remain `82`, above the 80% minimum.

Smoke checks:

```powershell
python -B -m py_compile tools\backtest_strict_10m_ledger.py local_trading_assistant.py short_term_live_monitor.py desktop_app.py trading_assistant_app.py
```

Rolling v0.4.37 vs v0.4.36 proxy:

| End Date | Period | v0.4.36 Return | v0.4.37 Return | Delta | v0.4.36 DD | v0.4.37 DD | Trades |
|---|---|---:|---:|---:|---:|---:|---:|
| 2026-05-29 | 1M | 5.5043% | 5.5043% | +0.0000 pct | 0.7775% | 0.7775% | 3 |
| 2026-05-29 | 3M | 32.2013% | 36.6584% | +4.4571 pct | 3.9614% | 3.9614% | 24 |
| 2026-05-29 | 6M | 47.1007% | 51.3161% | +4.2154 pct | 3.8778% | 3.8778% | 39 |
| 2026-06-30 | 1M | 18.1776% | 18.1776% | +0.0000 pct | 3.7837% | 3.7837% | 16 |
| 2026-06-30 | 3M | 38.9124% | 42.8623% | +3.9499 pct | 3.8198% | 3.7516% | 28 |
| 2026-06-30 | 6M | 60.7284% | 65.8083% | +5.0799 pct | 3.9391% | 3.9391% | 49 |
| 2026-07-03 | 1M | 13.7512% | 13.7512% | +0.0000 pct | 3.5834% | 3.5834% | 14 |
| 2026-07-03 | 3M | 37.6401% | 41.5900% | +3.9499 pct | 3.8198% | 3.7516% | 29 |
| 2026-07-03 | 6M | 59.7816% | 64.9549% | +5.1733 pct | 3.9370% | 3.9370% | 49 |

Rolling summary: return non-lower `9/9`, drawdown non-higher `9/9`, both `9/9`.
