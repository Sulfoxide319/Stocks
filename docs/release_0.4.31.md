# Stocks Trading Assistant 0.4.31

## Conservative Trailing Reference

- Changes strict 10-minute trailing-stop simulation to use the previous high watermark by default.
- Keeps the old behavior available with `--trailing-reference-policy same_bar_high`.
- Records `trailing_reference_policy` in the strict backtest summary for reproducibility.
- Leaves the dynamic exit parameters unchanged:
  - target: `max(5%, 0.9 * ATR% + 0.35 * 5d range%)`, capped at `18%`
  - hard stop: `max(1.5%, 0.45 * ATR%)`, capped at `7%`
  - trailing stop: `max(2.5%, 0.25 * ATR%)`, capped at `6%`

## Root Cause

The old strict 10-minute engine updated the bar's high before checking whether the same bar had fallen through the trailing stop. That made every 10-minute OHLC bar behave as if the high occurred before the low.

The new default uses the high watermark available before the current bar for trailing-stop triggers. The current bar's high is still recorded for later bars if no exit occurs. This better matches a sequential live high-watermark model and removes the ambiguous same-bar high/low ordering assumption.

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.30 strict 10-minute execution.

| Period | v0.4.30 Return | v0.4.31 Return | Delta | v0.4.30 Max DD | v0.4.31 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 10.2311% | 13.0521% | +2.8210 pct | 2.3046% | 3.3571% | 15 |
| 3M | 29.1512% | 34.6485% | +5.4973 pct | 2.7915% | 3.5742% | 33 |
| 6M | 48.1821% | 53.8359% | +5.6538 pct | 5.1574% | 5.2055% | 57 |
| 9M | 47.2864% | 54.6630% | +7.3766 pct | 6.5300% | 6.3346% | 79 |
| 12M | 42.5919% | 53.8373% | +11.2454 pct | 8.0107% | 7.6350% | 95 |

Policy reproduction check:

- `--trailing-reference-policy same_bar_high` reproduces v0.4.30 returns exactly:
  - 1M `10.2311%`
  - 3M `29.1512%`
  - 6M `48.1821%`
  - 9M `47.2864%`
  - 12M `42.5919%`

Acceptance checks:

- `1M/3M/6M/9M/12M return >= v0.4.30`: passed.
- `12M return > 42.5919%`: passed (`53.8373%`).
- `12M max_drawdown <= 8.0107%`: passed (`7.6350%`).
- `12M trades >= 80% of v0.4.30`: passed (`95 >= 76`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.
