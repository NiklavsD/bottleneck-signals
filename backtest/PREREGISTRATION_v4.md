# Pre-registered rules (v4): memory composition fix, optics and power as tilt signals

Written 2026-09-23, BEFORE any v4 number was computed. v1–v3 stay fixed.

## 1. Memory: like-for-like momentum (an engineering fix; the rule is unchanged)
**Problem found live:** Comtrade's Korea series ends in Dec 2025. From Jan 2026 the composite C therefore became Taiwan revenue only. C jumped from 2.19 to 4.73 in one month, and momentum M was inflated for the next three months. That was an artifact of which inputs were available, not a real change in the market.

**Fix:**
- M_t = the average, over components present at BOTH t and t−3, of (z_i,t − z_i,t−3).
- C_t stays the average of the components available at t.
- If, at a decision date, a component that existed at t−3 is missing at t, M uses only the components present in both months.
- **Korea source:** use the Korea Customs Service series (tradedata.go.kr, HS 854232) when it validates against Comtrade within 5% median absolute difference over 2013–2025. Otherwise keep Comtrade. Korea Customs publishes month M on about the 1st–15th of M+1, which fits the 16th-of-M+1 decision date.
- **Rule M is unchanged:** stay IN unless C > 0 AND M < 0.
- **Pass criteria:** the same as v2, on the same OOS window (Mar 2008 – Feb 2017).

## 2. Optics and power: tilt, not timing
v2 and v3 showed that these indices sort forward excess returns but lose money when used to move between the basket and cash. v4 tests them as tilt signals.

**Tilt position:**
- +1 × (basket − benchmark) when C > 0
- −1 × (basket − benchmark) when C ≤ 0
- Rebalanced on decision dates

**Compared against:** always holding +1 × (basket − benchmark).

**Pass:** in the evaluation window, the tilt's Sharpe must beat the always-overweight Sharpe AND its mean daily excess return must be above 0.

**Composites and windows:**
- **Optics:** the v2 composite. Window: OOS Mar 2008 – Feb 2017.
- **Power, v2 composite** (TW + PPI): window OOS Mar 2008 – Feb 2017.
- **Power, v3 composite** (TW + US imports): no OOS window exists, so it is reported over its full window and labelled UNVALIDATED.
- **Benchmarks:** SOXX for optics, XLI for power.

## 3. What gets logged live (from the next monthly run)
- **Memory:** Rule M, IN or OUT.
- **Optics and power:** tilt, OVERWEIGHT or UNDERWEIGHT vs the benchmark. A tilt call is logged only for a bottleneck whose tilt test passes. A bottleneck that fails is logged as "no call" with its C and M values.
