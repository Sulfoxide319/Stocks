# Release 0.4.56

## Changes

- Fixes scan failure when `output` is not writable:
  - scan output directory is now probed with a real write test before use.
  - if the preferred output directory raises `PermissionError`, `FileExistsError`, or another `OSError`, the scan automatically falls back to the app-owned data directory.
  - fallback target: `%LOCALAPPDATA%\StocksTradingAssistant\data\output\trading_assistant`.
- Hardens path handling:
  - relative and absolute paths are resolved explicitly.
  - `git_publish` now ignores files outside the repository instead of failing on `relative_to(cwd)`.

## Package

- `dist\StocksTradingAssistant-v0.4.56.zip`
- SHA256: `E1E18CD50770FCB48B6648E465502A6E4CF7885C6D6CFAAB2637E2C96B2C74A9`

## Root Cause

The desktop app already passed an app-data output path in the normal packaged path, but some scan paths can still touch relative `output` locations or inherit a non-writable current directory. On Windows this can surface as:

```text
[WinError 5] 拒绝访问。: 'output'
```

The correct behavior is not to fail the scan. The assistant should use a user-writable app data output directory.

## Validation

- `python -m py_compile local_trading_assistant.py desktop_app.py tools\desktop_functional_smoke.py short_term_live_monitor.py`
- `python tools\desktop_functional_smoke.py`
- fallback smoke:
  - create a bad workspace `output` path.
  - call output preparation.
  - verify it switches to app data output and remains writable.
