# Data Sources And Freshness Contract

This project currently defaults to an A-share short-term assistant for liquid
mainboard-style symbols. The live path is intentionally stricter than the
research path: when execution-grade intraday data is missing, the system must
degrade visibly instead of producing a buy signal.

## Live Data Chain

1. `config/watchlist.mainboard_liquid.csv`
   - Default stock universe.
   - Default buyable prefixes: `000`, `001`, `002`, `003`, `600`, `601`, `603`,
     `605`.
   - ChiNext `300/301` is not included in the default live package.
   - Built from buyable prefixes and BaoStock liquidity filters.
   - `notes` keeps liquidity evidence such as `avg20_amount` and `last_amount`.

2. Yahoo daily bars
   - Used by `short_term_live_monitor.py` for daily trend, volume/value
     expansion, ATR, moving averages, market temperature, and sector context.
   - This is a discovery and filtering source, not an execution source.

3. BaoStock 5-minute bars
   - Used for VWAP, entry-window checks, gap checks, sell-side monitoring, and
     T+1 execution logic.
   - Cache hits are valid only when the cached content covers the requested
     trading date. File names alone are not trusted.
   - Overlapping cache files are merged and de-duplicated before any remote
     request. Missing weekdays are fetched in compact date ranges.
   - Backtests pass the daily-bar trading calendar into 5-minute prefetch, so
     exchange holidays are not treated as missing intraday coverage.
   - Future end dates are clamped to the current date so backtests do not create
     misleading cache files for unavailable future bars.
   - BaoStock login is lazy and query failures caused by a dropped login session
     are retried after re-login.
   - If BaoStock has no 5-minute bars for a candidate, the candidate cannot
     become `BUY_TRIGGER`.

4. Sina quote fallback
   - Used only when BaoStock 5-minute bars are unavailable for today's scan.
   - Provides latest-price visibility only.
   - Quote-only rows are marked `QUOTE_ONLY`; they do not carry VWAP, target,
     stop, Edge ranking, or `BUY_NOW` eligibility.

5. Event radar JSON
   - `short_term_live_monitor.py` auto-loads `output/tech_event_radar_YYYYMMDD.json`
     for the scan date, then falls back to the latest dated radar file.
   - Event scores older than `--max-event-age-days` are disabled by default.
   - The live report prints event source path, status, and age.

## Source Roles

| Source | Role | Can Trigger Buy? | Main Failure Mode | Required Degrade |
|---|---|---:|---|---|
| Watchlist CSV | Universe and liquidity scope | No | stale pool | regenerate liquidity pool |
| Yahoo daily | setup discovery | No | delayed/missing bars | skip symbol or lower confidence |
| BaoStock 5m | execution confirmation | Yes | no bars or stale cache | `DATA_UNAVAILABLE` |
| Sina quote | latest-price fallback | No | delayed/blocked quote | `DATA_UNAVAILABLE` |
| Event JSON | catalyst bonus | No | stale file | `stale_disabled` |
| Xueqiu/CNInfo/RSS radar | research catalyst discovery | No direct live trigger | WAF/noisy text | low weight or disabled |

## Live Report Status

`short_term_live_monitor.py` writes these data-health fields:

- `Event score source`: the JSON used for event scores, or `-`.
- `status`: `ok`, `empty`, `missing`, or `stale_disabled`.
- `age_days`: age inferred from `tech_event_radar_YYYYMMDD.json`.
- `Intraday data status`: `ok`, `partial_quote_only`, `unavailable`, or
  `not_applicable` in daily mode.
- `Quote fallback`: `sina` or `none`.

Interpretation:

- `ok`: 5-minute bars are available for the reported candidates.
- `partial_quote_only`: at least one candidate has only a real-time quote
  fallback. Do not trade those rows from this report.
- `unavailable`: no execution-grade intraday data is available for selected
  candidates. Buy-side target, stop, and ranking are disabled.

## Operational Contract

Before using the assistant during a trading day:

1. Refresh the event radar if catalysts matter for the session.

```powershell
python .\tech_event_radar.py --watchlist .\config\watchlist.mainboard_liquid.csv --out .\output\tech_event_radar_YYYYMMDD.md --json-out .\output\tech_event_radar_YYYYMMDD.json
```

2. Run the live assistant.

```powershell
python .\local_trading_assistant.py --once
```

3. Treat these as hard stop states:

- `DATA_UNAVAILABLE`
- `QUOTE_ONLY`
- `Intraday data status: unavailable`
- `Event score source status: stale_disabled` when your thesis depends on events

4. Regenerate the liquidity watchlist after major universe changes or after a
long market regime change.

```powershell
python .\tools\filter_watchlist_by_baostock_liquidity.py
```

## Research Sources

The broader radar code still supports CNInfo, RSS/IR pages, Xueqiu social
attention, and older U.S. event workflows. These are research/discovery inputs.
They should not bypass the live A-share execution contract above.

Xueqiu remains low weight because discussion data can be delayed, promotional,
or blocked by WAF. If Xueqiu is unavailable, the live assistant should continue
with market data and explicit source status rather than silently assuming social
attention is zero.
