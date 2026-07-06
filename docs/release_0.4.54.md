# Release 0.4.54

## Changes

- Fixes capital-percentage safety:
  - `position_sizing` never returns a suggested capital percentage above `100%`.
  - manual buy quantity calculation caps abnormal input such as `250%` to `100%`.
  - desktop numeric parsing now accepts percent-formatted strings such as `50%`.
- Changes the default quality sizing profile:
  - `quality_capital_max_factor`: `1.40` -> `1.60`
  - `max_single_position_pct`: backtest default `0`/disabled -> `50%`
  - live monitor default single-position cap `45%` -> `50%`
- Live scan rows now use portfolio-budget normalization:
  - capital is assigned only to the highest-quality candidates up to `max_positions`.
  - the displayed buy-side suggested capital percentages sum to no more than `100%`.
  - non-selected observation rows keep `0%` suggested capital.

## Package

- `dist\StocksTradingAssistant-v0.4.54.zip`
- SHA256: `ED079419290723E99442C0D45B475CE22ED395EFE4401977D6E0EAA1D770904D`

## Data Validation

Fixed end date `2026-07-03`, strict 10-minute backtest, periods `1M/3M/6M/12M`.

| Period | Old Return | New Return | Delta | Old Max DD | New Max DD | Old Avg Actual Cap | New Avg Actual Cap |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1M | 16.7714% | 17.8802% | +1.1088 pct | 4.0568% | 4.5942% | 35.1146% | 37.3124% |
| 3M | 49.1110% | 51.8538% | +2.7428 pct | 4.1093% | 4.5384% | 35.6404% | 37.4945% |
| 6M | 76.6191% | 82.8558% | +6.2367 pct | 4.6716% | 4.5455% | 35.8614% | 37.0603% |
| 12M | 88.4358% | 92.7826% | +4.3468 pct | 7.0904% | 8.0595% | 38.0335% | 40.4646% |

Position exposure check:

| Metric | Old | New |
|---|---:|---:|
| 12M max target capital | 66.7823% | 50.0000% |
| 12M max actual capital | 65.8314% | 49.9918% |
| 12M actual capital > 50% count | 7 | 0 |
| target capital > 100% count | 0 | 0 |

Rolling validation over end dates `2026-05-29`, `2026-06-30`, `2026-07-03`:

| Period | Return Wins vs Old | Avg Old Return | Avg New Return | Avg Old DD | Avg New DD |
|---|---:|---:|---:|---:|---:|
| 1M | 3/3 | 14.5683% | 15.6594% | 3.0492% | 3.4270% |
| 3M | 3/3 | 47.6969% | 50.8142% | 4.2620% | 4.4748% |
| 6M | 3/3 | 72.6134% | 79.2352% | 4.5860% | 4.4917% |
| 12M | 3/3 | 77.3328% | 81.4523% | 7.9453% | 8.8116% |

## Validation Commands

- `python -m py_compile position_sizing.py trade_quantity.py local_trading_assistant.py short_term_live_monitor.py single_stock_analysis.py tools\backtest_strict_10m_ledger.py desktop_app.py trading_assistant_app.py`
- capital invariant smoke:
  - single suggested capital never exceeds `100%`
  - `250%` budget input caps to `100%`
  - generated buy advice totals no more than `100%`
- `python tools\desktop_functional_smoke.py`
- `python tools\backtest_strict_10m_ledger.py --end-date 2026-07-03 --period-months 1,3,6,12 --out-dir output\capital_sizing_fix_new_default_verify`
- `python tools\rolling_strict_10m_validation.py --no-v0432-proxy --baseline-name old_v053 --end-dates 2026-05-29,2026-06-30,2026-07-03 --period-months 1,3,6,12 --scenario "old_v053=--quality-capital-max-factor 1.40 --max-single-position-pct 0" --out-dir output\capital_sizing_fix_rolling_3x_12m`

## Notes

- This is not a pure drawdown-reduction release. It caps extreme single-position exposure and fixes percentage overflow, while the tested return profile improves in every fixed and rolling return window.
- The existing profit-cushion behavior remains the portfolio-level "performance is good, allow stronger capital deployment" mechanism; this release avoids unlimited last-slot concentration by adding the `50%` single-position cap.
