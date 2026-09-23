# Pre-registered backtest rules (v1)

Written 2026-09-23 BEFORE computing any signal or return. Any later change gets a new version, and v1 results are kept.

## Question
Do free, dated, physical-supply signals turn on before the stock baskets they relate to re-rate, and off before they fall?

## Bottlenecks, signals and baskets
Taiwan monthly revenue (MOPS): each group is the sum of its members' revenue. I use the YoY growth of the trailing 3-month sum.

- **MEMORY / STORAGE**
  - Signals: TW revenue of 2408 Nanya, 2344 Winbond, 2337 Macronix, 2451 Transcend, 8299 Phison, 3260 ADATA
  - Basket: MU, WDC, STX, 000660.KS (SK hynix)
- **OPTICS / DATA MOVEMENT**
  - Signals: TW revenue of 3081 LandMark, 4971 IntelliEPI, 2455 VPEC, 3163 Browave, 3363 FOCI, 2345 Accton
  - Basket: LITE, COHR, AAOI, FN, CIEN
- **POWER EQUIPMENT**
  - Signals: TW revenue of 1519 Fortune Electric, 1503 Shihlin Electric, 1513 Chung-Hsin, 1514 Allis Electric; US PPI YoY for transformers (PCU335311335311), switchgear (PCU335313335313) and turbines/generator sets (PCU333611333611)
  - Basket: ETN, HUBB, POWL, VRT, SIE.DE, 1519.TW

Hyperscaler capex (SEC XBRL) is shown as demand context only. It is not part of any composite.

## Point-in-time handling
- **TW revenue for month M:** usable from the 11th of M+1 (the legal deadline is the 10th).
- **PPI for month M:** usable from the 16th of M+1. Caveat: FRED serves the *revised* series, and PPI revisions are small but non-zero.
- **Z-scores:** expanding-window only (mean and std of history up to that month), with at least 24 observations.
- **Tickers:** a basket member is used only once it has a price. There is no survivorship repair; the baskets are hand-picked today, which is a stated bias.

## Index
- z_i = expanding z-score of series i's YoY growth
- Composite C = mean of the available z_i
- Momentum M = C − C three months earlier

## Rules (both reported, nothing else tuned)
- **Rule A, "tightening":** long the basket when C > 0 AND M > 0, else cash.
- **Rule B, "tight":** long the basket when C > 0, else cash.

The decision is taken on the 16th of the month after month M and held until the next decision date. The basket is equal-weight and rebalanced at each decision. There are no costs; 10 bp per switch is shown as a sensitivity.

## Evaluation
- **Window:** from the first valid decision (2016 or later) through the latest data.
- **Compared with:** buy-and-hold of the same basket, and SPY.
- **Metrics:** CAGR, max drawdown, time in market, and the basket's mean forward 6- and 12-month return when ON vs OFF.
- **Case studies:** 2017–18 memory boom and bust, 2022 glut, 2024 "HBM sold out" drawdown, 2025–26 memory run, 2024–26 power run.
