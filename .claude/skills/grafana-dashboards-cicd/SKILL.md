---
name: grafana-dashboards-cicd
description: Operations guide and hard-won gotchas for the Grafana-cicd-test repo — dashboards-as-code for selini-grafana.selini.tech via tools/grafana_sync.py and two cron legs (deploy/export). Invoke when working in this repo; when touching grafana_sync.py, ops/*.sh, or files under grafana/; when debugging the deploy/export crons or ~/grafana-sync logs; when a push/pull/status behaves unexpectedly (403s, drift, save-loops); or when adding/forking a tracked folder.
---

# grafana-dashboards-cicd

This repo is the source of truth for the `CICD - *` folders on
`https://selini-grafana.selini.tech` (Grafana **10.4.2 Enterprise**, private IP
— GitHub-hosted runners cannot reach it; everything Grafana-touching runs from
an internal box). Read `SPEC.md` first for the 1-minute orientation; this skill
holds the operational depth.

## The non-negotiable invariants

1. **The repo manages parallel COPIES, never the originals.** Every managed UID
   (folders and dashboards) is prefixed `cicd-`. If you see an un-prefixed UID
   in `grafana-sync.json` or under `grafana/`, something is wrong — a push with
   an original dashboard UID would **move the user's original dashboard** into
   a CICD folder (Grafana UIDs are instance-unique; `overwrite:true` moves, it
   does not copy).
2. **Repo wins on conflict.** Deploys overwrite UI edits to the same dashboard.
   Deploys are diff-driven (only dashboards changed in merged commits), so UI
   edits to *other* dashboards are never collateral damage.
3. **Normalization is what makes everything idempotent.** Pull and every
   comparison strip top-level `id`/`version`/`iteration` and serialize
   `json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False) + "\n"`.
   Never write a dashboard file by hand in a different shape — run it through
   `canon()` (see how `fork` does it) or pull will show phantom drift.

## Auth & environment

- Token: `~/.config/grafana-sync/env` (chmod 600) → `export GRAFANA_API_KEY=...`
  (service account, Editor). The CLI reads the env var only — no `--api-key`
  flag, by design (ps leakage).
- TLS verification is off by default (internal cert), matching house scripts.
- `GITHUB_TOKEN` in the same env file enables auto-PR from the export leg
  (stdlib `ops/open_pr.py`); without it (and without `gh`) the leg pushes the
  branch and logs the create-PR URL.

## Gotchas that cost real debugging time

- **Enterprise RBAC returns 403, not 404, for non-existent UIDs.** Probing
  `GET /api/folders/<uid>` or `/api/dashboards/uid/<uid>` for something that
  doesn't exist yields 403 "permissions needed: folders:read". The CLI treats
  403+404 both as not-found on reads (`Client.NOT_FOUND`); genuine permission
  problems still fail loudly on the write. Don't "fix" a 403 here by escalating
  the token.
- **`/usr/bin/python3` on the cron host is 3.9.** The CLI needs
  `from __future__ import annotations` — don't add 3.10+-only runtime syntax.
  Dev shells pick up Anaconda 3.10 via PATH, so this only bites under cron.
- **Cron scripts self-update mid-run.** `git reset/checkout` inside the clone
  replaces the running script file, and bash reads scripts incrementally — the
  old process resumes at a byte offset inside the new file. Both ops scripts
  wrap their body in `main() { ... }; main "$@"`. Keep it that way for any new
  script that lives in the synced clone.
- **One corrupt dashboard exists server-side**: original Crypto folder, uid
  `a9803566-2e60-4234-b28a-3110503a5809` ("Mimid Feed Latencies (Minimums)") —
  Grafana itself 500s on GET ("dashboard data is invalid"). pull/status skip
  and warn; push heals such dashboards by overwriting. It was never imported.
- **UID length cap is 40 chars.** `derive_uid()` uses `cicd-<old>` when it
  fits, else `cicd-` + sha1[:12] (matters for UUID-style UIDs, 36+5 > 40).
- **Filenames are slugs of titles** (`slugify(title) + ".json"`); validation
  enforces it, and pull renames files when titles change (matched by uid).
  Folder directories are the folder *title* (may contain spaces — all shell
  parsing in ops scripts is tab-separated for this reason).
- **Empty cron logs are healthy.** Both legs exit silently on their no-op
  paths; lines appear only for real work or skip-reasons. Log files self-trim
  at 5 MB (truncating copy, preserves the cron-held inode).

## Operational recipes

- **Track a new folder (parallel-run):** `track <orig-folder-uid>` then
  `fork <orig-folder-uid>` (offline; deterministic UIDs; rewrites cross-links),
  commit, merge — deploy creates the `CICD - *` folder. Originals are NOT
  re-synced after fork; refresh manually if a model dashboard evolves.
- **Force an immediate deploy:** `~/grafana-sync/repo-deploy/ops/deploy_cron.sh`
  (cron-safe to run by hand; shared flock with the export leg).
- **Heal any divergence (repo wins):** `deploy_cron.sh --full` — full push,
  unchanged dashboards skipped by content compare; reverts un-exported UI
  drift, so warn users first.
- **Check drift:** `python3 tools/grafana_sync.py status [--diff]`;
  exit 0 = in sync, 1 = drift, 2 = error (the cron contract).
- **Delete a dashboard:** delete its file in a PR; the deploy leg's
  diff-driven delete removes it from Grafana. `push --prune` exists but is
  manual-only. Restoring = revert the commit.
- **Conflict guard:** the export leg refuses to run while
  `state/last-deployed` ≠ `origin/main` — if exports seem stuck, check whether
  a deploy is failing (state file not advancing, see `logs/deploy.log`).

## Future migration (don't lose this)

When Grafana is upgraded to v12.4+/v13, native **Git Sync** replaces both cron
legs: connect it to this repo with path `grafana/` (layout is already
compatible — classic dashboard JSON + `.folder.json` per dir), then remove the
two crontab lines and `~/grafana-sync/`. Keep `validate.yml` and the CLI.
The cron host is currently Peter's box and is expected to move to a dedicated
box: the only host state is the two clones under `~/grafana-sync/`, the env
file, and the two crontab lines in `ops/crontab.txt`.
