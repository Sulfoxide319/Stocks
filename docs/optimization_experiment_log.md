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

### Takeaway

The next viable improvement probably needs a more local rule than simple cold-state tightening, global sector filtering, or delayed VWAP failure. Good next candidates should target pre-entry false-breakout quality without removing the current profitable cold rebounds, or use position sizing rather than outright filtering.
