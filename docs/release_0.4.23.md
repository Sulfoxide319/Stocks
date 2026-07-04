# Stocks Trading Assistant 0.4.23

## Display Clarification

- Renames the desktop buy-advice target column to `目标上沿`.
- Adds a first-management-line note to buy advice reasons, e.g. `目标价是上沿，先看xx.xx管理线`.
- Marks score-only observation rows as not trading against the displayed target.

## Validation Note

The v0.4.22 strict 12M ledger hit the full take-profit target in only `4/113`
closed trades (`3.54%`). The more useful validation metric is whether a trade
first made a strong favorable move and then exited by target or trailing stop:
`40/113` (`35.4%`) in the full 12M ledger, and `4/8` (`50.0%`) in the current
`narrow_rally` market-state sample.

This release does not change the trading strategy or backtest results; it only
reduces target-price ambiguity in the UI and exported reports.

