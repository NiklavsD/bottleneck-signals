# Pre-registered backtest rules (v3, power only)

Written 2026-09-23, BEFORE computing any v3 signal or return. v1 and v2 stay fixed.

## Why
In v2, power failed out-of-sample with both the PPI and TW-only composites. v3 adds the direct physical-demand series we lacked: **US import value of power transformers and switchgear** (Census intltrade API, HS6, 2013 onward).

## Signals
Each series is converted to YoY growth of a trailing 3-month sum, then to an expanding z-score (at least 24 observations).
1. TW revenue for 1519, 1503, 1513 and 1514 (unchanged).
2. US imports of liquid-dielectric transformers, HS 850421 + 850422 + 850423, summed.
3. US imports of switchgear and control boards above 1000 V, HS 853720.

PPI is dropped because it failed in v2. HS 850440 (static converters) is excluded because it is dominated by consumer and solar inverters.

The composite C is the mean of the available z-scores; M = C − C three months earlier.

## Point-in-time
- **Census data for month M:** used from the 16th of M+2. (It is published about 35 days after month end.)
- **TW revenue for month M:** used from the 16th of M+1.
- The composite for a decision date uses only the data available by that date. In practice, the decision for month M is taken on the 16th of M+2, with Census month M and TW month M.

## Basket, benchmark and rules
- **Basket:** the same as v2 (12 names).
- **Benchmark:** XLI.
- **Rules:**
  - **Rule B:** long when C > 0.
  - **Rule A:** long when C > 0 AND M > 0.

## Window and honesty caveat
- **Evaluation window:** the first valid decision (about 2016) to today.
- **There is NO clean out-of-sample window.** Census HS data starts in 2013, and the author already knew that power equipment rallied in 2023–26. The rules themselves are copied unchanged from v1/v2. Treat any result as weak evidence; only the live forward log can confirm it.
- **Pass criteria:** the same as v2, applied to the full window.
