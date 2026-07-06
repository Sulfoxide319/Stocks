# Release 0.4.50

## Functional Hardening

- Added a desktop functional smoke test for runtime position onboarding,
  broker sync, trigger popup actions, popup shortcut cells, and duplicate alert
  suppression.
- Fixed a mid-session position-entry bug where saving a different ticker after
  quick registration could overwrite the previously selected position.
- Trigger popups now match the main table's stock shortcuts:
  - click ticker to copy/jump Guoshengrui
  - click name to open Xueqiu

## Validation

- `python tools/desktop_functional_smoke.py`
- `python -m compileall -q desktop_app.py tools/desktop_functional_smoke.py`
- `python tools/audit_release_package.py dist/StocksTradingAssistant-v0.4.50.zip --expected-version 0.4.50`
- Temporary install check confirmed `StocksTradingAssistant.exe`, version `0.4.50`,
  `config/xueqiu_cookie.example.txt`, and preservation of an existing local
  `config/xueqiu_cookie.txt`.
