# Short-Term Strategy Optimization Roadmap

This roadmap tracks the iterative optimization loop for the rule-score, hard-filter, and intraday VWAP-confirmed short-term strategy.

## Global Acceptance Contract

Each trading-logic release must be validated against the previous default version with the strict 10-minute engine, ending at `2026-07-03`, across `1M,3M,6M,9M,12M`.

A candidate may become default only if:

- `1M/3M/6M/9M/12M` returns are not lower than the previous default.
- `12M` max drawdown is not higher than the previous default, unless the change is explicitly a reporting-only or execution-model correction with a documented risk tradeoff.
- `12M` closed trades are at least `80%` of the previous default.
- `bad_300_301=0`, `bad_lots=0`, and `bad_tick=0`.
- The root cause is documented with ledger or diagnostics evidence.

## Optimization Targets

| # | Target | Current Status | Evidence / Release |
|---:|---|---|---|
| 1 | Keep buy universe liquid mainboard only; never write `300/301` buy candidates. | Done, ongoing invariant | v0.4.22+ validation checks |
| 2 | Separate `目标上沿`, first management line, and actual exit probability in UI/reporting. | Done | v0.4.23, v0.4.24 |
| 3 | Add official low-noise event scoring without allowing events to bypass VWAP/score/mainboard filters. | Audited; current US event samples have no A-share watchlist overlap, so no default trading impact | v0.4.24, v0.4.35 |
| 4 | Expose practical sell signal points: target upper, first management, trailing stop, hard stop, VWAP/cost weakness, pre-close weakness. | Done | v0.4.26 |
| 5 | Persist holding management state so repeated scans do not spam first-touch prompts. | Done | v0.4.27 |
| 6 | Add condition diagnostics for market state, entry timing, volatility, momentum, sector, and exit reason. | Done | v0.4.28 |
| 7 | Reduce cold-market and false-breakout losses without over-filtering recent profitable rebounds. | Done for cold weak momentum and low-ATR noise; continue monitoring | v0.4.28, v0.4.32, v0.4.33 |
| 8 | Align strict backtest exits with live sell rules. | Done for VWAP fail; ongoing for sell-state parity | v0.4.29 |
| 9 | Tune risk exits and trailing behavior to preserve winners while cutting noise losses. | Done for stop floor and trailing reference; continue sensitivity checks | v0.4.30, v0.4.31 |
| 10 | Improve external validation robustness: rolling windows, policy reproduction, and walk-forward style checks. | Done for rolling strict validation; continue using before strategy releases | v0.4.34 |

## Current Baseline

Baseline: `v0.4.33`.

| Period | Return | Max DD | Trades | Profit Factor |
|---|---:|---:|---:|---:|
| 1M | 13.7512% | 3.5834% | 14 | 4.9490 |
| 3M | 37.6401% | 3.8198% | 29 | 6.0905 |
| 6M | 59.7816% | 3.9371% | 49 | 4.5010 |
| 9M | 65.1715% | 5.2000% | 68 | 3.5786 |
| 12M | 63.0575% | 7.2032% | 82 | 2.8959 |
