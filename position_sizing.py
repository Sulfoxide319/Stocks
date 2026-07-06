#!/usr/bin/env python3
"""Shared quality-aware position sizing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MIN_CAPITAL_FACTOR = 0.70
MAX_CAPITAL_FACTOR = 1.60
MAX_SINGLE_POSITION_PCT = 50.0
MAX_TOTAL_CAPITAL_PCT = 100.0
MIN_BUCKET_SAMPLE_SIZE = 8


@dataclass(frozen=True)
class PositionSizingResult:
    mode: str
    quality_score: float
    quality_grade: str
    capital_factor: float
    suggested_capital_pct: float
    reason: str


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def estimated_edge_score(
    score: float,
    target_pct: float,
    stop_pct: float,
    traded_value_ratio: float,
    momentum_3d_pct: float,
    distance_to_ma5_pct: float,
) -> float:
    probability = 0.35 + max(0.0, score - 85.0) * 0.006
    if traded_value_ratio >= 1.5:
        probability += 0.04
    if 2.0 <= momentum_3d_pct <= 12.0:
        probability += 0.03
    if distance_to_ma5_pct > 4.0:
        probability -= 0.08
    if distance_to_ma5_pct < -3.0:
        probability -= 0.05
    probability = min(0.68, max(0.22, probability))
    return (probability * target_pct - (1 - probability) * stop_pct) * 100


def score_component(score: float) -> float:
    return clamp((score - 88.0) / 10.0)


def edge_component(edge_score: float) -> float:
    return clamp((edge_score - 1.0) / 5.0)


def value_component(traded_value_ratio: float) -> float:
    return clamp((traded_value_ratio - 1.0) / 2.0)


def plateau_component(value: float, low: float, preferred_low: float, preferred_high: float, high: float) -> float:
    if value <= low:
        return clamp(value / low * 0.35) if low > 0 else 0.0
    if value < preferred_low:
        return 0.35 + (value - low) / max(0.0001, preferred_low - low) * 0.55
    if value <= preferred_high:
        return 1.0
    if value < high:
        return 1.0 - (value - preferred_high) / max(0.0001, high - preferred_high) * 0.60
    return 0.25


def heat_component(distance_to_ma5_pct: float, close_position_20d_pct: float) -> float:
    ma5_penalty = 0.70 * clamp((distance_to_ma5_pct - 2.0) / 6.0)
    pullback_penalty = 0.25 * clamp((-distance_to_ma5_pct - 2.0) / 5.0)
    high_position_penalty = 0.35 * clamp((close_position_20d_pct - 83.0) / 17.0)
    return clamp(1.0 - ma5_penalty - pullback_penalty - high_position_penalty)


def setup_component(setup_type: str) -> float:
    mapping = {
        "EVENT_PLUS_VOLATILITY": 1.0,
        "VOLUME_BREAKOUT": 0.88,
        "HIGH_VOLATILITY": 0.66,
    }
    return mapping.get(str(setup_type or "").upper(), 0.55)


def historical_component(
    first_manage_hit_rate_pct: float | None,
    target_upper_hit_rate_pct: float | None,
    target_upper_touch_rate_pct: float | None,
    sample_size: int,
) -> float | None:
    if sample_size < MIN_BUCKET_SAMPLE_SIZE:
        return None
    pieces: list[float] = []
    if first_manage_hit_rate_pct is not None:
        pieces.append(clamp((first_manage_hit_rate_pct - 20.0) / 45.0))
    if target_upper_touch_rate_pct is not None:
        pieces.append(clamp((target_upper_touch_rate_pct - 8.0) / 35.0))
    if target_upper_hit_rate_pct is not None:
        pieces.append(clamp((target_upper_hit_rate_pct - 5.0) / 30.0))
    if not pieces:
        return None
    return sum(pieces) / len(pieces)


def quality_grade(score: float) -> str:
    if score >= 0.78:
        return "A"
    if score >= 0.63:
        return "B"
    if score >= 0.48:
        return "C"
    return "D"


def quality_score_from_signal(
    *,
    score: float,
    setup_type: str,
    edge_score_value: float,
    traded_value_ratio: float,
    atr_pct: float,
    momentum_3d_pct: float,
    momentum_10d_pct: float,
    distance_to_ma5_pct: float,
    close_position_20d_pct: float,
    sector_momentum_5d_pct: float,
    first_manage_hit_rate_pct: float | None = None,
    target_upper_hit_rate_pct: float | None = None,
    target_upper_touch_rate_pct: float | None = None,
    hit_rate_sample_size: int = 0,
) -> tuple[float, str]:
    components = [
        ("score", score_component(score), 0.24),
        ("edge", edge_component(edge_score_value), 0.25),
        ("liquidity", value_component(traded_value_ratio), 0.12),
        ("volatility", plateau_component(atr_pct, 3.5, 4.5, 8.5, 13.0), 0.10),
        ("momentum", plateau_component(momentum_10d_pct, 3.0, 7.0, 16.0, 26.0), 0.10),
        ("heat", heat_component(distance_to_ma5_pct, close_position_20d_pct), 0.12),
        ("sector", clamp((sector_momentum_5d_pct + 2.0) / 8.0), 0.04),
        ("setup", setup_component(setup_type), 0.03),
    ]
    history = historical_component(
        first_manage_hit_rate_pct,
        target_upper_hit_rate_pct,
        target_upper_touch_rate_pct,
        hit_rate_sample_size,
    )
    if history is not None:
        components.append(("history", history, 0.10))
    total_weight = sum(weight for _, _, weight in components)
    value = sum(component * weight for _, component, weight in components) / total_weight if total_weight else 0.0
    strongest = sorted(components, key=lambda item: item[1] * item[2], reverse=True)[:3]
    weakest = sorted(components, key=lambda item: item[1])[:2]
    reason = "strong=" + ",".join(name for name, _, _ in strongest) + "; weak=" + ",".join(name for name, _, _ in weakest)
    return clamp(value), reason


def capital_factor_for_mode(
    mode: str,
    *,
    score: float,
    edge_score_value: float,
    quality_score_value: float,
    traded_value_ratio: float,
    min_factor: float = MIN_CAPITAL_FACTOR,
    max_factor: float = MAX_CAPITAL_FACTOR,
) -> float:
    low = max(0.0, min_factor)
    high = max(low, max_factor)
    if mode == "equal":
        return 1.0
    if mode == "score_linear":
        component = score_component(score)
    elif mode == "edge_linear":
        component = edge_component(edge_score_value)
    else:
        edge_base = edge_component(edge_score_value)
        liquidity_gate = clamp((traded_value_ratio - 1.9) / 0.4)
        component = edge_base + max(0.0, quality_score_value - edge_base) * liquidity_gate
    return low + (high - low) * clamp(component)


def position_sizing_for_signal(
    *,
    mode: str,
    score: float,
    setup_type: str,
    target_pct: float,
    hard_stop_pct: float,
    traded_value_ratio: float,
    atr_pct: float,
    momentum_3d_pct: float,
    momentum_10d_pct: float,
    distance_to_ma5_pct: float,
    close_position_20d_pct: float,
    sector_momentum_5d_pct: float,
    edge_score_value: float | None = None,
    first_manage_hit_rate_pct: float | None = None,
    target_upper_hit_rate_pct: float | None = None,
    target_upper_touch_rate_pct: float | None = None,
    hit_rate_sample_size: int = 0,
    max_positions: int = 3,
    market_capital_factor: float = 1.0,
    drawdown_capital_factor: float = 1.0,
    min_factor: float = MIN_CAPITAL_FACTOR,
    max_factor: float = MAX_CAPITAL_FACTOR,
    max_single_position_pct: float = MAX_SINGLE_POSITION_PCT,
) -> PositionSizingResult:
    edge = (
        edge_score_value
        if edge_score_value is not None
        else estimated_edge_score(score, target_pct, hard_stop_pct, traded_value_ratio, momentum_3d_pct, distance_to_ma5_pct)
    )
    quality, reason = quality_score_from_signal(
        score=score,
        setup_type=setup_type,
        edge_score_value=edge,
        traded_value_ratio=traded_value_ratio,
        atr_pct=atr_pct,
        momentum_3d_pct=momentum_3d_pct,
        momentum_10d_pct=momentum_10d_pct,
        distance_to_ma5_pct=distance_to_ma5_pct,
        close_position_20d_pct=close_position_20d_pct,
        sector_momentum_5d_pct=sector_momentum_5d_pct,
        first_manage_hit_rate_pct=first_manage_hit_rate_pct,
        target_upper_hit_rate_pct=target_upper_hit_rate_pct,
        target_upper_touch_rate_pct=target_upper_touch_rate_pct,
        hit_rate_sample_size=hit_rate_sample_size,
    )
    normalized_mode = mode if mode in {"equal", "score_linear", "edge_linear", "quality"} else "quality"
    factor = capital_factor_for_mode(
        normalized_mode,
        score=score,
        edge_score_value=edge,
        quality_score_value=quality,
        traded_value_ratio=traded_value_ratio,
        min_factor=min_factor,
        max_factor=max_factor,
    )
    slot_count = max(1, int(max_positions))
    suggested = 100.0 / slot_count * max(0.0, market_capital_factor) * max(0.0, drawdown_capital_factor) * factor
    if max_single_position_pct > 0:
        suggested = min(max_single_position_pct, suggested)
    suggested = min(MAX_TOTAL_CAPITAL_PCT, suggested)
    return PositionSizingResult(
        mode=normalized_mode,
        quality_score=round(quality, 4),
        quality_grade=quality_grade(quality),
        capital_factor=round(factor, 4),
        suggested_capital_pct=round(suggested, 2),
        reason=reason,
    )


def position_sizing_from_features(
    *,
    mode: str,
    score: float,
    setup_type: str,
    target_pct: float,
    hard_stop_pct: float,
    features: dict[str, Any],
    edge_score_value: float | None = None,
    max_positions: int = 3,
    market_capital_factor: float = 1.0,
    drawdown_capital_factor: float = 1.0,
    min_factor: float = MIN_CAPITAL_FACTOR,
    max_factor: float = MAX_CAPITAL_FACTOR,
    max_single_position_pct: float = MAX_SINGLE_POSITION_PCT,
) -> PositionSizingResult:
    return position_sizing_for_signal(
        mode=mode,
        score=score,
        setup_type=setup_type,
        target_pct=target_pct,
        hard_stop_pct=hard_stop_pct,
        traded_value_ratio=as_float(features.get("traded_value_ratio")),
        atr_pct=as_float(features.get("atr_pct")),
        momentum_3d_pct=as_float(features.get("momentum_3d_pct")),
        momentum_10d_pct=as_float(features.get("momentum_10d_pct")),
        distance_to_ma5_pct=as_float(features.get("distance_to_ma5_pct")),
        close_position_20d_pct=as_float(features.get("close_position_20d_pct")),
        sector_momentum_5d_pct=as_float(features.get("sector_momentum_5d_pct")),
        edge_score_value=edge_score_value,
        first_manage_hit_rate_pct=optional_float(features.get("first_manage_hit_rate_pct")),
        target_upper_hit_rate_pct=optional_float(features.get("target_upper_hit_rate_pct")),
        target_upper_touch_rate_pct=optional_float(features.get("target_upper_touch_rate_pct")),
        hit_rate_sample_size=int(as_float(features.get("hit_rate_sample_size"))),
        max_positions=max_positions,
        market_capital_factor=market_capital_factor,
        drawdown_capital_factor=drawdown_capital_factor,
        min_factor=min_factor,
        max_factor=max_factor,
        max_single_position_pct=max_single_position_pct,
    )
