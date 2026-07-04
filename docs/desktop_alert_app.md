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

- Automatic pre-open focus scan starts during `09:00-09:30` once per trading day.
- Optional Windows startup registration keeps the desktop assistant running after sign-in.
- During trading hours, the desktop app refreshes the latest scan candidates with lightweight real-time quotes.
- Candidate trigger adjustment, near-threshold distance, normal interval, and fast interval are configurable in the left sidebar. Near-threshold candidates automatically switch monitoring to the fast interval.
- The default near-threshold baseline is `0.25%`, with a dynamic lower bound of 3 A-share ticks. This is based on local mainboard 5-minute cache statistics where median 5-minute absolute movement was about `0.18%` and median 10-minute movement was about `0.24%`.
- Buy triggers and urgent sell actions show a topmost popup, with same-day duplicate alerts suppressed.
- The sidebar quick position entry writes ticker, buy price, and share count directly into the local SQLite position store.
- Daily history scans fall back to BaoStock after repeated Yahoo 403 responses, so restricted Yahoo access does not stall the whole stock pool scan.
- Weekend/manual scans use the closed-market daily path instead of intraday quote fallback; `QUOTE_ONLY` rows keep reference trigger/target/stop prices but still block buy confirmation until 5-minute/VWAP data exists.
- On weekends, the app skips stale opening/intraday/pre-close cached snapshots and loads the latest closed-market snapshot instead.
- Scan reports and live logs include a filter funnel so it is clear how a large stock pool is reduced to final candidates.
- Opening focus scan during `09:20-09:45`
- 2-minute intraday buy/sell checks during `09:45-11:30` and `13:00-14:45`
- Pre-close review during `14:45-15:05`
- Post-close local journal archive during `15:05-15:30`
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

The local assistant writes scan history to:

```text
output/trading_assistant/trading_journal.sqlite
```

The journal is local state. It separates system advice from future manually
confirmed fills in the `actual_trades` table.

After the close, publish the latest daily plan to GitHub:

```powershell
python nightly_publish.py --pull --branch main
```

This publishes only the latest advice files. It does not publish `config/live_positions.csv` or Xueqiu cookies.
