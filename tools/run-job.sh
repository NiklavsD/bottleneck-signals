#!/usr/bin/env bash
# Run a cron job's steps in order (&& semantics), log them, and alert via ntfy on the first failure.
#
#   tools/run-job.sh <job-name> -- <cmd...> [-- <cmd...> ...]
#
# Output goes to data/raw/<job-name>.log with a timestamped START/END line per step. The project .env is
# sourced (exported) so steps inherit it; OPS_NTFY_TOPIC from it names the ntfy topic for failure alerts.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if [ $# -lt 3 ] || [ "$2" != "--" ]; then
  echo "usage: $0 <job-name> -- <cmd...> [-- <cmd...> ...]" >&2
  exit 2
fi
JOB="$1"; shift 2
case "$JOB" in *[!A-Za-z0-9._-]*|"") echo "bad job name: $JOB" >&2; exit 2 ;; esac

if [ -f ./.env ]; then set -a; . ./.env; set +a; fi
mkdir -p data/raw
LOG="data/raw/$JOB.log"

ts() { date '+%Y-%m-%d %H:%M:%S %z'; }

alert() {  # $1 = step, $2 = exit code
  local tail_lines
  tail_lines="$(tail -n 5 "$LOG" 2>/dev/null | cut -c1-300)"
  if [ -z "${OPS_NTFY_TOPIC:-}" ]; then
    echo "[$(ts)] ALERT not sent: OPS_NTFY_TOPIC is not set" >> "$LOG"
    return
  fi
  curl -s -o /dev/null --max-time 20 \
    -H "Title: Leadtime job failed: $JOB" -H "Priority: high" \
    -d "$1 exited $2; tail: $tail_lines" "https://ntfy.sh/$OPS_NTFY_TOPIC" \
    || echo "[$(ts)] ALERT send failed (curl exit $?)" >> "$LOG"
}

run_step() {
  local step="$*" rc
  echo "[$(ts)] [$JOB] START $step" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  rc=$?
  echo "[$(ts)] [$JOB] END $step exit $rc" >> "$LOG"
  if [ $rc -ne 0 ]; then
    alert "$step" "$rc"
    exit $rc
  fi
}

step=()
for arg in "$@"; do
  if [ "$arg" = "--" ]; then
    [ ${#step[@]} -gt 0 ] && run_step "${step[@]}"
    step=()
  else
    step+=("$arg")
  fi
done
[ ${#step[@]} -gt 0 ] && run_step "${step[@]}"
exit 0
