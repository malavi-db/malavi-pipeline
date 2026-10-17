#!/usr/bin/env python3
"""Turn a publication batch into a notification: a GitHub issue or an email.

Reads the batch written by scan_publications.py and, per config `watcher.delivery`,
either opens a GitHub issue (default; uses the Actions-provided token) or sends an
email via SMTP (creds from environment/secrets — never committed).

STUB (Phase 1): formatting the batch into Markdown is implemented; the actual
issue-open / SMTP-send calls are stubbed pending secrets wiring.

Usage:
    python watcher/notify.py --batch batch.json [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def format_batch(batch: List[Dict[str, Any]]) -> str:
    """Render the batch as a Markdown digest (issue body / email body)."""
    if not batch:
        return "No new haemosporidian publications in this window."
    lines = ["New publications for curation review:", ""]
    for h in batch:
        title = h.get("title", "(untitled)")
        journal = h.get("journal", "")
        year = h.get("year", "")
        url = h.get("url") or (("https://doi.org/" + h["doi"]) if h.get("doi") else "")
        lines.append(f"- **{title}** — {journal} {year}. {url}".rstrip())
    return "\n".join(lines)


def deliver(batch: List[Dict[str, Any]], delivery: str, dry_run: bool = False) -> None:
    body = format_batch(batch)
    if dry_run:
        print("[--dry-run] delivery =", delivery)
        print(body)
        return
    if delivery == "issue":
        raise NotImplementedError(
            "GitHub issue delivery is a Phase 1 stub. Use the gh CLI / REST API with "
            "the Actions GITHUB_TOKEN in notify.py."
        )
    elif delivery == "email":
        raise NotImplementedError(
            "Email delivery is a Phase 1 stub. Wire SMTP creds from secrets."
        )
    else:
        raise ValueError(f"Unknown delivery method: {delivery!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", default="batch.json")
    ap.add_argument("--delivery", default="issue", choices=["issue", "email"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    batch = json.loads(Path(args.batch).read_text()) if Path(args.batch).is_file() else []
    deliver(batch, args.delivery, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
