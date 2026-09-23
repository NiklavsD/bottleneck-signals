# Results v5: discovery scanner event study

Computed 2026-09-23 after `PREREGISTRATION_v5_discovery.md` was pushed. Returns are the flagged stock minus the market over 6 and 12 months from the flag's publication date. Clustered t-stats use event months.

- Flags: 3,145 (dropped for no price history: 84)
- **OOS 2008–2016, 6 months:** mean +8.5%, median +1.33%, hit 53.5%, t 5.9, 1610 events → **PASS**
- OOS 12 months: mean +9.87%, median -0.2%, hit 49.6%, t 4.56
- Tuning period 2017+, 6 months: mean +4.44%, median -4.09%, hit 42.3%, t 2.08
- Tuning period 2017+, 12 months: mean +1.67%, median -7.49%, hit 41.5%, t -0.1

## What this means
- The average flag beats the market, but the **median flag does not**: the average comes from a minority of very large winners. A flag is a lead to research, not a buy signal.
- The recent period is weaker than the older one.
- **Data-quality fix, disclosed:** a Yahoo bad tick (8917.TW quoted at 0.0197 for 15 days) first inflated the OOS mean to ~47%. Closes below 0.2× or above 5× the centred 252-day median are now treated as missing (677 ticks). The rule itself did not change.

