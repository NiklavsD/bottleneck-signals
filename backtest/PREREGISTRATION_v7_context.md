# Pre-registered rules (v7): context layer, press narrative and memory spot prices

Written 2026-09-23. This is BEFORE any narrative-heat number was computed, and BEFORE the historical spot-price backfill (`pipeline/backfill_spot_wayback.py`, run by a worker) returned any data. The spot history has not been looked at. v1–v6 stay fixed.

## A. Press narrative ("heat") and the hype gap: context now, test later

**Data.** `engine/context.py` runs daily. It reads free newsletters and research feeds (SemiAnalysis, Fabricated Knowledge, The Chip Letter, Asianometry, TrendForce, DigiTimes), trade press (EE Times, Tom's Hardware, SemiWiki, ServeTheHome, The Next Platform, DatacenterDynamics, Utility Dive, The Robot Report) and one Google News query per theme. An LLM (GLM-5.3) tags each item with:
- theme(s)
- signal: tightening, easing or neutral
- fact type
- a one-line summary

Items are stored with the date on which they were fetched and tagged. The prompt and the source list are in the repo. Any change to either is committed.

**Weekly heat per theme.**
- H = the number of relevant items tagged to the theme in the ISO week. News items found by more than one query count once.
- Heat z = the z-score of log(1 + H) against the theme's own history. It uses an expanding window and needs at least 12 weeks.
- Tone = (tightening − easing) / relevant items over the trailing 4 weeks.

**Hype gap.** At each monthly decision date:
- Heat z is the mean over the preceding 4 weeks.
- A theme is "QUIET-TIGHT" when its pressure C > 1 and its heat z < 0.
- It is "LOUD-LOOSE" when C < 0 and heat z > 1.

**No backtest is possible.** Past news cannot be reconstructed point-in-time, and today's search results are not what was visible then. So:
- The site shows heat, tone and the gap label as **context only**. None of them changes a call.
- The first test runs once 12 monthly decision dates of live data exist (earliest 2027-09).
- Test: over all theme-months, the 6-month basket-minus-benchmark return after QUIET-TIGHT labels is compared with the return after all other labels. Clustered by month, t > 2 to pass.
- It is reported whatever the result.

## B. Memory spot prices (DRAMeXchange)

**Live series.** `pipeline/fetch_spot.py` records the free DRAMeXchange spot tables daily from 2026-09-23. They are shown on the memory and storage pages as context.

**Historical test (one shot, run once the Wayback backfill exists).**

*Spot index.* A chained like-for-like DRAM chip spot index:
- Between consecutive observations, take the mean log change of every `dram_chip` item present at both.
- Cumulate the changes.
- Resample to month-end using the last observation. A month with no observation is carried forward.
- Items are not chosen by return. Every DRAM chip row the parser reads is used.

*Spot momentum.* S = the 3-month change of the spot index. The value for month M is usable from the 1st of M+1: spot prices are public the same day.

*Rule SP (a candidate, not a replacement).* Memory basket IN unless C > 0 AND S < 0, with C from the v4 memory index.

*Test.*
- Same basket, same costs and the same out-of-sample window as the v2/v4 memory test, restricted to months where S exists.
- Rule M is run on exactly the same months as the comparison.
- **Pass:** Rule SP's Sharpe ≥ Rule M's Sharpe AND Rule SP's CAGR ≥ Rule M's CAGR on the shared window.
- If the shared window has fewer than 60 months, the result is reported as "insufficient history" and nothing passes.

*What a pass means.* A pass does NOT change the live memory call. It would only justify a v8 pre-registration that proposes spot momentum as the memory rule's M.
