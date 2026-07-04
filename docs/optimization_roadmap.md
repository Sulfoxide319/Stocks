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
| 9 | Tune risk exits and trailing behavior to preserve winners while cutting noise losses. | Improved for first-management VWAP pullbacks, normal-market trailing winners, and mature profit-cushion concentration | v0.4.30, v0.4.31, v0.4.37, v0.4.38, v0.4.39, docs/optimization_experiment_log.md |
| 10 | Improve external validation robustness: rolling windows, policy reproduction, and walk-forward style checks. | Done for rolling strict validation; added reusable baseline-vs-candidate summary comparison and v0.4.38 proxy checks for release gates | v0.4.34, v0.4.37, v0.4.38, v0.4.39, tools/compare_backtest_summaries.py |
| 11 | Add quality-aware buy-side capital sizing instead of equal slot allocation only. | Done; quality default beats equal, score-linear, and edge-linear in 1M/3M/6M/9M/12M while keeping 12M DD below v0.4.40 | v0.4.41 |

## Current Research Leads

| Lead | Evidence | Default Decision |
|---|---|---|
| Conditional concentration / aggressive profile | `max-positions=2` with 70% capital per state lifts 1M/3M/6M/9M/12M returns to `15.1691%/42.3105%/68.1952%/74.3256%/74.5904%`, with clean `bad_300_301/bad_lots/bad_tick=0/0/0`. Mature profit-cushion gating keeps fixed and rolling gates clean while preserving part of the long-window gain. | Promoted in v0.4.39 only after `8%` equity cushion and `120` elapsed trading days. |
| Quality-aware position sizing | `quality` sizing with Edge floor, traded-value confirmed quality uplift, and a 3.5% drawdown governor improves 1M/3M/6M/9M/12M over equal, score-linear, and edge-linear sizing. | Promoted in v0.4.41. |
| Quality sorting | `--selection-mode quality` holds 12M DD flat and slightly improves 6M/9M/12M. | Still not default: v0.4.41 improves sizing, not buy ranking. |
| Simple cold/sector filters | Multiple follow-up tests in `docs/optimization_experiment_log.md`. | Rejected: over-filtering removes profitable rebounds. |

## Current Baseline

Baseline: `v0.4.41`.

| Period | Return | Max DD | Trades | Profit Factor |
|---|---:|---:|---:|---:|
| 1M | 16.7714% | 4.0568% | 14 | 5.2905 |
| 3M | 49.1110% | 4.1093% | 29 | 7.2641 |
| 6M | 76.6191% | 4.6716% | 49 | 4.7995 |
| 9M | 86.8375% | 5.2586% | 63 | 3.9879 |
| 12M | 88.4358% | 7.0904% | 75 | 3.3479 |
