# Stocks Trading Assistant 0.4.41

## Quality-Aware Capital Sizing

- Adds `position_sizing.py` as the shared buy-side capital allocation model used by strict backtests and live scan reporting.
- New default strict backtest sizing mode is `quality`:
  - Start from the existing slot budget: cash divided by remaining position slots.
  - Apply market and profit-cushion capital factors.
  - Apply a quality capital factor in the `0.70` to `1.40` range.
  - Apply a drawdown governor: when current equity drawdown reaches `3.5%`, new-entry capital is multiplied by `0.75`.
- The quality factor is deliberately Edge-led:
  - Edge is the floor, based on estimated hit probability, target/stop payoff, volume confirmation, short momentum, and MA5 heat.
  - Composite quality can only lift the factor above Edge when strong traded-value confirmation exists.
  - This keeps low-liquidity or weakly confirmed candidates from receiving extra capital even when their raw score looks acceptable.

## Live Buy Prompt

- Buy candidates now carry:
  - `suggested_capital_pct`
  - `position_quality_score`
  - `position_quality_grade`
  - `capital_factor`
  - `capital_reason`
- Markdown, JSON, CSV, PySide desktop UI, and Tk desktop UI show the suggested buy-side capital percentage and quality grade.
- Watch-only, quote-only, hot-market, data-unavailable, and low-score rows keep `suggested_capital_pct=0` so the UI does not turn observation rows into buy instructions.

## Strict Backtest Validation

Engine: strict 10-minute execution, no events, end date `2026-07-03`, periods `1M,3M,6M,9M,12M`.

| Algorithm | 1M | 3M | 6M | 9M | 12M | 12M DD | 12M PF | Avg Actual Capital |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| equal / v0.4.40 | 13.7512% | 41.5900% | 64.9549% | 71.2338% | 72.3845% | 7.2032% | 3.1659 | 35.43% |
| score_linear | 14.6623% | 44.9802% | 70.2950% | 81.1639% | 81.8773% | 7.2945% | 3.2817 | 36.68% |
| edge_linear | 15.4827% | 45.5629% | 73.0079% | 82.6328% | 84.8956% | 7.9227% | 3.2552 | 38.07% |
| quality default | 16.7714% | 49.1110% | 76.6191% | 86.8375% | 88.4358% | 7.0904% | 3.3479 | 38.03% |

Acceptance comparison against v0.4.40:

```text
PASS quality_default: returns=True 12M_dd=True 12M_trades=True bad=0/0/0
```

Generated evidence:

- `output/backtest_position_quality_default/strict_10m_no_events_1M_12M_to_20260703_summary.csv`
- `output/backtest_position_quality_default/quality_vs_v04040.md`

## Notes

- This remains a rule-score, hard-filter, VWAP-confirmed strategy; no machine-learning model is introduced.
- The capital recommendation is a sizing signal, not a guarantee of reaching the target upper price.
- Live portfolio-level drawdown is not inferred from brokerage data yet; the strict backtest drawdown governor is applied inside the backtest engine, while live rows show per-candidate quality sizing and keep disabled rows at `0%`.
