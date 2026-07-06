# Release 0.4.48

## Correction

- Reworked broker-imported winner management lines to stay anchored to holding cost.
- Long-held winners no longer use `current/high × 1.10` as the target. Instead, the system advances to the next cost-based profit ladder:
  - gain `< 20%`: 10% ladder
  - gain `20%-60%`: 15% ladder
  - gain `60%-120%`: 20% ladder
  - gain `> 120%`: 25% ladder
- Protective stop moves to the previous cost-based profit ladder, while normal or losing positions still keep the original cost-based defaults.

## Example

- `000725` cost `2.3448`, high/current `8.38`, gain about `257%`
- next target ladder: `+275%` from cost -> target `8.7930`
- protected profit ladder: `+250%` from cost -> stop/protection `8.2068`
