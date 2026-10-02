# Running Leadtime safely

## Upgrade the account database

Before deploying a release with a schema change, stop the website and alert dispatcher and back up the production SQLite database with SQLite's backup API. Copying only a live `.db` file can miss its WAL contents. Use the configured `LEADTIME_DB` path, or `web/data/leadtime.db` when unset:

```bash
.venv/bin/python -c 'import os, sqlite3; p=os.environ.get("LEADTIME_DB", "web/data/leadtime.db"); src=sqlite3.connect(p); dst=sqlite3.connect(p+".pre-upgrade.bak"); src.backup(dst); dst.close(); src.close()'
```

Install the pinned runtime dependencies, then start the app. Its startup migration is transactional and preserves accounts, preferences, linked channels and valid delivery history. It removes orphan records and records predating a reused channel ID.

This release invalidates old login cookies and unused Telegram link codes. Users must sign in again; existing linked Telegram channels remain connected. Run the migration once before starting alert workers. Verify the website's `/healthz` endpoint and a normal sign-in before resuming scheduled jobs. Keep the backup until these checks pass. Reverting an application release after a database migration requires restoring its matching database backup.

## Retry queued alerts

Normal `engine/dispatch.py` runs save event payloads and the snapshot cursor in one database transaction. Failed jobs remain in the queue and retry with a delay from one minute up to one hour. Network delivery happens outside database write locks; workers use atomic claims with renewable leases.

Run the retry worker regularly even when a snapshot refresh fails:

```bash
.venv/bin/python engine/dispatch.py --retry-only
```

The `ops/leadtime-retry.service` and `.timer` files are systemd examples. Adjust `User`, `WorkingDirectory`, `EnvironmentFile` and `ExecStart` to the actual deployment. The worker must use the website's database and provider environment. Install both units and enable the timer only after validating those paths and completing the database migration:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now leadtime-retry.timer
```

Use `engine/dispatch.py --dry-run` to inspect newly detected events and queue counts without migrating the database, advancing state, claiming jobs or contacting providers.

Confirmed successes are deduplicated. Delivery is at least once: a process crash after a provider accepts a message but before the success commits can cause a repeat. Historic failed ledger rows do not contain message payloads and cannot be replayed automatically; the dispatcher reports their count. Removing an alert channel cancels its queued jobs.

Only public HTTPS webhook/ntfy destinations are supported. Requests validate all DNS answers, pin connections to a checked address, verify the original hostname's certificate, and disable redirects and environment proxies. Telegram linking requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` and `TELEGRAM_WEBHOOK_SECRET`.

## Monthly publication

Run `pipeline/monthly.py --publish` from the checkout with its normal data-provider environment. A Census API key is required. Refresh failures, missing or stale data, invalid decisions, broken hash chains and failed GitHub pushes abort publication and alert dispatch. The candidate snapshot is staged until validation and publication succeed.

Before logging an active call, the job checks the required due observations for its physical inputs: fixed v4 Taiwan members, the selected memory Korea source and power PPI, plus mature members of the physical components used by active v6 themes. It checks the current and prior-year comparison windows at each source's declared lag, so a fresh dataset-wide date cannot hide a missing member. Newly listed v6 members without prior-year eligibility retain the registered aggregation behavior.

The monthly job and ordinary snapshot builds share a cross-process lock. A durable `data/monthly_pending.json` marker blocks ordinary builds from the start of a refresh until the recorded entry has been pushed and the public snapshot has been promoted. Existing queued alerts can still retry against their saved payloads.

After an interrupted refresh, rerun the complete monthly command; `--no-refresh` cannot establish that partially rewritten inputs are consistent. A publication retry within the same period reuses the existing log entry when its calls match, preserving its original price baselines. Different calls are rejected. An unpublished entry from an older period requires operator recovery before a new period can run. Verify the hash chain and GitHub publication when recovering; do not delete log entries or blindly remove the marker to force a run.

The monthly job must run from a clean release checkout: do not mix source edits or unrelated staged files with its data publication. Correcting numerical code does not authorize rewriting historical pre-registrations or signal-log entries. Keep original research results and publish corrections separately with their data provenance.

See `README.md` for the test command. CI uses temporary databases and mocked providers; tests never send real alerts.
