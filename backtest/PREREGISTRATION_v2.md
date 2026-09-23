# Pre-registered backtest rules (v2)

Written 2026-09-23 after v1 results were known and BEFORE any v2 signal or return was computed. v1 stays fixed as the record.

## What changes and why
v1 had three problems:
- it ran over one AI bull market,
- it used hand-picked winning baskets,
- its memory rule ignored the cycle.

v2 changes three things:
1. **Longer history.** Taiwan revenue now starts in 2005 and prices in 2004. That makes a true **out-of-sample (OOS) window of Jan 2008 – Feb 2017**, which v1 never saw. The v2 rules below were informed by the v1 window (Mar 2017 – Sep 2026, the "in-sample" or IS window), so **only the OOS results count as evidence.**
2. **Broad baskets.** Each basket holds every priceable incumbent in the sector, including laggards and all the Taiwan signal companies. A name enters when its price history starts. Delisted or acquired names (e.g. old SanDisk, Finisar, Oclaro, NeoPhotonics) cannot be priced, so some survivorship bias remains.
3. **Relative returns.** Every result is also reported as excess return over a sector benchmark: SOXX for memory and optics, XLI for power.

## Signals (the index itself is unchanged: expanding z of YoY, composite C, momentum M = C − C[3 months earlier])
- **Memory:** TW revenue (2408, 2344, 2337, 2451, 8299, 3260), plus **Korea memory-IC exports YoY** (UN Comtrade HS 854232, usable on the 16th of M+1).
- **Optics:** TW revenue (3081, 4971, 2455, 3163, 3363, 2345). Same as v1.
- **Power:** TW revenue (1519, 1503, 1513, 1514), plus PPI for transformers, switchgear and turbines. Same as v1. One pre-declared variant, "power-TWonly", drops the PPI inputs.

## Baskets
- **Memory:** MU, WDC, STX, 000660.KS, 005930.KS, 2408.TW, 2344.TW, 2337.TW, 2451.TW, 8299.TWO, 3260.TWO
- **Optics:** COHR, CIEN, FN, AAOI, LITE, VIAV, 2455.TW, 4971.TWO, 3163.TWO, 3363.TWO, 3081.TWO, 2345.TW
- **Power:** ETN, HUBB, POWL, SIE.DE, ABBN.SW, SU.PA, 6501.T, 1519.TW, 1503.TW, 1513.TW, 1514.TW, VRT

## Rules
- **Memory, Rule M "avoid tight-and-slowing":** long unless C > 0 AND M < 0.
- **Optics and power, Rule B:** long when C > 0.
- v1's Rule A (C > 0 AND M > 0) is also reported for every group, for continuity.

Execution is the same as v1: decide on the 16th of M+1, act the next trading day, cash otherwise.

## Pass criteria, judged on the OOS window only
1. The average forward 12-month **excess** return (basket − benchmark) is higher when the rule is ON than when it is OFF.
2. The rule's Sharpe ratio is at least buy-and-hold's Sharpe, with Sharpe = mean daily return / std × √252, cash counted as 0.

A group passes only if it meets both criteria.
