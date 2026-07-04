# Optimization Experiment Log

This log records sensitivity checks that did not become default trading logic. The goal is to avoid repeating plausible but rejected changes and to keep the optimization loop evidence-based.

## 2026-07-04 v0.4.38 Follow-Up

Baseline: `v0.4.38`, strict 10-minute engine, no events, end date `2026-07-03`.

Acceptance reminder: a default change must keep `1M/3M/6M/9M/12M` returns non-lower than the current baseline, keep 12M drawdown no higher unless explicitly documented as a risk tradeoff, keep 12M trades above 80% of baseline, and preserve `bad_300_301=0`, `bad_lots=0`, `bad_tick=0`.

### Root Observation

The remaining 12M drag is concentrated in trades that never reach the first management line:

- `hard_stop_10m`: 23 trades, average `-2.85%`
- `vwap_fail_10m`: 20 trades, average `-1.23%`
- cold state remains weaker than normal, but simple cold filters also remove profitable rebounds

### Rejected Candidates

| Scenario | Change | 1M | 3M | 6M | 9M | 12M | 12M Delta vs v0.4.38 | 12M DD | 12M Trades | Decision |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `scenario_v0439_cold_tv2` | `--cold-min-traded-value-ratio 2.0` | 13.2066% | 42.0671% | 59.9637% | 67.6321% | 67.6916% | -2.7816 pct | 6.4214% | 72 | Reject: over-filters cold rebounds and hurts 1M/6M/9M/12M |
| `scenario_v0439_cold_sector0` | `--cold-min-sector-momentum-5d-pct 0` | 8.9017% | 27.4977% | 48.1359% | 56.0335% | 56.3341% | -14.1391 pct | 6.4214% | 58 | Reject: severe over-filtering |
| `scenario_v0439_cold_score93` | `--cold-min-score 93` | 9.6353% | 33.4446% | 56.1500% | 62.7161% | 63.0168% | -7.4564 pct | 6.4214% | 63 | Reject: score-only cold tightening removes too many winners |
| `scenario_v0439_sector_filter0` | `--sector-mode filter --min-sector-momentum-5d 0 --min-sector-above-ma20-ratio 0.35` | 0.0000% | 8.4158% | 13.5019% | 17.7335% | 21.3473% | -49.1259 pct | 3.9148% | 30 | Reject: drawdown improves only by collapsing participation |
| `scenario_v0439_vwap2global` | `--vwap-fail-bars 2` | 13.3545% | 40.7446% | 61.1288% | 67.2840% | 67.2937% | -3.1795 pct | 7.0338% | 82 | Reject: delayed VWAP exit before first management preserves losers |
| `scenario_v0439_vwap_buffer003` | `--vwap-fail-buffer 0.003` | 13.5214% | 41.3002% | 64.0109% | 70.1652% | 69.3868% | -1.0864 pct | 7.3147% | 82 | Reject: small buffer hurts all required return windows and raises 12M DD |
| `scenario_v0439_entry_start1000` | `--entry-start-time 10:00` | 13.7714% | 31.7754% | 53.0724% | 61.1448% | 60.9653% | -9.5079 pct | 7.7840% | 74 | Reject: tiny 1M gain is not worth broad degradation |
| `scenario_v0439_entry1000_vwapbuf003` | `--entry-start-time 10:00 --vwap-fail-buffer 0.003` | 13.5416% | 31.5056% | 51.9667% | 60.2190% | 59.8993% | -10.5739 pct | 7.8944% | 74 | Reject: broad degradation |
| `scenario_v0439_selection_quality` | `--selection-mode quality` | 13.5093% | 41.3019% | 65.1967% | 70.9262% | 70.6045% | +0.1313 pct | 7.2032% | 82 | Reject for default: 6M/9M/12M improve slightly, but 1M/3M returns fall below baseline |
| `scenario_v0439_maxpos4` | `--max-positions 4` | 10.2329% | 30.1453% | 44.3385% | 47.1338% | 45.1825% | -25.2907 pct | 5.2718% | 89 | Reject: drawdown improvement comes from diluted exposure and much weaker returns |
| `scenario_v0439_cold_cap080` | `--cold-capital-factor 0.8` | 12.2729% | 36.8574% | 59.0005% | 65.5529% | 65.5067% | -4.9665 pct | 6.8213% | 82 | Reject: colder sizing helps DD but removes too much return |
| `scenario_v0439_maxpos2_cap067` | `--max-positions 2 --normal-capital-factor 0.67 --cold-capital-factor 0.67` | 13.6202% | 40.7391% | 64.4765% | 70.8650% | 71.2348% | +0.7616 pct | 7.7224% | 72 | Reject: 1M/3M/6M/9M below baseline and 12M DD higher |
| `scenario_v0439_maxpos2_cap070` | `--max-positions 2 --normal-capital-factor 0.70 --cold-capital-factor 0.70` | 15.1691% | 42.3105% | 68.1952% | 74.3256% | 74.5904% | +4.1172 pct | 8.0944% | 72 | Not default: all returns and trade-ratio gates pass, but 12M DD is +0.8912 pct; keep as aggressive-mode lead |
| `scenario_v0439_maxpos2_cap075` | `--max-positions 2 --normal-capital-factor 0.75 --cold-capital-factor 0.75` | 15.2986% | 45.0613% | 72.3358% | 80.3659% | 80.9646% | +10.4914 pct | 8.4412% | 72 | Not default: stronger return, higher DD; aggressive-mode lead only |
| `scenario_v0439_maxpos2_cap080` | `--max-positions 2 --normal-capital-factor 0.80 --cold-capital-factor 0.80` | 16.3915% | 47.5563% | 76.8515% | 85.8273% | 85.6822% | +15.2090 pct | 9.0301% | 72 | Not default: strongest return in this group, but DD rises too much for steady default |
| `scenario_v0439_maxpos2_n070_c050` | `--max-positions 2 --normal-capital-factor 0.70 --cold-capital-factor 0.50` | 11.3202% | 35.4908% | 59.3049% | 65.0675% | 66.9953% | -3.4779 pct | 7.6723% | 72 | Reject: reducing cold/narrow exposure destroys short-window returns |
| `scenario_v0439_maxpos2_n070_c060` | `--max-positions 2 --normal-capital-factor 0.70 --cold-capital-factor 0.60` | 13.2550% | 38.7106% | 63.4144% | 69.2601% | 70.6750% | +0.2018 pct | 7.7531% | 72 | Reject: 1M/3M/6M/9M below baseline and 12M DD higher |
| `scenario_v0439_maxpos2_n075_c050` | `--max-positions 2 --normal-capital-factor 0.75 --cold-capital-factor 0.50` | 11.3202% | 36.3697% | 62.1905% | 68.7513% | 70.2001% | -0.2731 pct | 7.9787% | 72 | Reject: broad short-window degradation |
| `scenario_v0439_maxpos2_n075_c060` | `--max-positions 2 --normal-capital-factor 0.75 --cold-capital-factor 0.60` | 13.2550% | 39.5217% | 66.0805% | 74.0279% | 75.4428% | +4.9696 pct | 8.0595% | 72 | Reject: 1M/3M below baseline and 12M DD higher |
| `scenario_v0439_maxpos2_cap070_quality` | `--max-positions 2 --normal-capital-factor 0.70 --cold-capital-factor 0.70 --selection-mode quality` | 11.8181% | 39.3700% | 67.2487% | 73.0437% | 73.8094% | +3.3362 pct | 8.0944% | 72 | Reject: quality sort does not reduce concentration DD and hurts 1M/3M |
| `scenario_v0439_maxpos2_cap070_trail033` | `--max-positions 2 --normal-capital-factor 0.70 --cold-capital-factor 0.70 --normal-trail-atr-mult 0.33` | 15.1691% | 42.3105% | 68.1952% | 71.9333% | 72.3449% | +1.8717 pct | 8.0944% | 72 | Reject: tighter normal trailing lowers return without fixing 12M DD |
| `scenario_v0439_maxpos2_cap070_trail032` | `--max-positions 2 --normal-capital-factor 0.70 --cold-capital-factor 0.70 --normal-trail-atr-mult 0.32` | 15.1691% | 42.3105% | 68.1952% | 71.9465% | 72.3662% | +1.8930 pct | 8.0944% | 72 | Reject: same DD problem as cap070 with less return |

