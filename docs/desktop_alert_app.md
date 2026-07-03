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

## Window Layout

- Left sidebar: current state, phase, last scan time, next scan time, and action counters.
- Main tabs:
  - `总览`: buy and sell advice together.
  - `买入`: only buy-side candidates and triggers.
  - `卖出/持仓`: registered positions and sell-side rules.
- Detail panel: click any row to see the trigger/cost, target, stop, edge, PnL, and reason.
- Action buttons:
  - `启动`: run automatically in trading windows.
  - `停止`: stop automatic scans.
  - `立即扫描`: run one scan now.
  - `打开最新计划`: open the Markdown report.
  - `编辑持仓 CSV`: open or create the local position file.
  - `测试弹窗`: verify popup and sound behavior.

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
