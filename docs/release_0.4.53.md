# Release 0.4.53

## Changes

- Guoshengrui holdings sync now reports the exact failure code in the popup instead of only showing a generic scan failure.
- Holdings export file discovery now searches common Guoshengrui/Tongdaxin export locations in addition to Documents and the current working directory:
  - user Documents / Desktop / Downloads
  - current working directory
  - `%TEMP%` / `%TMP%`
  - Guoshengrui install directory
  - `T0002/export` under the detected Guoshengrui install directory
- Output dialog matching is more tolerant across different Guoshengrui installations:
  - output dialog title can contain `输出`
  - buttons can match `输出` / `导出`
  - confirm buttons can match `确定` / `确认` / `保存` / `开始`
- Scan failures now include detected Guoshengrui blocking dialogs such as login, password, confirmation, or risk prompt windows.

## Validation

- `python -m py_compile guoshengrui_bridge.py desktop_app.py trading_assistant_app.py`
- Guoshengrui export path/parse smoke:
  - confirms `T0002/export/资金股份查询.txt` is found.
  - confirms cash and one holding row are parsed.
- `python tools/desktop_functional_smoke.py`
