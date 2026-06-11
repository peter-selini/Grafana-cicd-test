#!/usr/bin/env python3
"""
grafana_sync — manage Grafana dashboards as code.

The repo is the source of truth. Dashboards live under grafana/<Folder Title>/
as normalized JSON (sorted keys, volatile fields stripped), one directory per
tracked Grafana folder, with a .folder.json identity file per directory
(Git-Sync-compatible layout).

Subcommands:
    list-folders          List Grafana folders with dashboard counts
    track <folder-uid>    Start tracking a folder (adds to config + initial pull)
    pull                  Mirror tracked folders from Grafana into the repo
    push                  Push repo dashboards to Grafana (skips unchanged)
    delete --uid <uid>    Delete a dashboard from Grafana
    status                Report drift between repo and Grafana (exit 1 = drift)
    validate              Offline validation of repo contents (used by CI)

Auth: GRAFANA_API_KEY environment variable (service account token, Editor role).
"""

import argparse
import difflib
import json
import logging
import os
import re
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger("grafana_sync")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "grafana-sync.json"
GRAFANA_DIRNAME = "grafana"
FOLDER_META = ".folder.json"

# Top-level dashboard keys managed by Grafana, not us — stripped on pull and
# before any comparison. Nothing else is stripped (datasource UIDs, template
# variable selections and time ranges are kept verbatim).
_STRIP_KEYS = ("id", "version", "iteration")

MAX_NESTING = 4


class GrafanaError(RuntimeError):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# HTTP


