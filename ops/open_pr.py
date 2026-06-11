#!/usr/bin/env python3
"""Open a GitHub PR via the REST API. Stdlib fallback for when gh is absent.

Requires GITHUB_TOKEN in the environment (fine-grained PAT, Pull requests:
read/write on this repo). Treats an already-open PR for the branch as success.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def repo_slug() -> str:
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    if not m:
        sys.exit(f"Cannot parse GitHub repo from remote: {url}")
    return m.group(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set")

    req = Request(
        f"https://api.github.com/repos/{repo_slug()}/pulls",
        data=json.dumps(
            {"title": args.title, "body": args.body, "head": args.head, "base": args.base}
        ).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")

    try:
        with urlopen(req) as resp:
            pr = json.loads(resp.read())
            print(f"Opened PR #{pr['number']}: {pr['html_url']}")
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 422 and "already exists" in body:
            print(f"PR for {args.head} already open")
            return
        sys.exit(f"GitHub API {e.code}: {body}")


if __name__ == "__main__":
    main()
