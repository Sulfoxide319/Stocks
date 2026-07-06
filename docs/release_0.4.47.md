# Release 0.4.47

## Changes

- Guoshengrui holdings sync now detects long-held winning positions whose current/highest price is already above the old cost-based target.
- Such positions are repriced from the current holding high instead of cost:
  - target upper = holding high × 1.10
  - protective stop = holding high × `(1 - trailing_stop_pct)`
  - management state becomes `PROFIT_PROTECTED` when the first management line has already been reached.
- Normal or losing positions still keep the original cost-based default lines.

## Example

- `000725` cost `2.3448`, high/current `8.38`
- old target/stop: `2.5793 / 2.2510`
- new target/stop: `9.2180 / 8.1286`
