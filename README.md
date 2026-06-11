# Grafana Dashboards as Code

This repo is the **source of truth** for Selini's CICD-managed Grafana
dashboards (`https://selini-grafana.selini.tech`). Tracked dashboards live as
normalized JSON under [grafana/](grafana/), one directory per Grafana folder.
The layout is compatible with Grafana's native Git Sync, so when our instance
is upgraded to v13+ we can switch to native sync without restructuring.

## Parallel-run model

The repo manages **copies** of the original dashboards, not the originals.
Each tracked folder is a fork: `Crypto` → `CICD - Crypto`, with every
dashboard UID prefixed `cicd-` (and cross-dashboard links rewritten to follow
the fork). The original "model" dashboards and their folders are **never
written to by this pipeline** — pushes can only create/update `CICD - *`
folders, because only those UIDs appear in [grafana-sync.json](grafana-sync.json)
and the dashboard files. The originals stay editable in the UI as before,
until they are deprecated and the `CICD - *` copies become canonical (at which
point they can be renamed in a PR).

To bring another original folder under management:
`track <orig-folder-uid>` (imports it), then `fork <orig-folder-uid>`
(re-points the repo at a `cicd-` copy), commit and merge — the deploy leg
creates the new folder in Grafana. The originals are not re-synced afterwards;
re-import manually if a model dashboard changes and you want the copy updated.

## How it works

```
Grafana UI edits ──(export cron, every 15 min)──> PR on branch grafana-auto-export
PR merged to main ──(deploy cron, every 5 min)──> pushed to Grafana
```

- **You can keep editing dashboards in the Grafana UI.** Edits in tracked
  folders are exported automatically as a PR within ~15 minutes. Review and
  merge it to make the change permanent.
- **Or edit the JSON directly**: open a PR changing files under `grafana/`.
  CI validates it; once merged, the deploy cron pushes it to Grafana within
  ~5 minutes — overwriting any unmerged UI edits to the same dashboard
  (**the repo wins on conflict**).
- **To delete a dashboard**, delete its JSON file in a PR. The deploy cron
  removes it from Grafana. (UI deletions show up in the export PR as file
  deletions — merging confirms them; closing the PR means the deploy cron
  will restore the dashboard.)
- **New dashboards created in the UI** inside a tracked folder are picked up
  by the export PR automatically. New *folders* are ignored until tracked.

## Conflict semantics

Two people can touch the same dashboard at once — one in the UI, one via a PR.
The rules, in order of what actually happens:

1. **Deploys are diff-driven.** Merging a PR pushes only the dashboards whose
   files changed in that merge. UI edits to *other* dashboards are never
   touched by a deploy; they ride along until the export cron PRs them.
2. **Same dashboard, both sides: the repo wins.** When a merged PR touches a
   dashboard that also has un-exported UI edits, the deploy overwrites the UI
   version within ~5 minutes. If the export cron captured the UI edit first,
   it is preserved on the `grafana-auto-export` PR (review it there — it may
   need manual reconciliation against the new main); if not, it is lost.
   Exposure window for a UI edit: up to ~15 min (export interval) plus
   however long the export PR stays unmerged.
3. **The export leg never reverts repo changes.** It refuses to run while a
   merged-but-not-yet-deployed commit is pending (conflict guard), so drift it
   captures is always genuine UI edits on top of the deployed state.
4. **The two cron legs never overlap** (shared lock); a leg that finds the
   lock held skips and retries on its next tick.
5. **UI vs UI** is unchanged native Grafana behavior (optimistic locking —
   "someone else updated this dashboard" on save).

Practical guidance: for substantial UI work, merge the auto-export PR (or run
`pull` + commit manually) before someone lands a JSON change to the same
dashboard. Healing command after any confusion:
`ops/deploy_cron.sh --full` reconciles Grafana to main (skips unchanged,
reverts un-exported UI drift).

## Tooling

Everything is one stdlib-only CLI: [tools/grafana_sync.py](tools/grafana_sync.py).

```bash
export GRAFANA_API_KEY=...                      # service account token (Editor)
python3 tools/grafana_sync.py list-folders      # see folders + tracked status
python3 tools/grafana_sync.py track <uid>       # start tracking a folder
python3 tools/grafana_sync.py status [--diff]   # drift report (exit 1 = drift)
python3 tools/grafana_sync.py pull              # Grafana -> repo (mirror)
python3 tools/grafana_sync.py push [--dry-run]  # repo -> Grafana (skips unchanged)
python3 tools/grafana_sync.py validate          # offline checks (what CI runs)
python3 tools/grafana_sync.py fork <uid>        # re-point a tracked folder at a CICD copy (offline)
```

Tracked folders are listed in [grafana-sync.json](grafana-sync.json). Dashboard
JSON is normalized on pull: top-level `id`, `version`, `iteration` stripped,
keys sorted, 2-space indent — this keeps git diffs stable and push/pull
idempotent. Datasource UIDs, variable selections, and time ranges are kept
verbatim.

## Ops

The two cron legs run on an internal box (GitHub runners cannot reach Grafana —
it is on a private IP). See [ops/](ops/):

- [ops/deploy_cron.sh](ops/deploy_cron.sh) — every 5 min: fetch `origin/main`,
  delete dashboards whose files were removed, push only the dashboards changed
  in the merged commits (`--full` for a manual full reconcile). State in
  `~/grafana-sync/state/last-deployed`.
- [ops/export_cron.sh](ops/export_cron.sh) — every 15 min: skip if a deploy is
  pending (conflict guard), otherwise pull UI drift onto the rolling branch
  `grafana-auto-export` and force-push + open a PR.
- Install: `( crontab -l; cat ops/crontab.txt ) | crontab -` after cloning the
  repo to `~/grafana-sync/repo-deploy` and `~/grafana-sync/repo-export`, and
  writing the token to `~/.config/grafana-sync/env`.

Logs: `~/grafana-sync/logs/{deploy,export}.log` (self-trimmed at 5 MB).

Rollout is two-phase: **phase 1** uses a Viewer-role token (read-only: import +
drift reports, no cron legs) until the imported JSON is verified; **phase 2**
swaps in an Editor-role token and enables the cron legs. The cron host is
currently Peter's box; it will move to a dedicated box later — the only
host-specific pieces are the two clones, `~/.config/grafana-sync/env`, and the
crontab lines.

## Limitations

- Dashboards and folder titles only. **Not tracked:** alert rules, data
  sources, library panel definitions (dashboard refs to library panels are
  kept, and push works only if the panel exists in the instance).
- Single Grafana instance. Cross-instance promotion would need datasource UID
  mapping.
- Nested folders: not used (instance is on 10.4); layout supports up to 4
  levels for future Git Sync compatibility.

## Future: native Git Sync

When Grafana is upgraded to v12.4+/v13, connect Git Sync to this repo with
path `grafana/`, then delete the two crontab lines and `~/grafana-sync/`.
Native sync replaces both cron legs (UI saves become commits/PRs directly).
Keep `validate.yml` and the CLI for ad-hoc use.
