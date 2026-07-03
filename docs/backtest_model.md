# Tech Event Backtest Model

Purpose: test whether the event radar can produce profitable 1-3 day short-term trades over a recent one-month window.

This is a historical simulation. It is not a prediction and not a trading recommendation.

## Timeline Contract

The backtest avoids lookahead bias:

1. Event data is considered available on its filing or publication date.
2. The score is computed using only that event and price bars up to the signal date.
3. The simulated trade enters on the next trading day's open.
4. The position exits by stop-loss, take-profit, or the fixed holding window.

## Default Trading Cycle

- Holding cycle: 3 trading days.
- Entry: next trading day open after the signal date.
- Exit priority: stop-loss first, then take-profit, then time exit.
- If stop and take-profit both touch on the same daily bar, the stop is assumed first.
- This is conservative because daily candles do not reveal intraday path.

## Optimized Parameters

The optimizer tests a small grid:

- Minimum event score: `35,45,55,65,75,85`
- Take-profit: `3%,5%,8%,10%`
- Stop-loss: `1%,2%,3%,5%`
- Maximum simultaneous positions: `1,2,3`

Run:

```powershell
python .\tech_event_backtest.py --start-date 2026-06-02 --end-date 2026-07-02
```

## Time-Weighted Objective

Default objective:

```text
objective = time_weighted_return * (hold_days / average_holding_trading_days)
            - drawdown_penalty * max_drawdown
```

This favors rules that:

- make money,
- use capital for fewer days,
- avoid large drawdowns.

Alternative objectives:

```powershell
python .\tech_event_backtest.py --objective total_return
python .\tech_event_backtest.py --objective return_per_exposure
```

## Outputs

The script writes:

- Markdown summary: `output/tech_event_backtest_START_END.md`
- JSON details: `output/tech_event_backtest_START_END.json`
- Best-rule trades: `output/tech_event_backtest_START_END_trades.csv`

## Short-Term Opportunity Screen

Use this when the goal is to find stocks with enough short-term elasticity to plausibly move 10% in 1-5 trading days:

```powershell
python .\short_term_opportunity.py --today 2026-07-02 --min-score 55 --top 20
```

It ranks the watchlist by:

- traded value and traded value expansion,
- recent 3-day and 5-day high-low range,
- ATR percentage,
- trend versus MA5/MA20,
- latest event score from the event radar.

Output:

- `output/short_term_opportunity_YYYYMMDD.md`
- `output/short_term_opportunity_YYYYMMDD.csv`

Interpretation:

- `READY_WATCH`: liquid, volatile, and close enough to recent highs. Wait for intraday trigger.
- `WATCH_FOR_TRIGGER`: eligible, but needs confirmation such as value expansion, reclaim of VWAP, or reclaim of MA5/MA20.
- `EVENT_PLUS_VOLATILITY`: event-backed volatility candidate.
- `HIGH_VOLATILITY`: pure volatility candidate; requires stricter stop discipline.

## Pattern Mining And Active Exits

Use this to learn what recent short-term winners had in common:

```powershell
python .\short_term_pattern_miner.py --start-date 2026-06-02 --end-date 2026-07-02 --horizon 3
```

It labels every stock-date sample by whether the next 3 trading days touched +10%, then simulates exits:

- take profit at +10%,
- hard stop at -4%,
- trailing stop after a 4% favorable move,
- otherwise time exit.

Output:

- `output/short_term_patterns_START_END.md`
- `output/short_term_patterns_START_END.csv`

In the 2026-06-02 to 2026-07-02 sample, `EVENT_PLUS_VOLATILITY` had the highest +10% hit rate, while `BACKGROUND_WATCH` had the lowest. This supports a practical rule: prefer event-backed volatility, then use active exits instead of holding blindly to the close.

## Dynamic Exit Optimization

Fixed `+10%` take profit is too rigid. The dynamic strategy computes exit thresholds from the stock's current volatility:

```text
target = ATR% * target_atr_mult
       + recent_5d_range% * target_range_mult
       + event_bonus

hard_stop = ATR% * stop_atr_mult
trailing_stop = ATR% * trail_atr_mult
```

Each threshold is bounded by min/max values so that it does not become absurdly tight or loose.

Run one dynamic backtest:

```powershell
python .\short_term_strategy_backtest.py `
  --start-date 2026-06-02 --end-date 2026-07-02 `
  --dynamic-exit --max-positions 2 --min-score 65 `
  --target-atr-mult 0.9 --target-range-mult 0.35 `
  --stop-atr-mult 0.4 --trail-atr-mult 0.35
```

Optimize the parameter grid:

```powershell
python .\optimize_short_term_strategy.py --start-date 2026-06-02 --end-date 2026-07-02
```

The optimizer sorts by:

```text
objective = total_return - drawdown_penalty * max_drawdown
```

In the 2026-06-02 to 2026-07-02 sample, the best tested dynamic rule returned about `17.02%` with max drawdown around `5.28%`.

## Add-On Rules

The strategy supports pyramiding, but only into winning positions:

```powershell
python .\short_term_strategy_backtest.py `
  --start-date 2026-06-02 --end-date 2026-07-02 `
  --dynamic-exit --allow-add `
  --add-on-profit 0.04 --add-size-factor 0.5
```

Rules:

- never average down,
- add only when an existing same-ticker leg is profitable by the configured threshold,
- add size is smaller than the initial leg,
- total open legs remain capped by `--max-positions`.

In the tested 30-day sample, increasing max positions from 2 to 3 reduced return. The best observed result stayed with dynamic exits and `max_positions=2`, not with pyramiding.

## Interpretation

If the best trading rule has negative return, the correct conclusion is not "force a trade." It means this event-score setup did not beat cash over that window.

One month is a small sample. Treat any optimized rule as a hypothesis, then test it on a later out-of-sample window before using real money.