def _ssl_ctx(verify: bool):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Client:
    def __init__(self, base_url: str, api_key: str, verify_tls: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.verify_tls = verify_tls

    def request(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, context=_ssl_ctx(self.verify_tls)) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except HTTPError as e:
            body = e.read().decode(errors="replace")
            raise GrafanaError(f"Grafana API {e.code} on {method} {path}: {body}", e.code)
        except URLError as e:
            raise GrafanaError(f"Cannot reach Grafana at {self.base_url}: {e.reason}")

    def get(self, path: str):
        return self.request("GET", path)

    # --- API wrappers ---

    def folders(self) -> list[dict]:
        return self.get("/api/folders?limit=1000")

    def folder(self, uid: str) -> dict:
        return self.get(f"/api/folders/{quote(uid)}")

    def create_folder(self, uid: str, title: str) -> dict:
        return self.request("POST", "/api/folders", {"uid": uid, "title": title})

    def update_folder_title(self, uid: str, title: str) -> dict:
        return self.request(
            "PUT", f"/api/folders/{quote(uid)}", {"title": title, "overwrite": True}
        )

    def search_dashboards(self, folder_uid: str) -> list[dict]:
        return self.get(
            f"/api/search?folderUIDs={quote(folder_uid)}&type=dash-db&limit=5000"
        )

    def dashboard(self, uid: str) -> dict | None:
        """Fetch a dashboard model by UID, or None if it doesn't exist."""
        try:
            return self.get(f"/api/dashboards/uid/{quote(uid)}").get("dashboard", {})
        except GrafanaError as e:
            if e.code == 404:
                return None
            raise

    def save_dashboard(self, dashboard: dict, folder_uid: str, message: str) -> dict:
        return self.request(
            "POST",
            "/api/dashboards/db",
            {
                "dashboard": dashboard,
                "folderUid": folder_uid,
                "overwrite": True,
                "message": message,
            },
        )

    def delete_dashboard(self, uid: str) -> bool:
        """Delete by UID. Returns False if it was already gone."""
        try:
            self.request("DELETE", f"/api/dashboards/uid/{quote(uid)}")
            return True
        except GrafanaError as e:
            if e.code == 404:
                return False
            raise


# ---------------------------------------------------------------------------
# Normalization / repo layout


def normalize(dashboard: dict) -> dict:
    d = dict(dashboard)
    for key in _STRIP_KEYS:
        d.pop(key, None)
    return d


def canon(dashboard: dict) -> str:
    return json.dumps(normalize(dashboard), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def slugify(title: str) -> str:
    s = title.lower().replace(" ", "-").replace("/", "-")
    s = re.sub(r"[^a-z0-9_-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "untitled"


def folder_dirname(title: str) -> str:
    name = title.replace("/", "-").strip().lstrip(".")
    return name or "untitled"


def grafana_dir(repo_root: Path) -> Path:
    return repo_root / GRAFANA_DIRNAME


def load_config(repo_root: Path) -> dict:
    path = repo_root / CONFIG_NAME
    if not path.exists():
        return {"grafana_url": "", "folders": []}
    with open(path) as f:
        return json.load(f)


def save_config(repo_root: Path, config: dict) -> None:
    with open(repo_root / CONFIG_NAME, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def folder_meta(uid: str, title: str) -> dict:
    return {
        "apiVersion": "folder.grafana.app/v1beta1",
        "kind": "Folder",
        "metadata": {"name": uid, "uid": uid},
        "spec": {"title": title},
    }


def write_folder_meta(dirpath: Path, uid: str, title: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    with open(dirpath / FOLDER_META, "w") as f:
        json.dump(folder_meta(uid, title), f, indent=2)
        f.write("\n")


def local_folder_dirs(repo_root: Path) -> dict[str, Path]:
    """Map folder UID -> directory, from .folder.json files under grafana/."""
    result = {}
    base = grafana_dir(repo_root)
    if not base.exists():
        return result
    for meta_path in sorted(base.rglob(FOLDER_META)):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            uid = meta.get("metadata", {}).get("uid")
            if uid:
                result[uid] = meta_path.parent
        except (json.JSONDecodeError, OSError):
            continue
    return result


def local_dashboards(dirpath: Path) -> dict[str, Path]:
    """Map dashboard UID -> file path for one folder directory."""
    result = {}
    for path in sorted(dirpath.glob("*.json")):
        if path.name == FOLDER_META:
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        uid = data.get("uid")
        if uid:
            result[uid] = path
    return result


def selected_folders(config: dict, only: list[str] | None) -> list[dict]:
    folders = config.get("folders", [])
    if only:
        by_uid = {f["uid"]: f for f in folders}
        missing = [u for u in only if u not in by_uid]
        if missing:
            raise GrafanaError(f"Folder uid(s) not tracked: {', '.join(missing)}")
        return [by_uid[u] for u in only]
    return folders


# ---------------------------------------------------------------------------
# Subcommands


def cmd_list_folders(client: Client, repo_root: Path, args) -> int:
    config = load_config(repo_root)
    tracked = {f["uid"] for f in config.get("folders", [])}
    folders = client.folders()
    rows = []
    for f in sorted(folders, key=lambda x: x.get("title", "").lower()):
        count = len(client.search_dashboards(f["uid"]))
        rows.append((f["uid"], f.get("title", ""), count, "yes" if f["uid"] in tracked else ""))
    width_uid = max([len(r[0]) for r in rows] + [3])
    width_title = max([len(r[1]) for r in rows] + [5])
    print(f"{'UID':<{width_uid}}  {'TITLE':<{width_title}}  {'DASHBOARDS':>10}  TRACKED")
    for uid, title, count, tracked_flag in rows:
        print(f"{uid:<{width_uid}}  {title:<{width_title}}  {count:>10}  {tracked_flag}")
    return 0


def cmd_track(client: Client, repo_root: Path, args) -> int:
    config = load_config(repo_root)
    if any(f["uid"] == args.folder_uid for f in config.get("folders", [])):
        logger.info(f"Folder {args.folder_uid} already tracked")
    else:
        remote = client.folder(args.folder_uid)
        config.setdefault("folders", []).append(
            {"uid": remote["uid"], "title": remote["title"]}
        )
        save_config(repo_root, config)
        logger.info(f"Tracking folder '{remote['title']}' ({remote['uid']})")
    args.folder = [args.folder_uid]
    args.keep_missing = False
    return cmd_pull(client, repo_root, args)


def _sync_folder_identity(client: Client, repo_root: Path, config: dict, entry: dict) -> Path | None:
    """Align local dir/.folder.json/config with the remote folder (matched by uid).

    Returns the folder directory, or None if the folder is gone remotely.
    """
    try:
        remote = client.folder(entry["uid"])
    except GrafanaError as e:
        if e.code == 404:
            logger.warning(f"Folder '{entry['title']}' ({entry['uid']}) not found in Grafana — skipping")
            return None
        raise

    dirs = local_folder_dirs(repo_root)
    dirpath = dirs.get(entry["uid"])
    want = grafana_dir(repo_root) / folder_dirname(remote["title"])

    if dirpath is None:
        dirpath = want
    elif dirpath != want:
        logger.info(f"Folder renamed in Grafana: {dirpath.name} -> {want.name}")
        want.parent.mkdir(parents=True, exist_ok=True)
        dirpath.rename(want)
        dirpath = want

    write_folder_meta(dirpath, remote["uid"], remote["title"])
    if entry["title"] != remote["title"]:
        entry["title"] = remote["title"]
        save_config(repo_root, config)
    return dirpath


def cmd_pull(client: Client, repo_root: Path, args) -> int:
    config = load_config(repo_root)
    for entry in selected_folders(config, args.folder):
        dirpath = _sync_folder_identity(client, repo_root, config, entry)
        if dirpath is None:
            continue

        remote_list = client.search_dashboards(entry["uid"])
        local = local_dashboards(dirpath)
        remote_uids = set()

        for item in remote_list:
            uid = item["uid"]
            remote_uids.add(uid)
            dashboard = client.dashboard(uid)
            if dashboard is None:
                continue
            text = canon(dashboard)
            want = dirpath / f"{slugify(dashboard.get('title', uid))}.json"
            existing = local.get(uid)
            if existing is not None and existing != want:
                existing.unlink()
                logger.info(f"Renamed {existing.name} -> {want.name}")
            if want.exists():
                with open(want) as f:
                    if f.read() == text:
                        continue
            with open(want, "w") as f:
                f.write(text)
            logger.info(f"Pulled {uid} -> {want.relative_to(repo_root)}")

        if not args.keep_missing:
            for uid, path in local.items():
                if uid not in remote_uids:
                    path.unlink()
                    logger.info(f"Removed {path.relative_to(repo_root)} (gone from Grafana)")

    logger.info("Pull complete")
    return 0


def cmd_push(client: Client, repo_root: Path, args) -> int:
    problems = validate_repo(repo_root)
    if problems:
        for p in problems:
            logger.error(p)
        logger.error("Validation failed — not pushing")
        return 1

    config = load_config(repo_root)
    dirs = local_folder_dirs(repo_root)
    message = f"grafana_sync push {os.environ.get('GIT_SHA', '')}".strip()
    errors = 0

    for entry in selected_folders(config, args.folder):
        dirpath = dirs.get(entry["uid"])
        if dirpath is None:
            logger.warning(f"No local directory for tracked folder '{entry['title']}' — skipping")
            continue

        # Ensure the folder exists with the repo's title (repo wins).
        try:
            remote_folder = client.folder(entry["uid"])
            if remote_folder["title"] != entry["title"]:
                if args.dry_run:
                    logger.info(f"[dry-run] Would rename folder to '{entry['title']}'")
                else:
                    client.update_folder_title(entry["uid"], entry["title"])
                    logger.info(f"Renamed folder {entry['uid']} to '{entry['title']}'")
        except GrafanaError as e:
            if e.code != 404:
                raise
            if args.dry_run:
                logger.info(f"[dry-run] Would create folder '{entry['title']}' ({entry['uid']})")
            else:
                client.create_folder(entry["uid"], entry["title"])
                logger.info(f"Created folder '{entry['title']}' ({entry['uid']})")

        local = local_dashboards(dirpath)
        for uid, path in local.items():
            with open(path) as f:
                dashboard = json.load(f)
            try:
                remote = client.dashboard(uid)
                if remote is not None and canon(remote) == canon(dashboard):
                    logger.info(f"Unchanged: {path.name}")
                    continue
                if args.dry_run:
                    logger.info(f"[dry-run] Would push {path.name} ({uid})")
                    continue
                result = client.save_dashboard(normalize(dashboard), entry["uid"], message)
                logger.info(f"Pushed {path.name}: {result.get('status', 'ok')}")
            except GrafanaError as e:
                logger.error(f"Failed to push {path.name}: {e}")
                errors += 1

        if args.prune:
            for item in client.search_dashboards(entry["uid"]):
                if item["uid"] not in local:
                    if args.dry_run:
                        logger.info(f"[dry-run] Would delete {item['uid']} ('{item.get('title')}')")
                    else:
                        client.delete_dashboard(item["uid"])
                        logger.info(f"Deleted {item['uid']} ('{item.get('title')}')")

    if errors:
        return 1
    logger.info("Push complete" + (" (dry run)" if args.dry_run else ""))
    return 0


def cmd_delete(client: Client, repo_root: Path, args) -> int:
    if client.delete_dashboard(args.uid):
        logger.info(f"Deleted dashboard {args.uid}")
    else:
        logger.info(f"Dashboard {args.uid} already absent")
    return 0


def cmd_status(client: Client, repo_root: Path, args) -> int:
    config = load_config(repo_root)
    dirs = local_folder_dirs(repo_root)
    drift = False

    for entry in selected_folders(config, args.folder):
        try:
            remote_folder = client.folder(entry["uid"])
        except GrafanaError as e:
            if e.code == 404:
                print(f"folder-missing-remote: '{entry['title']}' ({entry['uid']})")
                drift = True
                continue
            raise
        if remote_folder["title"] != entry["title"]:
            print(f"folder-renamed: '{entry['title']}' -> '{remote_folder['title']}' ({entry['uid']})")
            drift = True

        dirpath = dirs.get(entry["uid"])
        local = local_dashboards(dirpath) if dirpath else {}
        remote_items = {i["uid"]: i for i in client.search_dashboards(entry["uid"])}

        for uid in sorted(set(local) | set(remote_items)):
            if uid not in remote_items:
                print(f"only-local: {local[uid].relative_to(repo_root)} ({uid})")
                drift = True
            elif uid not in local:
                print(f"only-remote: '{remote_items[uid].get('title')}' ({uid}) in '{entry['title']}'")
                drift = True
            else:
                with open(local[uid]) as f:
                    local_text = canon(json.load(f))
                remote = client.dashboard(uid)
                remote_text = canon(remote) if remote is not None else ""
                if local_text != remote_text:
                    print(f"changed: {local[uid].relative_to(repo_root)} ({uid})")
                    drift = True
                    if args.diff:
                        sys.stdout.writelines(
                            difflib.unified_diff(
                                local_text.splitlines(keepends=True),
                                remote_text.splitlines(keepends=True),
                                fromfile=f"repo/{local[uid].name}",
                                tofile=f"grafana/{uid}",
                            )
                        )

    if drift:
        return 1
    print("No drift — repo and Grafana are in sync")
    return 0


def validate_repo(repo_root: Path) -> list[str]:
    problems = []
    base = grafana_dir(repo_root)
    config = load_config(repo_root)
    tracked = {f["uid"]: f for f in config.get("folders", [])}

    seen_dash_uids: dict[str, Path] = {}
    seen_folder_uids: dict[str, Path] = {}
    dirs_by_uid = {}

    if base.exists():
        for meta_path in sorted(base.rglob(FOLDER_META)):
            rel = meta_path.relative_to(base)
            if len(rel.parts) > MAX_NESTING:
                problems.append(f"{meta_path}: nesting deeper than {MAX_NESTING} levels")
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except json.JSONDecodeError as e:
                problems.append(f"{meta_path}: invalid JSON: {e}")
                continue
            uid = meta.get("metadata", {}).get("uid")
            if not uid:
                problems.append(f"{meta_path}: missing metadata.uid")
                continue
            if uid in seen_folder_uids:
                problems.append(f"{meta_path}: duplicate folder uid '{uid}' (also {seen_folder_uids[uid]})")
            seen_folder_uids[uid] = meta_path
            dirs_by_uid[uid] = meta_path.parent

        for dirpath in sorted({p.parent for p in base.rglob("*.json")}):
            if not (dirpath / FOLDER_META).exists() and dirpath != base:
                problems.append(f"{dirpath}: missing {FOLDER_META}")
            titles: dict[str, Path] = {}
            for path in sorted(dirpath.glob("*.json")):
                if path.name == FOLDER_META:
                    continue
                try:
                    with open(path) as f:
                        dashboard = json.load(f)
                except json.JSONDecodeError as e:
                    problems.append(f"{path}: invalid JSON: {e}")
                    continue
                for field in ("uid", "title", "panels", "schemaVersion"):
                    if field not in dashboard:
                        problems.append(f"{path}: missing required field '{field}'")
                uid = dashboard.get("uid")
                title = dashboard.get("title")
                if uid:
                    if uid in seen_dash_uids:
                        problems.append(f"{path}: duplicate dashboard uid '{uid}' (also {seen_dash_uids[uid]})")
                    seen_dash_uids[uid] = path
                if title:
                    if title in titles:
                        problems.append(f"{path}: duplicate title '{title}' in folder (also {titles[title]})")
                    titles[title] = path
                    expected = f"{slugify(title)}.json"
                    if path.name != expected:
                        problems.append(f"{path}: filename should be '{expected}' (slug of title)")

    for uid, entry in tracked.items():
        if uid not in dirs_by_uid:
            problems.append(f"{CONFIG_NAME}: tracked folder '{entry['title']}' ({uid}) has no directory under {GRAFANA_DIRNAME}/")
    for uid, dirpath in dirs_by_uid.items():
        if uid not in tracked:
            problems.append(f"{dirpath}: folder uid '{uid}' not listed in {CONFIG_NAME}")

    return problems


def cmd_validate(client, repo_root: Path, args) -> int:
    problems = validate_repo(repo_root)
    for p in problems:
        print(f"ERROR: {p}")
    if problems:
        print(f"{len(problems)} problem(s) found")
        return 1
    print("Validation passed")
    return 0


# ---------------------------------------------------------------------------


NEEDS_GRAFANA = {"list-folders", "track", "pull", "push", "delete", "status"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--grafana-url", default=None, help="Grafana base URL (default: from grafana-sync.json)")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Repo root directory")
    parser.add_argument("--verify-tls", action="store_true", help="Verify TLS certificates (default: off)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-folders", help="List Grafana folders with dashboard counts")

    p = sub.add_parser("track", help="Track a folder and pull it")
    p.add_argument("folder_uid")

    p = sub.add_parser("pull", help="Mirror tracked folders from Grafana into the repo")
    p.add_argument("--folder", action="append", help="Limit to folder UID (repeatable)")
    p.add_argument("--keep-missing", action="store_true", help="Keep local files for dashboards gone from Grafana")

    p = sub.add_parser("push", help="Push repo dashboards to Grafana")
    p.add_argument("--folder", action="append", help="Limit to folder UID (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="Report without writing to Grafana")
    p.add_argument("--prune", action="store_true", help="Delete remote dashboards absent from the repo")

    p = sub.add_parser("delete", help="Delete a dashboard from Grafana by UID")
    p.add_argument("--uid", required=True)

    p = sub.add_parser("status", help="Report drift between repo and Grafana")
    p.add_argument("--folder", action="append", help="Limit to folder UID (repeatable)")
    p.add_argument("--diff", action="store_true", help="Print unified diffs for changed dashboards")

    sub.add_parser("validate", help="Offline validation of repo contents")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    client = None
    if args.command in NEEDS_GRAFANA:
        config = load_config(args.repo_root)
        url = args.grafana_url or config.get("grafana_url")
        if not url:
            logger.error("No Grafana URL: set grafana_url in grafana-sync.json or pass --grafana-url")
            sys.exit(2)
        api_key = os.environ.get("GRAFANA_API_KEY")
        if not api_key:
            logger.error("GRAFANA_API_KEY is not set")
            sys.exit(2)
        client = Client(url, api_key, verify_tls=args.verify_tls)

    handlers = {
        "list-folders": cmd_list_folders,
        "track": cmd_track,
        "pull": cmd_pull,
        "push": cmd_push,
        "delete": cmd_delete,
        "status": cmd_status,
        "validate": cmd_validate,
    }
    try:
        sys.exit(handlers[args.command](client, args.repo_root, args))
    except GrafanaError as e:
        logger.error(str(e))
        sys.exit(2)


if __name__ == "__main__":
    main()
