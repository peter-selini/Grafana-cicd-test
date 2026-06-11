#!/usr/bin/env bash
# Deploy leg: push merged origin/main to Grafana. Runs from cron every 5 min.
#
# Pushes are diff-driven: only dashboards whose files changed between the last
# deployed SHA and origin/main are pushed (deletes likewise). Un-exported UI
# edits to untouched dashboards are left alone — they belong to the export leg.
#
# Usage: deploy_cron.sh [--full]
#   --full  reconcile everything: push all tracked dashboards (unchanged ones
#           are skipped server-side by content compare). Reverts any UI drift
#           that hasn't been exported yet — manual/healing use only.
#
# State: ~/grafana-sync/state/last-deployed holds the last deployed SHA; on
# failure it is not advanced, so the deploy retries next cycle.
set -euo pipefail

# Whole body in a function: git updates this file mid-run (reset --hard in
# the same clone), and bash reads scripts incrementally — without this, the
# rest of the OLD script would resume at a byte offset inside the NEW file.
main() {

PYTHON=/usr/bin/python3
SYNC_HOME="$HOME/grafana-sync"
REPO="$SYNC_HOME/repo-deploy"
STATE="$SYNC_HOME/state/last-deployed"
LOG="$SYNC_HOME/logs/deploy.log"
FULL=0
[ "${1:-}" = "--full" ] && FULL=1

# Keep the last ~1MB once the log passes 5MB (truncating copy preserves the inode).
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5242880 ]; then
    tail -c 1048576 "$LOG" > "$LOG.trim" && cat "$LOG.trim" > "$LOG" && rm -f "$LOG.trim"
fi

# Lock shared with export_cron.sh — the two legs never run concurrently.
exec 9>"$SYNC_HOME/state/sync.lock"
flock -n 9 || exit 0

# shellcheck disable=SC1090
source "$HOME/.config/grafana-sync/env"

cd "$REPO"
git fetch -q origin main
NEW=$(git rev-parse origin/main)
OLD=$(cat "$STATE" 2>/dev/null || echo "")
if [ "$NEW" = "$OLD" ] && [ "$FULL" = 0 ]; then
    exit 0
fi

git reset -q --hard origin/main

dashboard_uid() {  # <rev> <path> -> uid
    git show "$1:$2" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["uid"])'
}

if [ "$FULL" = 1 ] || [ -z "$OLD" ]; then
    echo "$(date -Is) full push at $NEW"
    "$PYTHON" tools/grafana_sync.py push
    echo "$NEW" > "$STATE"
    exit 0
fi

echo "$(date -Is) deploying $OLD -> $NEW"

# Tab-separated --name-status parsing — folder directories may contain spaces.
# Rename entries (R###) carry two paths; the new path is the last field.
CHANGES=$(git -c core.quotePath=false diff --name-status "$OLD..$NEW" -- grafana/)

# Deletes: dashboards whose files were removed.
while IFS= read -r f; do
    [ -n "$f" ] || continue
    uid=$(dashboard_uid "$OLD" "$f")
    "$PYTHON" tools/grafana_sync.py delete --uid "$uid"
done < <(awk -F'\t' '$1=="D" && $2 ~ /\.json$/ && $2 !~ /\/\.folder\.json$/ {print $2}' <<< "$CHANGES")

# Pushes: dashboards added/modified/renamed.
UID_ARGS=()
while IFS= read -r f; do
    [ -n "$f" ] || continue
    UID_ARGS+=(--uid "$(dashboard_uid "$NEW" "$f")")
done < <(awk -F'\t' '$1 ~ /^(A|M|R|C)/ && $NF ~ /\.json$/ && $NF !~ /\/\.folder\.json$/ {print $NF}' <<< "$CHANGES")

if [ "${#UID_ARGS[@]}" -gt 0 ]; then
    GIT_SHA="$NEW" "$PYTHON" tools/grafana_sync.py push "${UID_ARGS[@]}"
elif ! git diff --quiet "$OLD..$NEW" -- grafana/ grafana-sync.json; then
    # Folder-level change only (rename / new tracked folder): full push ensures
    # folders; content compare skips unchanged dashboards.
    GIT_SHA="$NEW" "$PYTHON" tools/grafana_sync.py push
else
    echo "$(date -Is) no dashboard changes in $OLD..$NEW"
fi

echo "$NEW" > "$STATE"
echo "$(date -Is) deployed $NEW"

}
main "$@"
