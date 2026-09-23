# Results v6: every theme in the Leadtime universe

Computed 2026-09-23 after `PREREGISTRATION_v6_universe.md` (pushed in 937688c) and after `config/themes.json` was finalised (28 themes, 166 tickers, validated by `work/themes/validate_themes.py`).

Pass rule (as for v4): the tilt's Sharpe beats always-overweight AND its annualised excess is above 0, in the OOS window Mar 2008 – Feb 2017. Only themes with a Taiwan revenue component have that window.

| theme | components | OOS tilt Sharpe | OOS always Sharpe | OOS tilt ann. % | result | since-2016 tilt vs always Sharpe | call now |
|---|---|---|---|---|---|---|---|
| lithography-fab-tools | Taiwan supplier revenue, US imports | 0.44 | -0.22 | 6.2 | PASS | 0.12 vs 0.37 | UNDERWEIGHT |
| semiconductor-materials | Taiwan supplier revenue | -0.34 | -0.08 | -10.0 | fail | 0.19 vs -0.06 | MONITORING |
| foundry-logic | Taiwan supplier revenue | -0.67 | -0.0 | -13.5 | fail | 0.08 vs -0.35 | MONITORING |
| advanced-packaging-hbm | Taiwan supplier revenue, Korean exports | -0.14 | 0.23 | -3.1 | fail | -0.09 vs 0.21 | MONITORING |
| memory | Taiwan supplier revenue, Korean exports | (Rule M, v2/v4) | | | PASS (timing) | | OUT |
| storage | Taiwan supplier revenue | -0.32 | 0.1 | -8.2 | fail | 0.58 vs 0.59 | MONITORING |
| substrates-pcb | Taiwan supplier revenue | 0.07 | -0.34 | 2.0 | PASS | 0.71 vs 0.23 | OVERWEIGHT |
| photonics-optics | Taiwan supplier revenue | 0.34 | 0.35 | 23.2 | fail | 0.82 vs 0.71 | MONITORING |
| networking-interconnect | Taiwan supplier revenue, US imports | 0.11 | 0.62 | 1.8 | fail | 0.47 vs 1.32 | MONITORING |
| connectors-cables | Taiwan supplier revenue, US imports | 0.59 | -0.5 | 10.0 | PASS | 0.23 vs 0.59 | OVERWEIGHT |
| ai-servers | Taiwan supplier revenue | -0.25 | -0.0 | -6.3 | fail | -0.16 vs 0.54 | MONITORING |
| cooling-thermal | Taiwan supplier revenue | -0.35 | 0.34 | -7.9 | fail | -0.21 vs 0.92 | MONITORING |
| power-electronics | Taiwan supplier revenue, US imports | -0.23 | 0.22 | -4.2 | fail | -0.12 vs -0.01 | MONITORING |
| accelerators-custom-silicon | Taiwan supplier revenue | -0.55 | 0.08 | -9.8 | fail | 0.29 vs 0.75 | MONITORING |
| ai-cloud-neoclouds | none |  |  |  | no OOS window |  | MONITORING |
| rare-earths-magnets | US imports |  |  |  | no OOS window | -0.18 vs 0.31 | MONITORING |
| gas-turbines-generation | US imports |  |  |  | no OOS window | 0.14 vs 0.37 | MONITORING |
| transformers | Taiwan supplier revenue, US imports, Korean exports | 0.21 | -0.22 | 3.2 | PASS | 1.04 vs 0.74 | UNDERWEIGHT |
| switchgear-grid-equipment | Taiwan supplier revenue, US imports | 0.02 | -0.02 | 0.3 | PASS | 0.48 vs 0.76 | OVERWEIGHT |
| transmission-cables | US imports |  |  |  | no OOS window | 0.17 vs 0.27 | MONITORING |
| nuclear-smr | none |  |  |  | no OOS window |  | MONITORING |
| batteries-storage | US imports, Korean exports |  |  |  | no OOS window | 0.0 vs 0.49 | MONITORING |
| power-producers | none |  |  |  | no OOS window |  | MONITORING |
| construction-mep | Taiwan supplier revenue | -0.4 | 0.27 | -6.8 | fail | 0.24 vs 1.38 | MONITORING |
| precision-motion-actuators | Taiwan supplier revenue | 0.17 | -0.07 | 4.6 | PASS | -0.18 vs -0.09 | OVERWEIGHT |
| sensing-vision | Taiwan supplier revenue | 0.15 | 0.56 | 3.0 | fail | -0.12 vs 0.13 | MONITORING |
| industrial-robots-automation | Taiwan supplier revenue, US imports | -0.04 | 0.16 | -1.0 | fail | 0.21 vs -0.05 | MONITORING |
| robot-platforms | US imports |  |  |  | no OOS window | -0.45 vs 0.46 | MONITORING |

**6 of 19 testable themes pass** (memory is validated separately). The other themes are MONITORING: the site shows their pressure readings, and they are logged as NO CALL.

## Caveats (read before trusting a tilt)
- **Multiple testing.** 19 themes were tested at once. With a pass bar this loose (beat always-overweight Sharpe and have positive excess), some passes are likely luck. Treat a single theme's PASS as weak evidence. Live results from the log are what count.
- **Several passes failed since 2016:** connectors-cables, switchgear, precision-motion and lithography-fab-tools. Only substrates-pcb and transformers pass in both windows.
- **Survivorship:** baskets list companies that exist today.
- **The v4 optics and power tilts are still logged under model v4.** The v6 photonics-optics basket (a different, wider list) fails its OOS test, so v6 makes no optics call. Both models' records stay public.

First v6 log entry: see `signal_log.jsonl` (2026-09-23).
