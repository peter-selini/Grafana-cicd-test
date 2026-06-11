#!/usr/bin/env bash
# Auto-export leg: capture Grafana UI edits as a PR. Runs from cron every 15 min.
#
# Conflict guard: if origin/main hasn't been deployed yet (deploy leg pending or
# failing), skip — otherwise we'd misread an undeployed repo change as a UI edit
# and open a PR reverting it. Repo is the source of truth.
set -euo pipefail

# Whole body in a function: git updates this file mid-run (checkout/reset in
# the same clone), and bash reads scripts incrementally — without this, the
# rest of the OLD script would resume at a byte offset inside the NEW file.
main() {

PYTHON=/usr/bin/python3
SYNC_HOME="$HOME/grafana-sync"
REPO="$SYNC_HOME/repo-export"
STATE="$SYNC_HOME/state/last-deployed"
LOG="$SYNC_HOME/logs/export.log"
BRANCH=grafana-auto-export

if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5242880 ]; then
    tail -c 1048576 "$LOG" > "$LOG.trim" && cat "$LOG.trim" > "$LOG" && rm -f "$LOG.trim"
fi

# Lock shared with deploy_cron.sh — the two legs never run concurrently.
exec 9>"$SYNC_HOME/state/sync.lock"
flock -n 9 || exit 0

# shellcheck disable=SC1090
source "$HOME/.config/grafana-sync/env"

cd "$REPO"
git fetch -q origin
MAIN=$(git rev-parse origin/main)
DEPLOYED=$(cat "$STATE" 2>/dev/null || echo "")
if [ "$MAIN" != "$DEPLOYED" ]; then
    echo "$(date -Is) deploy pending ($DEPLOYED != $MAIN); skipping export"
    exit 0
fi

git checkout -q -B "$BRANCH" origin/main

if DRIFT=$("$PYTHON" tools/grafana_sync.py status 2>&1); then
    exit 0   # no drift
fi
echo "$DRIFT"

"$PYTHON" tools/grafana_sync.py pull
git add -A grafana/ grafana-sync.json
if git diff --cached --quiet; then
    exit 0
fi

git commit -q -m "Auto-export: UI changes from selini-grafana" -m "$(git diff --cached --stat | tail -1)"
git push -qf origin "$BRANCH"
echo "$(date -Is) pushed UI drift to $BRANCH"

# Ensure a PR exists (rolling branch — force-push keeps a single PR current).
if command -v gh > /dev/null 2>&1; then
    gh pr create --base main --head "$BRANCH" \
        --title "Auto-export: Grafana UI changes" \
        --body "Dashboard edits made in the Grafana UI, exported by ops/export_cron.sh." \
        2>&1 | grep -v "already exists" || true
elif [ -n "${GITHUB_TOKEN:-}" ]; then
    "$PYTHON" ops/open_pr.py --head "$BRANCH" --base main \
        --title "Auto-export: Grafana UI changes" \
        --body "Dashboard edits made in the Grafana UI, exported by ops/export_cron.sh."
else
    echo "No gh/GITHUB_TOKEN — open the PR manually:"
    echo "  https://github.com/peter-selini/Grafana-cicd-test/pull/new/$BRANCH"
fi

}
main "$@"
