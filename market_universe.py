#!/usr/bin/env python3
"""Buyable universe filters for local strategy scripts."""

from __future__ import annotations

from typing import Any


DEFAULT_BUYABLE_PREFIXES = ("600", "300", "301")


def parse_allowed_prefixes(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_BUYABLE_PREFIXES
    text = raw.strip()
    if not text:
        return DEFAULT_BUYABLE_PREFIXES
    if text.lower() in {"all", "*", "none", "off"}:
        return ()
    return tuple(item.strip() for item in text.split(",") if item.strip())


def is_buyable_ticker(ticker: str, prefixes: tuple[str, ...] = DEFAULT_BUYABLE_PREFIXES) -> bool:
    if not prefixes:
        return True
    code = ticker.strip()[:6]
    return any(code.startswith(prefix) for prefix in prefixes)


def filter_symbols(symbols: list[Any], raw_prefixes: str | None = None) -> list[Any]:
    prefixes = parse_allowed_prefixes(raw_prefixes)
    if not prefixes:
        return symbols
    return [symbol for symbol in symbols if is_buyable_ticker(str(getattr(symbol, "ticker", "")), prefixes)]
