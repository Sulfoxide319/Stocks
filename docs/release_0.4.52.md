# Release 0.4.52

## Changes

- Profit protection now gives a real management result based on the latest synced price and holding high.
- Long-running winners keep a cost-anchored target ladder, but reaching that target no longer means automatic full take-profit after the position is already managed.
- Managed winners now roll forward:
  - target upper = next cost-based profit ladder.
  - dynamic protection line = max(cost-ladder protection, recent/high trailing protection, existing valid protection).
  - management result shows latest price, protection line, distance to protection, and locked profit.
- Live sell logic now treats managed target hits as `MANAGE_PROFIT`/rolling protection, not `TAKE_PROFIT`; only a break below the dynamic protection line triggers profit reduction.

## Example

For `000725` cost `2.3448`, latest/high `8.38`:

- current gain: about `257%`
- next cost ladder target: `8.7930`
- dynamic protection line: `8.2068`
- management result: keep the strong position while price stays above protection; roll target/protection higher if it keeps rising.
