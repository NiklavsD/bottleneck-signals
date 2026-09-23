# Leadtime: project charter

Agreed with Freddie on 2026-09-23. This file is the standing brief; work proceeds against it without per-step approval.

## What we are building
**Leadtime** is a public product that finds physical supply bottlenecks behind AI, power and robotics in dated, free, point-in-time data, before stock prices move. It then keeps an honest, tamper-evident record of every call.

It does two jobs:
1. **Discovery.** Every month it scans all Taiwan-listed companies' revenue and trade flows by HS code (US imports, Korea exports). It flags unusual acceleration and maps each flag to a supply-chain theme.
2. **Signals.** Each theme gets a pressure index (C, level) and momentum (M). Calls come from pre-registered rules only after a rule passes an out-of-sample test. Themes that haven't passed are shown as "monitoring", with no call.

## Principles (decided)
- **Model makes the public call.** Human views may be logged next to it, labelled and scored separately, and are never edited into the model's record.
- **Coverage:** all themes (AI compute, power, robotics), at both basket and single-company level.
- **Honesty mechanics:**
  - Rules are pre-registered and pushed publicly before results.
  - The signal log is hash-chained.
  - Misses stay visible.
  - Failed tests are published.
- **Free data only for now.** Paid data (e.g. TrendForce) waits until the product shows value.
- **Monetisation is deferred.** The product is free, with an account needed for alerts. The design leaves room for a paid tier.
- **Not investment advice.** The product is impersonal, published on a regular schedule, and gives no personal recommendations. Copy-trading will only ever run through a licensed partner.

## Autonomy
- **Free to do without asking:** build, test, deploy to benbox infrastructure, and publish to the repo.
- **Ask first:** spending money, creating third-party accounts that need a human (Telegram bot, SMS/email providers), and a public launch or marketing.

## Product surface
- **Website:** landing, bottleneck map, theme pages, discovery feed, companies, track record, methodology, account and alert settings.
- **Alert channels:** Discord webhook, Slack/generic webhook, ntfy push, Telegram, email and SMS. The last three switch on when provider credentials are added.
- **Alert events:** call changes, new discovery flags above a threshold, and a monthly digest.
