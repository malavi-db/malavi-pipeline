#!/usr/bin/env python3
# @title Audit what is actually public: fetch the live site, feeds and public repositories and scan them
# @purpose Download what the world can see (the site's pages and feeds over HTTPS, the two
#   public repositories with their whole history) and run curation/privacy_scan.py over
#   it; also ask GitHub whether the private repository is still private.
# @why The publish scripts scan before they push, but a guard can be bypassed (a hand
#   push, an old commit, a file nobody knew was served). This is the detection for that
#   day (results/GUARDRAILS_PLAN_2026-10-16.md, section 2.2): run weekly from GitHub
#   Actions and on demand.
# @input config/project.yml (links.site; the forbidden list via privacy_scan)
# @input https://malavi-db.github.io/ and the two public repositories
# @output the exit code: 0 clean, 1 something private is public, 2 could not audit
# @program python3
# @program git
# @critical-var PUBLIC_REPOSITORIES
# @critical-var PRIVATE_REPOSITORY
# @critical-var SITE_PATHS
"""Audit what is public.

    .venv/bin/python ops/public_audit.py            # fetch, clone, scan, check visibility
    .venv/bin/python ops/public_audit.py --no-history   # HEAD of each repository only

The site is fetched page by page (SITE_PATHS) plus the four feeds; each public repository
is cloned shallowly for its working tree and, unless --no-history, fully for a dump of
``git log -p --all``, which is scanned as one file under the ``history`` destination (the
2026-08-10 lesson: a value removed from HEAD is still in the history). The private
repository's visibility is read from the GitHub API with the token in MALAVI_AUDIT_TOKEN
or GITHUB_TOKEN; without one, that check is skipped and says so.

Nothing here prints a private value: privacy_scan prints classes, files and hashes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "curation" / "src"))

from malavi_curation.config import load_config  # noqa: E402

PUBLIC_REPOSITORIES = (
    "https://github.com/malavi-db/malavi-db.github.io.git",
    "https://github.com/malavi-db/malavi-pipeline.git",
    "https://github.com/vincenzoaellis/malaviR.git",
)
PRIVATE_REPOSITORY = "vincenzoaellis/malavi_rebuild"
SITE_PATHS = ("index.html", "curating.html", "how-it-works.html", "sequence-match.html",
              "board.html", "assets/data/queue.json", "assets/data/contributors.json",
              "assets/data/reserved_names.json", "assets/data/board.json",
              "assets/data/site_stats.json")
TIMEOUT = 60


def _scanner():
    path = ROOT / "curation" / "privacy_scan.py"
    spec = importlib.util.spec_from_file_location("_privacy_scan", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_privacy_scan"] = module
    spec.loader.exec_module(module)
    return module


def fetch_site(base_url: str, into: Path) -> List[str]:
    """Download each SITE_PATH under base_url into ``into``; return what was missed."""
    missed = []
    for relative in SITE_PATHS:
        url = base_url.rstrip("/") + "/" + relative
        target = into / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
                target.write_bytes(response.read())
        except (urllib.error.URLError, OSError) as exc:
            missed.append(f"{relative}: {exc}")
    return missed


def _git(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *command], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)


def clone(url: str, into: Path, history: bool) -> Optional[str]:
    """Clone ``url`` into ``into`` (full when history is wanted); None, or why not."""
    command = ["clone", "--quiet"] + ([] if history else ["--depth", "1"]) + [url, str(into)]
    done = _git(*command)
    if done.returncode != 0:
        return done.stdout.strip() or f"git clone exited {done.returncode}"
    return None


def history_dump(repository: Path, into: Path) -> Optional[str]:
    # Bytes, decoded tolerantly: a history holds every encoding anyone ever committed.
    done = subprocess.run(["git", "-C", str(repository), "log", "-p", "--all", "--no-color"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    text = done.stdout.decode("utf-8", errors="replace")
    if done.returncode != 0:
        return text.strip()[:200] or "git log failed"
    into.write_text(text, encoding="utf-8")
    return None


def private_repository_visibility(token: str) -> Optional[str]:
    """'private' or 'public' from the GitHub API, or None when it cannot be asked."""
    if not token:
        return None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{PRIVATE_REPOSITORY}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "malavi-public-audit"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if "private" not in data:
        return None
    return "private" if data["private"] else "public"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-history", action="store_true",
                        help="scan each repository's HEAD only, not its history")
    parser.add_argument("--reveal", action="store_true",
                        help="print matched values (local use only)")
    arguments = parser.parse_args(argv)

    configuration = load_config()
    site_url = str((configuration.get("links") or {}).get("site") or "https://malavi-db.github.io/")
    scanner = _scanner()
    forbidden = scanner.build_forbidden(ROOT, configuration)
    public_addresses = [str(a) for a in
                        ((configuration.get("privacy") or {}).get("public_addresses") or [])]
    rules = scanner.load_allow_rules(ROOT / "publish" / "privacy_allow.txt")
    print(f"== malavi_rebuild :: public audit ==\n{len(forbidden)} forbidden values and "
          f"patterns; {len(rules)} allow rule(s)\n")

    problems = 0          # in what is served or at HEAD: act today
    history_problems = 0  # only in a repository's history: decide (rewrite, or accept)
    could_not = 0
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)

        # 1. The live site.
        site_dir = work / "site"
        missed = fetch_site(site_url, site_dir)
        for line in missed:
            print(f"  could not fetch {line}")
        if missed and len(missed) == len(SITE_PATHS):
            could_not += 1
        hits = scanner.scan([site_dir], "site", forbidden, public_addresses, rules, base=site_dir)
        feeds = [site_dir / "assets" / "data" / n for n in scanner.FEED_FILES
                 if (site_dir / "assets" / "data" / n).is_file()]
        hits += scanner.scan(feeds, "feeds", forbidden, public_addresses, rules, base=site_dir)
        print(f"site ({site_url}): {len(hits)} hit(s)")
        for hit in hits:
            print(hit.describe(arguments.reveal))
        problems += len(hits)

        # 2. The public repositories, HEAD and history.
        for url in PUBLIC_REPOSITORIES:
            name = url.rstrip("/").rsplit("/", 1)[-1].replace(".git", "")
            target = work / name
            failure = clone(url, target, history=not arguments.no_history)
            if failure:
                print(f"{name}: could not clone ({failure})")
                could_not += 1
                continue
            destination = "pipeline" if "pipeline" in name else (
                "malaviR" if "malaviR" in name else "site")
            hits = scanner.scan([target], destination, forbidden, public_addresses, rules,
                                base=target)
            print(f"{name} (HEAD, as {destination}): {len(hits)} hit(s)")
            for hit in hits:
                print(hit.describe(arguments.reveal))
            problems += len(hits)
            if not arguments.no_history:
                dump = work / f"{name}.history.txt"
                failure = history_dump(target, dump)
                if failure:
                    print(f"{name}: could not dump the history ({failure})")
                    could_not += 1
                    continue
                hits = scanner.scan([dump], "history", forbidden, public_addresses, rules,
                                    base=work)
                counts = {}
                for hit in hits:
                    counts[hit.cls] = counts.get(hit.cls, 0) + 1
                print(f"{name} (history): {len(hits)} hit(s)"
                      + (" (" + ", ".join(f"{c} {k}" for k, c in sorted(counts.items())) + ")"
                         if counts else ""))
                if arguments.reveal:
                    for hit in hits:
                        print(hit.describe(True))
                history_problems += len(hits)

        # 3. Is the private repository still private?
        token = os.environ.get("MALAVI_AUDIT_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        visibility = private_repository_visibility(token)
        if visibility is None:
            print(f"{PRIVATE_REPOSITORY}: visibility not checked "
                  f"({'no token' if not token else 'the API could not be read'})")
        else:
            print(f"{PRIVATE_REPOSITORY}: {visibility}")
            if visibility != "private":
                problems += 1

    print("")
    if problems:
        print(f"PUBLIC AUDIT: {problems} problem(s) in what is served or at HEAD. Something "
              f"private is public; act today.")
        if history_problems:
            print(f"Plus {history_problems} in the repositories' histories (see above).")
        return 1
    if history_problems:
        print(f"PUBLIC AUDIT: nothing private at HEAD or on the site, but {history_problems} "
              f"hit(s) remain in the public repositories' HISTORIES. Decide once: rewrite "
              f"that history, or accept it with a reasoned line in "
              f"publish/privacy_allow.txt (destination history, path <repo>.history.txt). "
              f"Run with --reveal on this machine to see the values.")
        return 1
    if could_not:
        print(f"PUBLIC AUDIT: nothing private found, but {could_not} source(s) could not be "
              f"audited.")
        return 2
    print("PUBLIC AUDIT: clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
