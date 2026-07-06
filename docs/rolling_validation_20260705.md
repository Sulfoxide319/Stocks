# Rolling Validation 2026-07-05

## Purpose

This validation checks whether the `v0.4.41` result is only an artifact of the fixed `2026-07-03` end date. It adds rolling month-end strict 10-minute backtests across `1M/3M/6M/12M` and a small rolling parameter perturbation test around quality-based position sizing.

This is not a proof of universal profitability. It is an out-of-sample pressure test across different ending dates and nearby parameter settings.

## Commands

```powershell
python tools\rolling_strict_10m_validation.py --no-v0432-proxy --baseline-name current_default --monthly-end-date-count 6 --monthly-end-date-to 2026-07-03 --period-months 1,3,6,12 --out-dir output\rolling_v041_current_default_monthly_6x_12m
```

```powershell
python tools\rolling_strict_10m_validation.py --no-v0432-proxy --baseline-name current_default --end-dates 2026-05-29,2026-06-30,2026-07-03 --period-months 1,3,6,12 --tuning-preset quality_sizing --out-dir output\rolling_v041_quality_sizing_tuning_3x_12m
```

## Current Default Rolling Results

| End Date | 1M | 3M | 6M | 12M | 12M DD | Hard Checks |
|---|---:|---:|---:|---:|---:|---|
| 2026-02-27 | 5.61% | 13.05% | 12.95% | 7.10% | 12.29% | 0/0/0 |
| 2026-03-31 | 12.35% | 18.83% | 23.41% | 17.71% | 11.43% | 0/0/0 |
| 2026-04-30 | 19.69% | 31.98% | 46.31% | 41.34% | 10.22% | 0/0/0 |
| 2026-05-29 | 6.79% | 43.37% | 63.57% | 52.35% | 9.65% | 0/0/0 |
| 2026-06-30 | 20.14% | 50.61% | 77.65% | 91.21% | 7.09% | 0/0/0 |
| 2026-07-03 | 16.77% | 49.11% | 76.62% | 88.44% | 7.09% | 0/0/0 |

The fixed `2026-07-03` result is not isolated: `2026-06-30` is similarly strong. However, the earlier rolling 12M windows are much weaker, especially `2026-02-27` at `7.10%` with `12.29%` drawdown. The strategy is profitable in all tested windows, but the high headline 12M return is highly dependent on the strong late-window regime.

## Rolling Parameter Perturbation

The tuning test compares `current_default` against equal sizing, score-linear sizing, edge-linear sizing, a lower quality cap, no drawdown governor, and looser drawdown-governor variants over `2026-05-29`, `2026-06-30`, and `2026-07-03`.

| Scenario | Period | Avg Return | Max DD | Both-Win Rows vs Default |
|---|---|---:|---:|---:|
| current_default | 12M | 77.33% | 9.65% | 3/3 |
| quality_no_dd_governor | 12M | 81.67% | 10.73% | 0/3 |
| quality_dd045_f085 | 12M | 79.42% | 10.16% | 0/3 |
| quality_dd04_f075 | 12M | 76.45% | 9.65% | 0/3 |
| edge_linear_sizing | 12M | 74.63% | 9.46% | 0/3 |
| quality_max135 | 12M | 74.61% | 9.31% | 0/3 |
| score_linear_sizing | 12M | 70.59% | 8.63% | 0/3 |
| equal_sizing | 12M | 58.04% | 7.87% | 0/3 |

For `6M`, removing the drawdown governor has the highest average return (`74.90%` versus default `72.61%`) without raising the tested max drawdown above default in these three windows. For `12M`, the same change increases average return but raises max drawdown from `9.65%` to `10.73%`, so it does not pass a steady-default risk gate.

## Interpretation

- The strategy is not only a single-end-date artifact, because all six tested month-end windows are positive and the `2026-06-30` result is as strong as `2026-07-03`.
- The strategy is still regime-sensitive. The 12M return rises from `7.10%` at `2026-02-27` to `91.21%` at `2026-06-30`, so the headline result depends heavily on the late strong market.
- Current `v0.4.41` remains a defensible steady default among the tested nearby sizing rules: higher-return variants buy more risk, while lower-risk variants give up too much return.
- The overfitting concern is not eliminated. The next required validation is walk-forward promotion: freeze parameters, then evaluate future month-end paper results without changing them.

## Artifacts

- `output/rolling_v041_current_default_monthly_6x_12m/rolling_strict_10m_validation.md`
- `output/rolling_v041_current_default_monthly_6x_12m/rolling_strict_10m_validation.csv`
- `output/rolling_v041_current_default_monthly_6x_12m/rolling_strict_10m_aggregate.csv`
- `output/rolling_v041_quality_sizing_tuning_3x_12m/rolling_strict_10m_validation.md`
- `output/rolling_v041_quality_sizing_tuning_3x_12m/rolling_strict_10m_validation.csv`
- `output/rolling_v041_quality_sizing_tuning_3x_12m/rolling_strict_10m_aggregate.csv`
