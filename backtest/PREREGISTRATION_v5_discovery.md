# Pre-registered rules (v5): discovery scanner

Written 2026-09-23, BEFORE any flag or return was computed.

## Question
When a Taiwan-listed company's monthly revenue *unusually accelerates*, does its stock beat the Taiwan market over the next 6 and 12 months, measured from the date that revenue was published?

## Flag rule (company level, applied every month with point-in-time data only)
For company i and data month t:
- R3 = revenue summed over months t-2..t
- g = R3 / R3(t-12) − 1, the 3-month YoY growth
- z = expanding z-score of g over company i's own history. At least 24 prior observations, using data up to t only.
- a = g − g(t−3), the acceleration

**FLAG** when all of these hold:
- z ≥ 2.0
- g ≥ 0.30
- a > 0
- R3 ≥ NT$300 million (to exclude micro-caps)

The same company is not re-flagged within 6 months of a previous flag.

## Event study
- **Event date:** the 11th of month t+1, the day after the MOPS revenue deadline. The position is entered at the close of the first trading day on or after it.
- **Returns:** the stock's 6- and 12-month total return, minus the TAIEX (^TWII) over the same dates.
- **Prices:** Yahoo Finance (.TW or .TWO). Flags without a price on the event date are dropped, and the number dropped is reported.
- **Windows:**
  - **OOS:** events dated Jan 2008 – Dec 2016
  - **IS:** events from Jan 2017 on
  - No part of the rule was tuned on either window; the thresholds were set before looking.

## Pass criteria (OOS)
1. The mean 6-month excess return is above 0, with a t-stat above 2 (standard errors clustered by event month).
2. The median 6-month excess return is above 0.

## Product use
If it passes, flags go live as "discovery flags", with an alert option.
If it fails, flags are still shown as a data feed, labelled "unvalidated".

HS-code trade flags (US imports, Korea exports) use the same z/g/a rule with a US$50m 3-month minimum. They are published as a data feed only, because there is no direct stock mapping to test.
