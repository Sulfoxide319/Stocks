# Release 0.4.44

## Changes

- Fixes Guoshengrui ticker clicks restoring a maximized window to normal size.
- Switches Guoshengrui input to prefix-aware key events:
  - `600/688/9xx` use numpad digits first to avoid period/function shortcuts.
  - `300xxx` use main-row digits first to avoid the `30` quote-list shortcut.
  - Suspicious quote-list results retry with the alternate key path.
- Keeps the clipboard fallback and Xueqiu name-click behavior unchanged.
- Success messages now include the Guoshengrui window title returned after input when available.

## Validation

- `python -m py_compile guoshengrui_bridge.py desktop_app.py trading_assistant_app.py`
- Runtime bridge checks on Guoshengrui V1.49:
  - `600363` -> `分析图表-联创光电`
  - `300059` -> `分析图表-东方财富`
  - `000001` -> `分析图表-平安银行`
  - `688981` -> `分析图表-中芯国际`
- During the runtime checks the Guoshengrui window stayed maximized with unchanged window bounds.
