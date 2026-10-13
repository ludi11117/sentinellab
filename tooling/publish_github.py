"""Explicitly invoked release helper. Uses existing OS Git credentials without logging them."""
import argparse
import os
import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main(owner, name):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise RuntimeError("commit and review the local project before publishing")
    result = subprocess.run(["git", "credential-manager", "get"], input="protocol=https\nhost=github.com\n\n",
                            capture_output=True, text=True, env=env, cwd=ROOT)
    credential = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    token = credential.get("password")
    if result.returncode or not token:
        raise RuntimeError("no existing noninteractive GitHub credential")
    with httpx.Client(base_url="https://api.github.com", headers={"Authorization": "Bearer " + token,
                       "Accept": "application/vnd.github+json", "User-Agent": "SentinelLab-publisher"}, timeout=30) as api:
        account = api.get("/user")
        account.raise_for_status()
        if account.json()["login"] != owner:
            raise RuntimeError("Git credential account differs from the authorized GitHub account")
        response = api.get(f"/repos/{owner}/{name}")
        if response.status_code == 404:
            response = api.post("/user/repos", json={"name": name, "private": True, "auto_init": False,
                                "description": "Agent delivery verification: fenced workers, recoverable evaluation, project isolation and evidence-backed release gates"})
            response.raise_for_status()
        else:
            response.raise_for_status()
            # Never overwrite an already populated repository.
            refs = api.get(f"/repos/{owner}/{name}/git/refs/heads")
            if refs.status_code == 200 and refs.json():
                raise RuntimeError("repository already contains branches; select an empty destination")
        repo = response.json()
        if repo["owner"]["login"] != owner:
            raise RuntimeError("repository owner mismatch")
        url = repo["clone_url"]
    remotes = subprocess.check_output(["git", "remote"], cwd=ROOT, text=True).splitlines()
    if "origin" in remotes:
        existing = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=ROOT, text=True).strip()
        if existing != url:
            raise RuntimeError("origin points at another repository")
    else:
        subprocess.run(["git", "remote", "add", "origin", url], cwd=ROOT, check=True)
    # Git's credential-manager supplies the token; it is not put in the URL or argv.
    subprocess.run(["git", "-c", "credential.helper=manager", "push", "-u", "origin", "main"], cwd=ROOT, env=env, check=True)
    print(repo["html_url"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("owner")
    parser.add_argument("name")
    args = parser.parse_args()
    main(args.owner, args.name)
