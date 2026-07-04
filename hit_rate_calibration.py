from __future__ import annotations

from typing import Any

MIN_BUCKET_SAMPLE_SIZE = 8

BUCKET_DEFINITIONS: dict[str, tuple[str, tuple[tuple[float, float, str], ...]]] = {
    "score": (
        "score_bucket",
        (
            (float("-inf"), 87.0, "<87"),
            (87.0, 90.0, "87~90"),
            (90.0, 93.0, "90~93"),
            (93.0, 96.0, "93~96"),
            (96.0, float("inf"), ">=96"),
        ),
    ),
    "traded_value_ratio": (
        "traded_value_bucket",
        (
            (float("-inf"), 1.2, "<1.2x"),
            (1.2, 1.5, "1.2~1.5x"),
            (1.5, 2.0, "1.5~2.0x"),
            (2.0, 3.0, "2.0~3.0x"),
            (3.0, float("inf"), ">=3.0x"),
        ),
    ),
    "atr_pct": (
        "atr_bucket",
        (
            (float("-inf"), 4.1, "<4.1%"),
            (4.1, 5.5, "4.1~5.5%"),
            (5.5, 7.0, "5.5~7%"),
            (7.0, float("inf"), ">=7%"),
        ),
    ),
    "momentum_10d_pct": (
        "momentum_10d_bucket",
        (
            (float("-inf"), 0.0, "<0%"),
            (0.0, 10.0, "0~10%"),
            (10.0, 20.0, "10~20%"),
            (20.0, 26.0, "20~26%"),
            (26.0, float("inf"), ">=26%"),
        ),
    ),
}

BUCKET_PRIORITY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("market_setup_score", ("market_state", "setup_type", "score_bucket")),
    ("market_setup_atr", ("market_state", "setup_type", "atr_bucket")),
    ("market_setup_value", ("market_state", "setup_type", "traded_value_bucket")),
    ("market_setup_momentum10", ("market_state", "setup_type", "momentum_10d_bucket")),
    ("market_setup", ("market_state", "setup_type")),
    ("market_score", ("market_state", "score_bucket")),
    ("setup_score", ("setup_type", "score_bucket")),
    ("market_state", ("market_state",)),
    ("setup_type", ("setup_type",)),
    ("overall", ()),
)


def safe_float(value: object) -> float:
    try:
        if value in {None, ""}:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def numeric_bucket(value: object, buckets: tuple[tuple[float, float, str], ...], missing_label: str = "missing") -> str:
    if value in {None, ""}:
        return missing_label
    numeric = safe_float(value)
    for low, high, label in buckets:
        if low <= numeric < high:
            return label
    return buckets[-1][2] if buckets else missing_label


def bucketed_features(
    market_state: object = "",
    setup_type: object = "",
    score: object = "",
    traded_value_ratio: object = "",
    atr_pct: object = "",
    momentum_10d_pct: object = "",
    sector_group: object = "",
) -> dict[str, str]:
    features = {
        "market_state": str(market_state or "").strip() or "unknown",
        "setup_type": str(setup_type or "").strip() or "unknown",
        "sector_group": str(sector_group or "").strip() or "unknown",
    }
    raw_values = {
        "score": score,
        "traded_value_ratio": traded_value_ratio,
        "atr_pct": atr_pct,
        "momentum_10d_pct": momentum_10d_pct,
    }
    for raw_name, value in raw_values.items():
        feature_name, buckets = BUCKET_DEFINITIONS[raw_name]
        features[feature_name] = numeric_bucket(value, buckets)
    return features


def bucket_key(criteria: dict[str, str]) -> str:
    if not criteria:
        return "overall"
    return "|".join(f"{key}={criteria[key]}" for key in criteria)


def bucket_label(criteria: dict[str, str]) -> str:
    if not criteria:
        return "overall"
    return ", ".join(f"{key}:{value}" for key, value in criteria.items())


def candidate_bucket_queries(
    market_state: object = "",
    setup_type: object = "",
    score: object = "",
    traded_value_ratio: object = "",
    atr_pct: object = "",
    momentum_10d_pct: object = "",
    sector_group: object = "",
) -> list[tuple[str, dict[str, str]]]:
    features = bucketed_features(
        market_state=market_state,
        setup_type=setup_type,
        score=score,
        traded_value_ratio=traded_value_ratio,
        atr_pct=atr_pct,
        momentum_10d_pct=momentum_10d_pct,
        sector_group=sector_group,
    )
    queries: list[tuple[str, dict[str, str]]] = []
    for bucket_type, fields in BUCKET_PRIORITY:
        criteria = {field: features[field] for field in fields}
        queries.append((bucket_type, criteria))
    return queries


def sample_size(row: dict[str, Any] | None) -> int:
    if not isinstance(row, dict):
        return 0
    try:
        return int(float(row.get("sample_size", 0) or row.get("closed_trades", 0) or 0))
    except (TypeError, ValueError):
        return 0
