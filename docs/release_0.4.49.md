# Release 0.4.49

## Packaging Safety

- Release packages now copy only managed config templates and watchlists.
- Local user-owned files are excluded from the zip and update manifest:
  - `config/xueqiu_cookie.txt`
  - `config/live_positions.csv`
  - `config/broker_account_snapshot.json`
  - `config/ui_settings.json`
- The installer/updater preserves those user-owned files if an older manifest
  listed them, instead of deleting or overwriting local state during upgrades.

## Desktop Fallback Fix

- The legacy Tk desktop app now uses a two-column action panel so the lower
  buttons stay visible at the supported minimum window size.
- The Tk event loop now cancels its queue polling timer on window destruction,
  removing teardown noise from GUI smoke tests.

## Validation

- `python -m compileall -q app_storage.py broker_position_sync.py desktop_app.py guoshengrui_bridge.py local_trading_assistant.py single_stock_analysis.py trade_quantity.py trading_assistant_app.py short_term_live_monitor.py`
- `python tools/gui_layout_smoke.py`
- `python tools/audit_release_package.py dist/StocksTradingAssistant-v0.4.49.zip --expected-version 0.4.49`
