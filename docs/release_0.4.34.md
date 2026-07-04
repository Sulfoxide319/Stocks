# Stocks Trading Assistant 0.4.34

## Rolling Strict Validation

- Adds `tools/rolling_strict_10m_validation.py`.
- Runs strict 10-minute backtests across multiple end dates and scenarios.
- Default scenarios:
  - `current_default`
  - `v0432_proxy`, using `--cold-min-atr-pct 0 --cold-capital-factor 0.9`
- Default end dates:
  - `2026-05-29`
  - `2026-06-30`
  - `2026-07-03`
- Default periods:
  - `1M`
  - `3M`
  - `6M`
- Writes:
  - `output/rolling_strict_10m_validation/rolling_strict_10m_validation.csv`
  - `output/rolling_strict_10m_validation/rolling_strict_10m_validation.md`

This release does not change the trading strategy. It adds a repeatable validation layer for future strategy releases.

## Why It Matters

The previous optimization loop validated every strategy release against the fixed `2026-07-03` end date. That is necessary for release gates, but not sufficient to detect ending-date overfit.

Rolling validation now makes the tradeoff explicit:

- v0.4.33 `current_default` beat the v0.4.32 proxy on return in `9/9` rolling rows.
- It beat on both return and drawdown in `4/9` rolling rows.
- All rolling rows passed `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.

This confirms v0.4.33's return lift is not unique to the final `2026-07-03` window, while also showing that short-window drawdown can rise. Future strategy changes should use this report before release.

## Validation

Command:

```powershell
python tools\rolling_strict_10m_validation.py
```

Report summary:

| Check | Result |
|---|---:|
| Compared rolling rows | 9 |
| Return >= v0.4.32 proxy | 9 |
| Drawdown <= v0.4.32 proxy | 4 |
| Return and drawdown both better | 4 |
| Rows with bad checks | 0 |

The tool also supports reusing already generated strict outputs:

```powershell
python tools\rolling_strict_10m_validation.py --reuse-existing
```
