# Release 0.4.51

## Changes

- Holding management table now shows derived management outputs for each position:
  - `第一管理线`
  - `移动止盈`
  - `管理结果`
- These values are calculated from the position's own cost, target upper, highest price, trailing percentage, and broker-synced market value.
- Selecting a position row still fills the editable form correctly; derived columns are not written back into the database.

## Example

For `000725` with cost `2.3448`, target `8.7930`, highest/current `8.38`:

- first management line is calculated from cost and target.
- management result shows that the position has already passed the first management line and should be managed as profit protected.
