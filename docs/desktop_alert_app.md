# Desktop Alert App

`trading_assistant_app.py` is the intraday local application.

It keeps the 2-minute trading loop local and separates it from nightly GitHub publishing.

## Start

```powershell
python trading_assistant_app.py
```

Or double-click:

```text
run_trading_app.bat
```

## What It Does

- Opening focus scan during `09:20-09:45`
- 2-minute intraday buy/sell checks during `09:45-11:30` and `13:00-14:45`
- Pre-close review during `14:45-15:05`
- Reads latest plan JSON from `output/trading_assistant/latest_plan.json`
- Shows a topmost popup only for actionable trading events

## Popup Actions

- `BUY_NOW`: buy trigger is active
- `SELL_NOW`: hard stop
- `TAKE_PROFIT`: target reached
- `TRAIL_SELL`: trailing stop
- `VWAP_WEAK_SELL`: weak VWAP sell condition
- `PRE_CLOSE_REDUCE`: reduce weak position before close

`HOLD_T1`, `HOLD`, `WAIT`, and `WATCH_BUY` stay in the main window but do not trigger a popup.

## Nightly Publish

After the close, publish the latest daily plan to GitHub:

```powershell
python nightly_publish.py --pull --branch main
```

This publishes only the latest advice files. It does not publish `config/live_positions.csv` or Xueqiu cookies.
