# Local Trading Assistant

This program turns the signal model into a local trading-day assistant.

It does not place orders. It produces action advice:

- `BUY_NOW`: buy trigger is active
- `WATCH_BUY`: candidate is valid but price has not confirmed yet
- `NO_BUY`: do not open a new position
- `SELL_NOW`: hard stop
- `TAKE_PROFIT`: target reached
- `TRAIL_SELL`: trailing stop
- `VWAP_WEAK_SELL`: weak VWAP sell condition
- `PRE_CLOSE_REDUCE`: reduce weak position before close
- `HOLD_T1`: bought today, cannot sell today under A-share T+1
- `HOLD`: no sell condition

## Run Once

```powershell
python local_trading_assistant.py --once
```

If `requests` or `baostock` is missing, the assistant will install project
dependencies from `requirements.txt` automatically before running. Disable this
with:

```powershell
$env:STOCKS_SKIP_AUTO_INSTALL='1'
```

Force a phase:

```powershell
python local_trading_assistant.py --once --phase opening
python local_trading_assistant.py --once --phase intraday
python local_trading_assistant.py --once --phase preclose
python local_trading_assistant.py --once --phase postclose
```

## Run All Day

```powershell
python local_trading_assistant.py --beep
```

Default schedule:

- `09:20-09:45`: opening focus scan
- `09:45-11:30`: intraday 2-minute buy/sell scan
- `13:00-14:45`: intraday 2-minute buy/sell scan
- `14:45-15:05`: pre-close review
- `15:05-15:30`: post-close archive

## Daily Loop

Recommended operating flow:

1. Before the open, generate today's focus list. This is a watchlist, not a buy signal.
2. After `09:45`, poll every 2 minutes for VWAP, trigger, gap, and risk checks.
3. During `14:45-15:05`, prioritize sell-side and weak-position handling.
4. After the close, write the final plan and all advice events to the local journal database.

## Position File

Create the local position file:

```powershell
copy .\config\live_positions.example.csv .\config\live_positions.csv
notepad .\config\live_positions.csv
```

Columns:

- `ticker`: stock code
- `name`: stock name
- `buy_date`: `YYYY-MM-DD`
- `buy_time`: buy time
- `buy_price`: cost
- `shares`: optional
- `target_price`: take-profit price
- `hard_stop_price`: hard stop price
- `trailing_stop_pct`: trailing stop percent, for example `3`
- `highest_price`: highest observed price since entry
- `status`: `open`

`config/live_positions.csv` is ignored by git.

## Outputs

- `output/trading_assistant/latest_plan.md`
- `output/trading_assistant/latest_plan.csv`
- `output/trading_assistant/latest_plan.json`
- `output/trading_assistant/trading_journal.sqlite`

The SQLite journal stores:

- `assistant_runs`: one row per scan.
- `advice_events`: every buy/sell advice row from each scan.
- `daily_archives`: one post-close archive row per trade date.
- `actual_trades`: reserved for manually confirmed fills or broker import.

Disable database writes when testing:

```powershell
python local_trading_assistant.py --once --no-db
```

## Optional GitHub Update

```powershell
python local_trading_assistant.py --beep --github-mode commit --git-pull-before-scan --git-branch main
```

This commits only the latest plan files, not your local position file.

## Desktop Popup App

For intraday use, prefer the desktop app:

```powershell
python trading_assistant_app.py
```

Or double-click:

```text
run_trading_app.bat
```

The app runs scans locally and opens a topmost popup only for actionable trading events:

- `BUY_NOW`
- `SELL_NOW`
- `TAKE_PROFIT`
- `TRAIL_SELL`
- `VWAP_WEAK_SELL`
- `PRE_CLOSE_REDUCE`

It does not publish to GitHub during the trading day. Use the nightly publisher after market close:

```powershell
python nightly_publish.py --pull --branch main
```
