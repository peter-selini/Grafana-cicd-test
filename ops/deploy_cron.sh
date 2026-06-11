#!/usr/bin/env bash
# Deploy leg: push merged origin/main to Grafana. Runs from cron every 5 min.
# State: ~/grafana-sync/state/last-deployed holds the last deployed SHA; on
# failure it is not advanced, so the deploy retries next cycle.
set -euo pipefail

PYTHON=/usr/bin/python3
SYNC_HOME="$HOME/grafana-sync"
REPO="$SYNC_HOME/repo-deploy"
STATE="$SYNC_HOME/state/last-deployed"
LOG="$SYNC_HOME/logs/deploy.log"

# Keep the last ~1MB once the log passes 5MB (truncating copy preserves the inode).
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5242880 ]; then
    tail -c 1048576 "$LOG" > "$LOG.trim" && cat "$LOG.trim" > "$LOG" && rm -f "$LOG.trim"
fi

exec 9>"$SYNC_HOME/state/deploy.lock"
flock -n 9 || exit 0

# shellcheck disable=SC1090
source "$HOME/.config/grafana-sync/env"

cd "$REPO"
git fetch -q origin main
NEW=$(git rev-parse origin/main)
OLD=$(cat "$STATE" 2>/dev/null || echo "")
if [ "$NEW" = "$OLD" ]; then
    exit 0
fi

echo "$(date -Is) deploying ${OLD:-<none>} -> $NEW"
git reset -q --hard origin/main

# Diff-driven deletes: dashboards whose files were removed between OLD..NEW.
# Tab-separated parsing — folder directories may contain spaces.
if [ -n "$OLD" ]; then
    git -c core.quotePath=false diff --name-status "$OLD..$NEW" -- grafana/ \
        | awk -F'\t' '$1=="D" && $2 ~ /\.json$/ && $2 !~ /\/\.folder\.json$/ {print $2}' \
        | while IFS= read -r f; do
            uid=$(git show "$OLD:$f" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["uid"])')
            "$PYTHON" tools/grafana_sync.py delete --uid "$uid"
        done
fi

GIT_SHA="$NEW" "$PYTHON" tools/grafana_sync.py push

echo "$NEW" > "$STATE"
echo "$(date -Is) deployed $NEW"
