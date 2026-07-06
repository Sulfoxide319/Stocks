# Release 0.4.42

## Changes

- Adds a desktop `单股分析` tab for standalone stock diagnosis.
- The analysis reuses the live short-term rules for daily score, VWAP confirmation, target upper, first management line, hard stop, trailing-stop reference, historical hit-rate context, quality score, and suggested capital percentage.
- When buy cost is provided, the same position-management rules used by registered holdings are applied without writing back to the position store.
- Non-default buy-universe tickers remain blocked for new buy suggestions, but can still be diagnosed for holding/sell management when a cost is supplied.

## Validation

- `python -m py_compile single_stock_analysis.py desktop_app.py local_trading_assistant.py trading_assistant_app.py`
- `python desktop_app.py --smoke-test`
- Manual single-stock diagnostic smoke run for `600363` with a sample cost.
