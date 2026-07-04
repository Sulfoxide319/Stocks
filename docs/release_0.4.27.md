# Stocks Trading Assistant 0.4.27

## Holding Management State

- Persisted a holding management state machine for live sell advice:
  - `OPEN`
  - `FIRST_MANAGE_HIT`
  - `PROFIT_PROTECTED`
  - `REDUCED`
  - `EXITED`
- The first management line now only emits `MANAGE_PROFIT` on the first touch.
- After that first touch, repeated scans show `HOLD_MANAGED` unless a real protection signal appears.
- Profit protection signals still remain urgent:
  - `REDUCE_PROFIT`
  - `TRAIL_SELL`
  - `VWAP_WEAK_SELL`
  - `PRE_CLOSE_REDUCE`
  - `SELL_NOW`
  - `TAKE_PROFIT`
- Desktop holding management now shows the current management state in both advice rows and position rows.
- SQLite positions are migrated automatically with management timestamps and last signal fields.
- CSV position sync now preserves management state, timestamps, and last signal metadata.

## Validation

- Syntax checks passed for:
  - `app_storage.py`
  - `local_trading_assistant.py`
  - `desktop_app.py`
- SQLite migration and CSV round-trip passed for management state fields.
- Sell-state scenario checks passed:
  - first touch: `OPEN` -> `FIRST_MANAGE_HIT` with `MANAGE_PROFIT`
  - repeated same-bar scan: `HOLD_MANAGED` without rewriting unchanged state
  - post-management VWAP weakness: `FIRST_MANAGE_HIT` -> `PROFIT_PROTECTED` with `REDUCE_PROFIT`
