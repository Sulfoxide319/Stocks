# Release 0.4.43

## Changes

- Adds Guoshengrui ticker jump integration for desktop table ticker clicks.
- Clicking a ticker now copies the 6-digit code, focuses or launches `C:\zd_gszq_gm\TdxW.exe`, pastes the code, and sends Enter.
- Xueqiu behavior is unchanged: clicking a stock name still opens the Xueqiu stock page.
- The bridge uses local Windows window automation only. It does not call trading APIs and does not read or store Guoshengrui credentials or trading data.
- The bridge stops before input when a foreground window title looks like an order/trading dialog.

## Validation

- `python -m py_compile guoshengrui_bridge.py desktop_app.py trading_assistant_app.py`
- `python desktop_app.py --smoke-test`
- Runtime bridge check found `TdxW_MainFrame_Class`.
- Manual bridge run returned success for `600000`.
