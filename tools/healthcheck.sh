#!/usr/bin/env bash
# Uptime check: alert via ntfy (topic OPS_NTFY_TOPIC from the project .env) unless /healthz returns 200.
# /healthz returns 503 when the site's prices or snapshot are stale.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
if [ -f ./.env ]; then set -a; . ./.env; set +a; fi
URL="${HEALTHZ_URL:-https://leadtime.benbox.dev/healthz}"
LOG="data/raw/healthcheck.log"
mkdir -p data/raw

body="$(curl -s --max-time 20 -w '\n%{http_code}' "$URL" 2>/dev/null)"
code="${body##*$'\n'}"
body="$(printf '%s' "${body%$'\n'*}" | tr '\n' ' ' | cut -c1-300)"
STATE="data/raw/.healthcheck_last_alert"
if [ "$code" = "200" ]; then rm -f "$STATE"; exit 0; fi
# while the site stays unhealthy, re-alert at most every 6 hours instead of every hourly run
now=$(date +%s); last=$(cat "$STATE" 2>/dev/null || echo 0)
if [ $((now - last)) -lt 21600 ]; then exit 1; fi
echo "$now" > "$STATE"

echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] healthz $code: $body" >> "$LOG"
if [ -n "${OPS_NTFY_TOPIC:-}" ]; then
  curl -s -o /dev/null --max-time 20 \
    -H "Title: Leadtime job failed: healthcheck" -H "Priority: high" \
    -d "healthz returned ${code:-no response}; body: $body" "https://ntfy.sh/$OPS_NTFY_TOPIC"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] ALERT not sent: OPS_NTFY_TOPIC is not set" >> "$LOG"
fi
exit 1
