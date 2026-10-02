# Reliability review and numerical corrections, 2 October 2026

This release fixes account identity, alert retries, public webhook delivery, monthly publication, calendar alignment and historical source availability. It also labels listing currencies and distinguishes physical-data months, decision dates, market closes and page refresh times.

## Numerical corrections

Growth and momentum now use calendar months. A missing month is inserted as missing data before rolling sums and shifts, so a three-month or twelve-month comparison cannot silently become a comparison over a different number of calendar months. Historical decision dates use the longest lag among the components actually present in that observation. The site and the logged memory model now share the registered Korea Customs/Comtrade source-selection rule.

The discovery event study previously reported an event-weighted mean but tested an equally weighted mean of monthly averages. The corrected calculation tests the reported event-weighted mean with an intercept-only CR1 standard error clustered by event month. Flag thresholds, windows and stored event returns are unchanged.

The correction uses the existing `backtest/v5_discovery_events.csv`, rather than downloading new price histories. Its source hash and calculation timestamp are recorded in `backtest/v5_discovery_results_corrected.json`.

| Window | Horizon | Original t-statistic | Corrected t-statistic |
| --- | --- | ---: | ---: |
| OOS 2008–2016 | 6 months | 5.90 | 7.53 |
| OOS 2008–2016 | 12 months | 4.56 | 5.99 |
| IS 2017 onward | 6 months | 2.08 | 2.55 |
| IS 2017 onward | 12 months | -0.10 | 0.62 |

Means, medians and hit rates are unchanged. The registered OOS pass remains true. The original result files, event-return file, pre-registrations and signal-log entries are preserved; the snapshot builder selects the separately disclosed corrected discovery result.

Reproduce the statistics correction from the repository root:

```bash
python backtest/run_v5_discovery.py --from-events backtest/v5_discovery_events.csv --output work/v5-statistics-recheck.json
```

A separate memory OOS check with the repository's physical data and Yahoo adjusted closes downloaded on 2 October 2026 reproduced strategy CAGR 15.9% and Sharpe 0.71, versus buy-and-hold 13.0% and 0.60. Its pass result is unchanged. This is a check using a fresh price download, rather than a reconstruction of the original provider response.

The full v6 theme rerun requires `data/processed/us_imports_all.csv` and the provider environment. This dataset is intentionally absent from Git. Do not assert unchanged theme validation counts until the complete production inputs have been rerun with the corrected calendar and availability logic.

## Deployment

The account upgrade preserves existing accounts and linked channels, expires legacy cookies and unused Telegram link codes, and removes orphan records. Users sign in again after deployment. Back up the live database using SQLite's backup API before the upgrade.

Alert retries use saved payloads, atomic claims and renewable leases. Confirmed successes are deduplicated; a crash between provider acceptance and the success commit can still cause a repeat. Historical failures without saved payloads cannot be reconstructed automatically.

See [OPERATIONS.md](OPERATIONS.md) for the database migration, independent retry worker and monthly publication procedure. A deployment is complete only after the live service, migration, provider configuration and rebuilt snapshot have been verified.
