# Release 0.4.45

## Changes

- Upgrades the popup budget system from single-trade budget to account-level sizing:
  - total assets = available cash + current holdings value
  - target position value = total assets * suggested capital percentage
  - existing same-ticker holdings reduce the new buy quantity
  - available cash and Guoshengrui `最大可买` cap the final quantity
  - quantity is rounded down to 100-share lots
- Adds Guoshengrui holdings synchronization:
  - exports `资金股份查询.txt`
  - parses available cash, holdings value, total assets, and position rows
  - updates/adds local positions without deleting positions not shown in the export
  - stores cash and holdings value as the default budget-system inputs
- Keeps the execution boundary unchanged: no automatic order submission, no price fill, no Enter/confirm key.

## Verification

- `python -m py_compile guoshengrui_bridge.py trade_quantity.py desktop_app.py trading_assistant_app.py app_storage.py`
- `python desktop_app.py --smoke-test`
- Runtime Guoshengrui V1.49 checks:
  - holdings export parsed 2 rows
  - available cash `42412.32`
  - holdings value `35694.00`
  - total assets `78106.32`
  - buy quantity fill with cash `10000`, holdings `90000`, suggested capital `20%` filled `200` shares without submitting
