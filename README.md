# Grafana Dashboards as Code

This repo is the **source of truth** for Selini's Grafana dashboards
(`https://selini-grafana.selini.tech`). Tracked dashboards live as normalized
JSON under [grafana/](grafana/), one directory per Grafana folder. The layout is
compatible with Grafana's native Git Sync, so when our instance is upgraded to
v13+ we can switch to native sync without restructuring.

## How it works

```
Grafana UI edits ──(export cron, every 30 min)──> PR on branch grafana-auto-export
PR merged to main ──(deploy cron, every 5 min)──> pushed to Grafana
```

- **You can keep editing dashboards in the Grafana UI.** Edits in tracked
  folders are exported automatically as a PR within ~30 minutes. Review and
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
  delete dashboards whose files were removed, push the rest (no-op when
  nothing changed). State in `~/grafana-sync/state/last-deployed`.
- [ops/export_cron.sh](ops/export_cron.sh) — every 30 min: skip if a deploy is
  pending (conflict guard), otherwise pull UI drift onto the rolling branch
  `grafana-auto-export` and force-push + open a PR.
- Install: `( crontab -l; cat ops/crontab.txt ) | crontab -` after cloning the
  repo to `~/grafana-sync/repo-deploy` and `~/grafana-sync/repo-export`, and
  writing the token to `~/.config/grafana-sync/env`.

Logs: `~/grafana-sync/logs/{deploy,export}.log` (self-trimmed at 5 MB).

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
