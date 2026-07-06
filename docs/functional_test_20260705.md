# Desktop Functional Test Report - 2026-07-05

## Scope

- Runtime position onboarding after a buy signal.
- Manual mid-session position entry.
- Broker holdings sync into SQLite.
- Trigger popup interaction design.
- Same-day duplicate popup suppression.
- Runtime positions exported into the next scan's sell-rule input.

## Findings

### Fixed

- After quick-registering a bought stock, `selected_position_id` remained set.
  If the user then typed a different ticker in the position form without first
  clicking `新建`, the form updated the previous position instead of creating a
  new one.
- Trigger popups had trade buttons, but their ticker/name cells did not share
  the main table's shortcut behavior. Popup ticker cells now copy/jump to
  Guoshengrui, and popup name cells open Xueqiu.

### Verified

- Selecting a buy row fills the quick position ticker and latest price.
- `同步持仓库` writes an open SQLite position with advice-derived target/stop
  management lines.
- Manual entry with a different ticker creates a separate position even if a
  previous position is selected.
- Exporting open positions for the next scan includes runtime-added holdings.
- Broker sync updates an existing ticker instead of duplicating it and refreshes
  account cash/holdings/total-assets settings.
- Popup rows are topmost, state that orders are not auto-submitted, load broker
  account defaults, expose per-row trade buttons, and pass existing holdings to
  buy quantity planning.
- Replaying the same payload on the same date does not show duplicate buy/sell
  popups.

## Automated Coverage

```powershell
python tools\desktop_functional_smoke.py
```

The smoke test runs against a temporary `%LOCALAPPDATA%` root and fake broker
bridge functions, so it does not modify real local positions, Windows startup
settings, or the live Guoshengrui client.
