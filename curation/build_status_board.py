#!/usr/bin/env python3
# @title Build the curators' board: where every submission stands, encrypted for the site
# @purpose Read the review ledger and write docs/assets/data/board.json, an encrypted file
#   the site's board page decrypts in a curator's browser with a shared password.
# @why A curator could not see whether a verdict counted, what the others had recorded, or
#   what a submission was waiting on; only the ledger knew, on a machine no curator can
#   reach (results/PROTOCOL_REVIEW_2026-10-02.md, sections 1 and 7).
# @input curation/intake/submissions/review_ledger.json (via malavi_curation.ledger)
# @input config/curators.yml (curator ids to names)
# @input config/project.yml (review.publish_hold_hours, review.verdict_form_url,
#   submissions.exclude, review.board_password_file)
# @input ~/.config/malavi/board_password.txt (the shared password; never in the repository)
# @output docs/assets/data/board.json (AES-256-GCM, key from PBKDF2-HMAC-SHA256)
# @program python3
# @program cryptography
# @critical-var KDF_ITERATIONS
# @critical-var BOARD_FILE
# @critical-var DEFAULT_PASSWORD_FILE
# @critical-flag build_status_board.py "" --dry-run
# @critical-flag build_status_board.py "" --init-password
"""Build the curators' board.

    .venv/bin/python curation/build_status_board.py --init-password   # once
    .venv/bin/python curation/build_status_board.py --dry-run         # what it would say
    .venv/bin/python curation/build_status_board.py                   # write the file

Writes by default and takes ``--dry-run`` to preview, like the other two feed builders
(``build_site_feeds.py``, ``build_name_reservations.py``); it is run with them by
``malavi_curation.public_feeds.refresh`` and by ``intake_cycle.py``.

**No password, no board, and no error.** Until ``--init-password`` has been run there is
nothing to encrypt with, so this prints a note and exits 0 without writing. That is
deliberate: this program runs inside the automatic feed refresh, and an optional feature
that is not set up must not stop the public queue from being published.

**The file is rewritten only when the board changed.** Encryption is randomized, so two
builds of the same board differ byte for byte; the builder decrypts what is already there
and leaves it alone when it says the same thing. Otherwise every run would make a new
commit in the public site repository.

What may appear on the board is decided in ``malavi_curation.status_board`` and explained
at the top of that file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import curators, ledger, status_board           # noqa: E402
from malavi_curation.config import load_config, repo_root            # noqa: E402
from malavi_curation.status_board import (                           # noqa: E402,F401
    BOARD_FILE, DEFAULT_PASSWORD_FILE, KDF_ITERATIONS, BoardError,
)


def _describe(content: dict) -> List[str]:
    """The board as lines for a terminal: what a curator will see, without the links."""
    lines = []
    for row in content["submissions"]:
        waiting = row["waiting"]
        verdicts = ", ".join(
            f"{v['id']} {v['verdict']} by {v['curator']}"
            + (f" ({v['ended']})" if v["ended"] else "") for v in row["verdicts"]) or "none"
        lines.append(f"  {row['id']}  {row['state']:<18} waiting on: {waiting['code']}"
                     f" since {str(waiting.get('since'))[:10]}; verdicts: {verdicts}")
    for row in content["finished"]:
        lines.append(f"  {row['id']}  {row['state']:<18} finished {row['ended'][:10]}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print what the board would say; write nothing")
    parser.add_argument("--init-password", action="store_true",
                        help="generate the shared password file (once) and stop. Refuses "
                             "to replace an existing one")
    arguments = parser.parse_args(argv)

    print("== malavi_rebuild :: build_status_board ==")

    if arguments.init_password:
        try:
            path = status_board.create_password_file()
        except BoardError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        # The path, never the password: this output may be copied into a log.
        print(f"A password was generated and stored at {path} (mode 600).\n"
              f"Read it there and give it to each curator yourself; it is not printed "
              f"here. Then run this program again to write the board.")
        return 0

    configuration = load_config()
    inbox = repo_root() / (configuration.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")

    entries = ledger.load(inbox)
    hidden = status_board.excluded_submission_ids(inbox, list(entries), configuration)
    content = status_board.board_content(entries, curators.load_registry(),
                                         configuration, excluded_ids=hidden)
    print(f"{len(content['submissions'])} submission(s) in review, "
          f"{len(content['finished'])} finished"
          + (f", {len(hidden)} hidden by submissions.exclude" if hidden else ""))
    for line in _describe(content):
        print(line)

    try:
        password = status_board.load_password()
    except BoardError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if password is None:
        print(f"\nNo board password at {status_board.password_path()}, so the board was "
              f"not written. Run with --init-password to set it up. (Not an error: the "
              f"board is optional and the public feeds do not depend on it.)")
        return 0

    target = repo_root() / BOARD_FILE
    if arguments.dry_run:
        print(f"\n[dry-run] nothing written. Would write {BOARD_FILE}.")
        return 0

    written = status_board.write_board(target, content, password)
    print(f"\nwrote {BOARD_FILE}" if written
          else f"\n{BOARD_FILE} already says this; left as it is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
