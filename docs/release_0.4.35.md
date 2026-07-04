# Stocks Trading Assistant 0.4.35

## Official Event Score Audit

- Adds `tools/audit_official_event_scores.py`.
- Audits event files before using them as score adjustments.
- Reports:
  - official-source count
  - signed positive/negative event count
  - source and reason distribution
  - watchlist overlap
  - signed event score adjustment
- Adds live-monitor event status `no_watchlist_overlap` when an event file has signed scores but none of them match the configured watchlist.

This release does not change trading rules or default strategy parameters.

## Root Cause

The official event mechanism existed, but the current event radar samples were US technology names while the active short-term strategy uses a liquid A-share mainboard universe.

Auditing the current samples showed:

| Event File | Events | Signed Official Symbols | Watchlist Overlap |
|---|---:|---:|---:|
| `output/tech_event_radar_20260703.json` | 6 | 3 | 0 |
| `output/tech_event_radar_20260702.json` | 9 | 2 | 0 |

So the event factor currently has no direct scoring impact on the configured A-share watchlist. This is better made explicit than silently treated as active.

## Guardrail

Official events remain score adjustments only. They do not bypass:

- mainboard prefix filtering
- score thresholds
- VWAP confirmation
- T+1 selling
- 100-share lot sizing
- 0.01 tick pricing
- limit-up/down handling

## Validation

Commands:

```powershell
python -m py_compile tools\audit_official_event_scores.py short_term_live_monitor.py
python tools\audit_official_event_scores.py --events output\tech_event_radar_20260703.json
python tools\audit_official_event_scores.py --events output\tech_event_radar_20260702.json --out-dir output\event_score_audit_20260702
```

Event status smoke check:

```text
scores 3 overlap 0 status no_watchlist_overlap warning event_scores_have_no_watchlist_overlap
```