### Accepted Candidate

| Scenario | Change | 1M | 3M | 6M | 9M | 12M | 12M Delta vs v0.4.38 | 12M DD | 12M Trades | Decision |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `backtest_strict_10m_v0439_candidate` | Profit-cushion aggressive mode: activate only after current equity return reaches `8%` and at least `120` trading days have elapsed, then use `max_positions=2` with `normal/cold capital factor=0.70` for new entries | 13.7512% | 41.5900% | 64.9549% | 71.2338% | 72.3845% | +1.9113 pct | 7.2032% | 75 | Accept: fixed-window returns are non-lower, 12M DD is flat, 12M trades stay above 80%, and hard checks remain `0/0/0` |

Rejected variants around the accepted rule:

- `8%` cushion with no maturity delay improved 12M more aggressively but hurt the `2026-05-29` rolling 3M/6M windows.
- `8%` cushion with `90` trading days passed the fixed-window gate but still hurt the `2026-05-29` rolling 6M window.
- Higher `18%/20%/25%` cushions delayed activation too much and failed one or more fixed windows, especially 6M.

### Takeaway

The next viable improvement probably needs a more local rule than simple cold-state tightening, global sector filtering, delayed VWAP failure, or blunt concentration. `max-positions=2` confirms that capital concentration is a real profit source, but the added drawdown is not acceptable unless the strategy first earns a mature profit cushion. The accepted v0.4.39 rule turns concentration into a late-stage exposure upgrade instead of a permanent default.

Validation aid added after these trials: `tools/compare_backtest_summaries.py`. The comparison report at `output/scenario_v0439_comparison/summary_comparison.md` shows every tested concentration candidate failing the default acceptance gate even when returns and hard checks pass.
