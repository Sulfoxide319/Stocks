# Stocks Trading Assistant 0.4.32

## Cold-Market Momentum Floor

- Raises the cold-market 10-day momentum floor from `5.0%` to `7.5%`.
- Applies the same default to:
  - strict 10-minute backtest
  - live monitor parser defaults
  - desktop/local assistant launch defaults
- Keeps other cold-market filters unchanged to avoid over-filtering recent profitable rebounds.

## Root Cause

The v0.4.31 condition diagnostics showed cold-market weak-momentum trades were still the main drag:

- `market_state=cold`: average return `0.2370%`, win rate `29.7297%`, hard-stop rate `48.6486%`.
- `momentum_10d 0~10%`: average return `-0.0577%`, win rate `33.3333%`, hard-stop rate `40.7407%`.
- `atr <4.1%`: average return `-0.6349%`, profit factor `0.5541`.

Grid validation found `cold_min_momentum_10d_pct=7.5` was the best conservative passing candidate. More aggressive filters improved some long-window metrics but failed the short-window acceptance gates or cut the 12M trade count too much.

Rejected examples:

- `cold_min_momentum_10d_pct=10.0`: failed `1M/3M/6M/9M/12M` return gates.
- cold first-entry high-score filters: improved 12M but failed `1M/3M`.
- cold sector momentum filters: reduced trades too much and failed all return gates.
- global `min_score=93`: reduced 12M trades to `28`, violating the anti-overfilter rule.

## Validation

End date fixed at `2026-07-03`; baseline is v0.4.31 strict 10-minute execution.

| Period | v0.4.31 Return | v0.4.32 Return | Delta | v0.4.31 Max DD | v0.4.32 Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 1M | 13.0521% | 13.5002% | +0.4481 pct | 3.3571% | 3.3571% | 14 |
| 3M | 34.6485% | 37.2883% | +2.6398 pct | 3.5742% | 3.5161% | 30 |
| 6M | 53.8359% | 56.9841% | +3.1482 pct | 5.2055% | 4.3920% | 53 |
| 9M | 54.6630% | 59.3964% | +4.7334 pct | 6.3346% | 5.5709% | 74 |
| 12M | 53.8373% | 57.8088% | +3.9715 pct | 7.6350% | 7.6350% | 90 |

Acceptance checks:

- `1M/3M/6M/9M/12M return >= v0.4.31`: passed.
- `12M return > 53.8373%`: passed (`57.8088%`).
- `12M max_drawdown <= 7.6350%`: passed (`7.6350%`).
- `12M trades >= 80% of v0.4.31`: passed (`90 >= 76`).
- `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.
