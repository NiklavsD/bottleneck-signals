# Pre-registered rules (v6): every theme in the Leadtime universe

Written 2026-09-23, BEFORE the theme list (`config/themes.json`) was finalised and BEFORE any v6 number was computed. v1–v5 stay fixed.

## Index (the same for every theme)
Components are built only from what the theme lists:
- **TW revenue:** the members' summed revenue, as the YoY growth of the trailing 3-month sum (present-in-both-periods rule, as in v1).
- **US imports:** the listed HS6 codes summed, as the YoY growth of the trailing 3-month sum. Usable from the 16th of M+2.
- **Korea exports:** the listed HS codes summed, same treatment. Usable from the 16th of M+1.

Each component becomes an expanding z-score (at least 24 observations). C = the mean of the available z-scores. M is like-for-like (v4). The decision date for month M is the latest availability date among the components used.

## Calls
- **memory:** keeps its v4 Rule M (IN/OUT). It is already validated.
- **Every other theme with at least one component:** the v4 tilt rule. OVERWEIGHT vs the benchmark when C > 0, UNDERWEIGHT otherwise.
- **Tilt test windows:**
  - **OOS window, Mar 2008 – Feb 2017:** used only if the theme has a TW component (TW data starts 2005).
  - **"IS-only / UNVALIDATED" window, 2016 to now:** used for themes whose only components are US or Korea trade data (2013+).
- **Pass (same as v4):** the tilt's Sharpe beats always-overweight AND the tilt's mean daily excess return is above 0.
- **Themes that fail, or have no OOS window:** shown as MONITORING. The site shows their C and M but makes no call, and they are logged as "no call".
- **Themes with no components** (e.g. power producers): MONITORING. The site shows their basket performance only.

## Baskets
Equal-weight, as listed in `config/themes.json`. A name enters when its price history starts. The survivorship caveat applies as in v2.
