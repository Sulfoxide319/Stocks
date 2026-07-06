# Release 0.4.46

## Changes

- Budget sizing now accepts broker-scanned total assets directly from Guoshengrui's funds/positions export.
- Buy quantity calculation uses scanned total assets as the target-position base, with available cash and broker `最大可买` still acting as hard caps.
- Trade popups now show an editable total-assets field and a `扫描账户` action to refresh cash, holdings value, total assets, and local positions before opening the flash buy window.
- Guoshengrui holdings sync persists `trade_total_assets` for the PySide app and writes `config/broker_account_snapshot.json` for the Tkinter app.

## Safety

- The bridge still does not fill price, press final submit, or interact with order-confirmation controls.
- If scanned total assets are unavailable, sizing falls back to `cash + holdings value`.
