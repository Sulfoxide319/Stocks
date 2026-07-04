# Stocks Trading Assistant 0.4.25

## Profit Management Prompt

- Adds first-management-line prompts to the holding/sell side.
- Existing positions do not need a database migration: the line is inferred from `buy_price` and `target_price`.
- New sell-side actions:
  - `MANAGE_PROFIT`: latest price reaches the first management line. This is a management prompt, not a forced sell.
  - `REDUCE_PROFIT`: price had reached the first management line, then falls below VWAP or trails down from the holding high.
- Desktop sell alerts now include `MANAGE_PROFIT` and `REDUCE_PROFIT`.
- Sell reports and JSON/CSV exports now include `first_manage_price` for holdings.

## Practical Meaning

`目标上沿` remains the optimistic upper bound. The first management line is now the practical alert point where the system tells you to protect profit, tighten risk, or consider a manual reduction.
