# Stocks Trading Assistant 0.4.26

## Sell Signal Points

- Holding/sell advice now exposes all practical sell signal points, not only the triggered action.
- Added sell-side fields:
  - `trailing_stop_price`
  - `vwap_fail_price`
  - `signal_points`
- Desktop advice table now shows `移动止盈` and `VWAP/成本` columns.
- Sell popups include the full signal point summary, including:
  - target upper
  - first management line
  - trailing stop line
  - hard stop
  - VWAP/cost weak line
  - pre-close weak condition

## Validation

Scenario checks passed for:

- hard stop: `SELL_NOW`
- target upper: `TAKE_PROFIT`
- first management line: `MANAGE_PROFIT`
- post-management VWAP weakness: `REDUCE_PROFIT`
- post-management trailing weakness: `REDUCE_PROFIT`
- VWAP/cost weakness: `VWAP_WEAK_SELL`
