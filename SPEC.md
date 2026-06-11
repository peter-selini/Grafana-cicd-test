# SPEC — Grafana-cicd-test (agent orientation)

One-page summary for agents/tools. Humans: see `README.md`. Deep operational
context and gotchas: `.claude/skills/grafana-dashboards-cicd/SKILL.md`.

## What this repo is

Source of truth for the **`CICD - *` dashboard folders** on
`https://selini-grafana.selini.tech` (Grafana 10.4.2 Enterprise, private IP —
reachable only from inside the network). It manages **parallel copies** of the
original ("model") dashboards, every managed UID prefixed `cicd-`; the
originals are never written by this pipeline and remain until deprecated.

## Data flow

```
PR merged to main ──(deploy cron, */5, internal box)──> Grafana   [repo wins]
Grafana UI edits  ──(export cron, :07/:22/:37/:52)────> PR branch grafana-auto-export
GitHub Actions (validate.yml) ── offline JSON validation only; cannot reach Grafana
```

## Layout

| Path | What |
|---|---|
| `grafana/<Folder Title>/*.json` | Dashboard JSON, normalized (sorted keys; `id`/`version`/`iteration` stripped; filename = slug of title) |
| `grafana/<Folder Title>/.folder.json` | Folder identity: uid (authoritative) + title. Git-Sync-compatible shape |
| `grafana-sync.json` | Tracked folders (uid + title) and Grafana URL — the allowlist of everything pushes may touch |
| `tools/grafana_sync.py` | The only tool. Stdlib-only, Python ≥3.9. Subcommands: `list-folders`, `track`, `fork`, `pull`, `push`, `delete`, `status`, `validate` |
| `ops/deploy_cron.sh` | main → Grafana; diff-driven (only changed/deleted files in merged commits); `--full` = manual reconcile |
| `ops/export_cron.sh` | Grafana drift → rolling PR branch; skips while a deploy is pending (conflict guard) |
| `ops/crontab.txt` | The two cron lines (installed on the internal sync host) |
| `.github/workflows/validate.yml` | CI: runs `grafana_sync.py validate` on PRs and main |

## Invariants (do not break)

1. Only `cicd-` prefixed UIDs in `grafana-sync.json` and `grafana/` — an
   un-prefixed UID would make a push **move a user's original dashboard**.
2. Dashboard files must be in canonical form (`canon()` in the CLI); hand-written
   files in another shape cause phantom drift.
3. `validate` must pass: uid unique repo-wide, filename == slug(title), every
   dir has `.folder.json`, dirs ⟷ config consistent.
4. Repo wins on conflict; deploys are scoped to the merged diff.

## Auth & host state

- `GRAFANA_API_KEY` (Editor service-account token) in `~/.config/grafana-sync/env`
  on the sync host — never in the repo or CLI flags.
- Sync host state: clones `~/grafana-sync/repo-{deploy,export}`,
  `~/grafana-sync/state/last-deployed` (SHA), logs in `~/grafana-sync/logs/`
  (empty log = healthy no-op). Currently Peter's box; designed to relocate.

## Quick commands

```bash
source ~/.config/grafana-sync/env
python3 tools/grafana_sync.py status --diff     # drift? (exit 0/1/2 = sync/drift/error)
python3 tools/grafana_sync.py push --dry-run    # what would a deploy do
~/grafana-sync/repo-deploy/ops/deploy_cron.sh   # deploy now (safe by hand; flock'd)
```

## Known quirks (details in the skill)

Enterprise RBAC 403s on non-existent UIDs (treated as 404); system python is
3.9; ops scripts wrap body in `main()` (self-update hazard); one corrupt
dashboard in the original Crypto folder (uid `a9803566…`, never imported);
UID cap 40 chars (`derive_uid` falls back to sha1). Native Git Sync replaces
the cron legs after a Grafana v13 upgrade — layout is already compatible.
