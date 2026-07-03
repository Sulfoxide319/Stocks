# Stocks

A-share short-term signal research and live monitoring toolkit.

This project focuses on buyable A-share technology stocks under the current rule set:

- Stock universe: `600`, `300`, `301`
- No STAR Market or US stocks
- T+1 execution: positions bought today cannot be sold today
- Dynamic market regime filter: hot/normal/cold
- 5-minute BaoStock/VWAP execution layer
- Local-only Xueqiu cookie support

## Install

```powershell
pip install -r requirements.txt
```

Most command-line tools also auto-install missing Python dependencies from
`requirements.txt` on first run. To disable this behavior in managed
environments:

```powershell
$env:STOCKS_SKIP_AUTO_INSTALL='1'
```

## Live 5-Minute Monitor

Run once:

```powershell
python live_advisor_daemon.py --once
```

Run every 5 minutes during market hours:

```powershell
python live_advisor_daemon.py --interval-seconds 300 --market-hours-only
```

The latest advice is written to:

- `output/live_advice/latest.md`
- `output/live_advice/latest.csv`

## Local Trading Assistant

Run one local scan:

```powershell
python local_trading_assistant.py --once
```

Run the full local assistant:

```powershell
python local_trading_assistant.py --beep
```

Schedule:

- Opening focus: `09:20-09:45`
- Intraday buy/sell checks: every 2 minutes in `09:45-11:30` and `13:00-14:45`
- Pre-close review: `14:45-15:05`

The latest plan is written to:

- `output/trading_assistant/latest_plan.md`
- `output/trading_assistant/latest_plan.csv`
- `output/trading_assistant/latest_plan.json`

To let the assistant evaluate sell rules, copy `config/live_positions.example.csv` to `config/live_positions.csv` and fill your real/paper positions. `config/live_positions.csv` is ignored by git.

## Desktop App

The recommended desktop entry point is the packaged Windows app:

```text
StocksTradingAssistant.exe
```

It stores app-owned data in:

```text
%LOCALAPPDATA%\StocksTradingAssistant\data\assistant.sqlite
```

The desktop app provides:

- a scan button with progress and logs
- cached latest scan results on startup
- position management backed by SQLite
- CSV import for existing positions, without using CSV as the primary data store
- update checks from the app without opening a console window

The older Tkinter script is kept as a development fallback:

```powershell
python trading_assistant_app.py
```

Nightly GitHub publishing is separate from the intraday popup app:

```powershell
python nightly_publish.py --pull --branch main
```

## Release Installer

Tagged releases publish a Windows zip installer named
`StocksTradingAssistant-vX.Y.Z.zip`.

Build a package locally:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_release_package.ps1
```

Install from a release package:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-StocksTool.ps1
```

The installed `Start-TradingAssistant.bat` checks the latest GitHub Release
before launching and applies updates automatically when a newer version is
available. Desktop shortcuts created by the installer point directly to
`StocksTradingAssistant.exe`, so normal launch does not open a console window.

## Publish Latest Advice To GitHub

Commit and push the latest generated advice after each scan:

```powershell
python live_advisor_daemon.py --interval-seconds 300 --market-hours-only --git-pull-before-scan --github-mode commit --git-branch main
```

Create a GitHub issue instead:

```powershell
gh auth login
python live_advisor_daemon.py --once --github-mode issue --github-issue-title "A股短线实时建议"
```

## Xueqiu Cookie

Do not commit real cookies. Save them locally only:

```powershell
copy .\config\xueqiu_cookie.example.txt .\config\xueqiu_cookie.txt
notepad .\config\xueqiu_cookie.txt
```

`config/xueqiu_cookie.txt` is ignored by git.

## Main Files

- `live_advisor_daemon.py`: backend daemon that scans every 5 minutes
- `short_term_live_monitor.py`: one-shot live signal generator
- `intraday_vwap_backtest.py`: 5-minute/VWAP backtest and execution model
- `short_term_strategy_backtest.py`: daily signal simulation
- `baostock_intraday.py`: BaoStock 5-minute data cache/client
- `docs/live_advisor.md`: live monitor usage notes
