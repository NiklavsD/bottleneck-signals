# Backtest v2 results (2026-09-23)

Rules: `PREREGISTRATION_v2.md` (sha256 93735af1…).
- **Out-of-sample (OOS), Mar 2008 – Feb 2017:** the rules never saw this period, so only these results count as evidence.
- **In-sample (IS), Mar 2017 – Sep 2026:** the period that informed the rules.

## Verdict per bottleneck (OOS pass = better excess-return spread AND Sharpe ≥ buy-and-hold)

- ✅ **Memory, Rule M ("get out when tight AND slowing"): PASS**
  - **OOS:** CAGR 15.9% vs 13.0% buy-and-hold. Sharpe 0.71 vs 0.60.
  - **OOS forward 12-month excess return vs SOXX:** +13.4% when IN, −25.1% when OUT.
  - **IS:** CAGR 43.4% vs 40.7%. Sharpe 1.50 vs 1.33. Max drawdown −38% vs −40%.
  - **Robustness (all still PASS):**
    - Signals arriving one month later
    - Taiwan revenue only, without the Korea exports
    - Basket of the three US names only
  - **Caveats:**
    - It is in the market about 90% of the time, and it did NOT avoid the −73% drop in 2008 (a macro crash, not a shortage turning).
    - There are only about 9 OUT months in the OOS window, about 6 independent cycles in total.
- ❌ **Optics, Rule B: FAIL**
  - The signal does sort excess returns in both windows. OOS: +39% vs +21%. IS: +46.5% vs +5.2%.
  - But using it to move in and out of cash lowers Sharpe (OOS 0.41 vs 0.57).
  - Use it for ranking and tilting, not timing.
- ❌ **Power, Rule B (with PPI): FAIL.** OOS Sharpe −0.40 vs 0.43.
- ❌ **Power, TW-only variant: FAIL.** OOS Sharpe 0.28 vs 0.49.
- **Power has no demonstrated edge.** It needs different inputs: order backlogs and lead times, and US transformer imports (the Census API needs a free key).

## Memory OUT episodes (decision dates)
- **Apr–Sep 2010:** memory peaked in Apr 2010. ✅
- **Sep–Oct 2013 and Apr 2014:** short, mixed.
- **Jun 2017 – May 2018, plus Sep–Nov 2018:** early. The stocks kept rising until mid-2018, then fell.
- **Nov 2021 – May 2022:** the 2022 glut. ✅
- **Jul 2024 – Jan 2025:** MU fell from $119 to the $60s. ✅
- **16 Sep 2026: OUT now.** The index is at a record 6.55 z, but momentum is −0.11.

## Limits
- The baskets still carry survivorship bias. Old SanDisk, Qimonda, Elpida, Spansion and others cannot be priced.
- Korea exports (Comtrade) only cover 2013 – Dec 2025, so 2026 relies on Taiwan revenue only.
- PPI inputs use revised FRED data, not the values available at the time.
- There are no costs or taxes. At about 10 switches per decade, the impact is small.
