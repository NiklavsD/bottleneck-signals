# Backtest v1 results (2026-09-23)

Rules as pre-registered in `PREREGISTRATION.md` (sha256 in `PREREGISTRATION.sha256`). Window: Mar 2017 → Sep 2026 (power from Jan 2015). No costs; 10 bp per switch changed CAGR by <0.5 pp.

## Headline: the rules did NOT beat buy-and-hold

**Memory** (buy-and-hold: CAGR 47.1%, max drawdown −48%)
- Rule A: CAGR 27.5%, max DD −51%, 37% time in market. **Now OFF.**
- Rule B: CAGR 27.6%, max DD −65%, 63% time in market. Now ON.

**Optics** (buy-and-hold: CAGR 38.8%, max DD −50%)
- Rule A: CAGR 2.4%, max DD −50%, 33% time in market. Now ON.
- Rule B: CAGR 22.1%, max DD −56%, 59% time in market. Now ON.

**Power** (buy-and-hold: CAGR 32.1%, max DD −43%)
- Rule A: CAGR 12.0%, max DD −43%, 42% time in market. Now ON.
- Rule B: CAGR 30.7%, max DD −43%, 79% time in market. Now ON.

**SPY** over the same window: CAGR about 16%.

Why: these were secular AI bull markets in hand-picked winners, so any time spent in cash was costly. The buy-and-hold figures themselves carry hindsight bias.

## The signals do carry information, but it differs by sector

Average forward 12-month basket return, grouped by the index state at the decision date. **Exploratory (post-hoc) grouping, not a pre-registered rule.**

**Memory (cyclical): buy the turn, not the tightness**
- Index < 0 and rising: **+170%** (n=18, 83% positive)
- Index < 0 and falling: +95% (n=24, 92% positive)
- Index > 0 and rising: +70% (n=31, 61% positive)
- Index > 0 and falling: **+15%** (n=30, 50% positive)

**Optics and power (secular): the level of tightness works as a filter**
- Optics, Rule B ON vs OFF: **+97.5% vs +23.5%**
- Power, Rule B ON vs OFF: **+52.5% vs +12.8%**

## Memory case study (Rule A, as pre-registered)

- **Jul 2024:** OFF on the June-2024 data, decided 16 Jul 2024. MU was about $119 (peak $152 on 18 Jun 2024). It then fell to $65 by Apr 2025, so the rule **avoided most of the −58% "HBM sold out" drawdown**.
- **Apr 2025:** momentum turned positive on the March data (decided ~16 Apr 2025), with MU around $69. This was the best-quadrant signal.
- **Jul 2025:** Rule A turned ON (decided 16 Jul 2025). Prices then: MU $113, SNDK $41.5, WDC $67, STX $146. It rode the run to the June 2026 peak (MU $1,213, SNDK $2,335) and stayed through the July 2026 drop (MU −39%, SNDK −56%).
- **Sep 2026:** OFF on the August data (decided 16 Sep 2026). Tightness is still extreme (z 5.2), but momentum has turned negative. Historically this has been the weakest memory quadrant. **This is the first live call, and not advice.**
- **2018 (bad):** the rule flickered ON in Jul 2018, near the memory top.

## Known weaknesses, to fix in v2 (a new version; v1 stays as recorded)

1. The baskets are hand-picked today. v2 should use point-in-time membership, or all listed names in a sector.
2. Power: US PPI barely moved in 2023–26 while power stocks tripled, so the PPI inputs dilute the index. Add lead times, backlog, interconnection queues and TW transformer exports.
3. Memory needs cycle-aware rules, as the quadrant results show. Add DRAM/NAND contract prices and Korea chip exports.
4. FRED serves revised PPI. Use ALFRED vintages.
5. The sample is small: roughly 2 memory cycles and one AI boom.
