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
