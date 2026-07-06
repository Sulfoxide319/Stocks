# Release 0.4.55

## Changes

- Holding rows are now scanned during live candidate watch:
  - live watch refreshes sell-side holding prices, PnL, and sell trigger state.
  - holding rows can trigger existing sell alerts such as hard stop, target upper, and trailing stop.
  - scan-time holding management falls back to Sina real-time quotes when BaoStock 5-minute bars are unavailable, so holdings no longer show `0.00` just because VWAP data is missing.
- Strict buy scans now keep an observe-only fallback pool:
  - if strict filters leave too few candidates, the monitor fills up to `--min-observation-candidates` rows, default `8`.
  - observe-only rows are marked `OBSERVE_ONLY`/`WATCH_ONLY`, have `buy_enabled=false`, and `suggested_capital_pct=0`.
  - observe-only rows are still watched live, but never promoted to `BUY_NOW`.
- Live watch now handles three paths separately:
  - enabled buy candidates can still become `BUY_NOW`.
  - disabled observe-only candidates update latest price and distance only.
  - holding rows update latest price and sell trigger state.
  - all listed buy/observe rows are watched; the old 30-row internal watch cap no longer drops visible rows.

## Package

- `dist\StocksTradingAssistant-v0.4.55.zip`
- SHA256: `9CC9BBC7AE652AA6B5D796DD905B36911A2F7EB01B8EEB0AC191938EE44BB4AE`

## Validation

- `python -m py_compile short_term_live_monitor.py local_trading_assistant.py desktop_app.py tools\desktop_functional_smoke.py`
- `python tools\desktop_functional_smoke.py`
- sell quote fallback smoke:
  - no BaoStock 5-minute bars.
  - mocked real-time quote `8.8`.
  - cost `10.0`, hard stop `9.0`.
  - result: `SELL_NOW`, latest price `8.8`, reason includes real-time quote fallback.
- observe-only advice smoke:
  - raw action `OBSERVE_ONLY`.
  - result action `WATCH_ONLY`.
  - `buy_enabled=false`, `suggested_capital_pct=0`.
- real monitor smoke:
  - command: `python short_term_live_monitor.py --today 2026-07-06 --history-timeout 2 --dynamic-params --skip-hot-entries --out output\observe_pool_smoke.md --csv-out output\observe_pool_smoke.csv --top 8 --min-observation-candidates 8`
  - strict daily pass count: `0`.
  - final candidates: `8`.
  - all rows: `OBSERVE_ONLY`.
  - all suggested capital: `0.0`.
  - bad `300/301`: `0`.

## Notes

- Observe-only rows are deliberately not buy advice. They exist so the user can see the nearest misses and let the live watcher track them.
- VWAP-based sell checks still require real 5-minute bars. Real-time quote fallback only supports price-based holding checks such as hard stop, target upper, first management line, and trailing stop.
