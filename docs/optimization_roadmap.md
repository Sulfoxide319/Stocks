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
| 2 | Separate `目标上沿`, first management line, and actual exit probability in UI/reporting. | Done; 12M calibrated hit rates now feed live UI | v0.4.23, v0.4.24, v0.4.36 |
| 3 | Add official low-noise event scoring without allowing events to bypass VWAP/score/mainboard filters. | Audited; current US event samples have no A-share watchlist overlap, so no default trading impact | v0.4.24, v0.4.35 |
| 4 | Expose practical sell signal points: target upper, first management, trailing stop, hard stop, VWAP/cost weakness, pre-close weakness. | Done; strict backtest now exports sell-path audit | v0.4.26, v0.4.36 |
| 5 | Persist holding management state so repeated scans do not spam first-touch prompts. | Done | v0.4.27 |
| 6 | Add condition diagnostics for market state, entry timing, volatility, momentum, sector, and exit reason. | Done | v0.4.28 |
| 7 | Reduce cold-market and false-breakout losses without over-filtering recent profitable rebounds. | Done for cold weak momentum and low-ATR noise; v0.4.38 follow-up rejected simple cold/sector filters as over-filtering | v0.4.28, v0.4.32, v0.4.33, docs/optimization_experiment_log.md |
| 8 | Align strict backtest exits with live sell rules. | Done for VWAP fail; managed-position VWAP weakness now uses a two-confirmation path in backtest and live alerts | v0.4.29, v0.4.36, v0.4.37 |
| 9 | Tune risk exits and trailing behavior to preserve winners while cutting noise losses. | Improved for first-management VWAP pullbacks and normal-market trailing winners; v0.4.38 follow-up rejected global VWAP delay/buffer changes | v0.4.30, v0.4.31, v0.4.37, v0.4.38, docs/optimization_experiment_log.md |
| 10 | Improve external validation robustness: rolling windows, policy reproduction, and walk-forward style checks. | Done for rolling strict validation; v0.4.38 beat v0.4.37 proxy on 9/9 rolling returns, with one 6M rolling DD micro-increase documented as a return/DD tradeoff | v0.4.34, v0.4.37, v0.4.38 |

## Current Baseline

Baseline: `v0.4.38`.

| Period | Return | Max DD | Trades | Profit Factor |
|---|---:|---:|---:|---:|
| 1M | 13.7512% | 3.5834% | 14 | 4.9490 |
| 3M | 41.5900% | 3.7516% | 29 | 6.9755 |
| 6M | 64.9549% | 3.9371% | 49 | 4.8789 |
| 9M | 70.9137% | 5.2000% | 68 | 3.8191 |
| 12M | 70.4732% | 7.2032% | 82 | 3.1338 |
