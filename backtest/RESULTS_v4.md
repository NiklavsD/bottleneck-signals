# Backtest v4 results (2026-09-23)

Rules: `PREREGISTRATION_v4.md`, pushed publicly (commit 8b2eedb) before any v4 number was computed.

## Korea data gap: fixed
- **New source:** Korea Customs Service, via `tradedata.go.kr` (`pipeline/fetch_korea_customs.py`, POST `/cts/hmpg/retrieveTrade.do`). No key needed.
- **Coverage:** HS 854232 and 8542, Jan 2013 – Aug 2026. The latest month appears about two weeks after month end. (Comtrade stopped at Dec 2025.)
- **Validation vs Comtrade, 2013–2025:** median absolute difference 0.0000055%. The pre-registered switch threshold was 5%, so the index now uses Korea Customs.

## Memory: composition artifact fixed; the call is unchanged
- **The artifact:** when Korea dropped out, the index jumped from 2.19 to 4.73 in Jan 2026. With complete Korea data it now rises smoothly: 2.19 → 3.23 → 4.19 → 5.04.
- **Momentum:** now like-for-like, meaning it only compares components present in both months.
- **OOS 2008–2017: PASS.** CAGR 15.9% vs 13.0%, Sharpe 0.71 vs 0.60. Unchanged, because the Korea data starts in 2013.
- **IS 2017–2026:** CAGR 44.1% vs 40.7%, Sharpe 1.51 vs 1.33.
- **Now: OUT.** Aug data: C = 5.33, M = −0.15. Both inputs have peaked and are easing: the Korea z-score peaked at 4.56 in Apr, the TW z-score at 6.83 in Jul.

## Optics and power as tilt signals (+1 or −1 × basket-minus-benchmark, vs always +1)
- ✅ **Optics** (vs SOXX): **PASS**, but marginal. OOS Sharpe 0.32 vs 0.30; IS 0.60 vs 0.20. **Now: OVERWEIGHT** (C = 1.50).
- ✅ **Power, v2 composite** (TW + PPI, vs XLI): **PASS**. OOS Sharpe 0.48 vs −0.18; IS 0.84 vs 0.83. **Now: OVERWEIGHT** (C = 0.53).
- ❌ **Power, v3 composite** (TW + US imports, vs XLI): **FAIL**, and unvalidated because there is no OOS window. Full-window Sharpe 0.58 vs 0.78. Not used for live calls.

The live log uses v4 from 2026-09-23 on: memory IN/OUT, and optics and power OVERWEIGHT/UNDERWEIGHT vs their benchmark.
