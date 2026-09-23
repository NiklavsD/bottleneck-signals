# Results v7: context layer

Computed 2026-09-23 after `PREREGISTRATION_v7_context.md` (pushed in c63c1d1).

## B. Memory spot momentum (Rule SP): insufficient history, no test run

- **Archived pages:** the Wayback backfill (`pipeline/backfill_spot_wayback.py`) read 274 of 318 archived DRAMeXchange homepages. Pages from before February 2014 carry no machine-readable spot table.
- **Index:** the chained DRAM chip spot index starts 2014-02. Spot momentum S (3-month change) starts 2014-06-30.
- **Shared window:** the pre-registered out-of-sample window is Mar 2008 – Feb 2017. It overlaps S for **29 months** (2014-06-30 to 2017-02-28), below the 60-month minimum.
- **Outcome:** per the pre-registration, the result is **"insufficient history"** and Rule SP does not pass. No performance number was computed for Rule SP.
- **What still runs:** the spot index is shown on the memory page as context, and daily recording continues.
- **Future use:** any future use of spot prices in a rule needs a new pre-registration with a window chosen before looking.

Sanity check of the index (descriptive, not a test). It tracks the known DRAM cycles:
- Trough in mid-2016.
- Peak in 2017–18.
- Trough in 2019.
- Low from 2023 to early 2025, then about 17× higher by September 2026.

## A. Press heat / hype gap

Collection started 2026-09-23 with a 30-day seed: 607 candidates, 388 tagged relevant. The first test is due after 12 monthly decision dates (earliest 2027-09).
