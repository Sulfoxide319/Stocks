# 5-minute Live Advisor

This backend-only monitor runs the current T+1 dynamic-regime A-share model and writes advice reports.

## One scan

```powershell
python live_advisor_daemon.py --once
```

Outputs:

- `output/live_advice/live_advice_YYYYMMDD_HHMMSS.md`
- `output/live_advice/live_advice_YYYYMMDD_HHMMSS.csv`
- `output/live_advice/latest.md`
- `output/live_advice/latest.csv`

## Run every 5 minutes

```powershell
python live_advisor_daemon.py --interval-seconds 300 --market-hours-only
```

## Publish by GitHub commit

Run this inside a git repo with a configured remote:

```powershell
python live_advisor_daemon.py --interval-seconds 300 --market-hours-only --github-mode commit
```

Optional:

```powershell
python live_advisor_daemon.py --interval-seconds 300 --market-hours-only --git-pull-before-scan --github-mode commit --git-branch main
```

## Publish as a GitHub issue

Requires GitHub CLI login:

```powershell
gh auth login
python live_advisor_daemon.py --once --github-mode issue --github-issue-title "A-share live advice"
```

## Current dynamic model

- A-share T+1 execution.
- Default buyable universe: liquid mainboard-style symbols with prefixes
  `000`, `001`, `002`, `003`, `600`, `601`, `603`, `605`.
- Hot market: skip new entries.
- Normal market: trade with current main filters.
- Narrow-rally market: expand the observation pool when breadth is weak but
  short-term returns are positive; strict buy execution remains high-score only.
- Cold market: allow entries with reduced risk sizing in strict backtests.
- Entry window: `09:45` to `11:20`.
- Buy confirmation requires both the trigger and buffered VWAP.
- Positive open gap requires signal-day value ratio `>= 1.30`.
- Main filters: 5-day range `<= 32`, 10-day momentum `<= 26`, 20-day position `<= 85`.
- Default observation score is `>= 83` in narrow-rally scans, but automatic buy
  eligibility still requires `buy_min_score >= 90`.
