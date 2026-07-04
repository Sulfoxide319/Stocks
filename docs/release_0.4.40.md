# Stocks Trading Assistant 0.4.40

## Longer Live Scan Timeout

- Extends the candidate monitor subprocess timeout from `300` seconds to `900` seconds by default.
- Adds `--monitor-timeout-seconds` so the scan timeout can be tuned without changing code.
- Keeps `--focus-interval-seconds=300` unchanged because that controls the next scan interval, not the current scan timeout.
- Desktop "scan now" and auto-scan paths inherit the new default through the shared local assistant argument parser.

## Validation

Smoke checks:

```powershell
python -m py_compile local_trading_assistant.py desktop_app.py
python desktop_app.py --scan-prepare-test
python -c "from local_trading_assistant import build_arg_parser; print(build_arg_parser().parse_args(['--once']).monitor_timeout_seconds)"
```

Expected parser default:

```text
900
```
