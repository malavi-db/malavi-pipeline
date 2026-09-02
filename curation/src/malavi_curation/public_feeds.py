"""Rebuild the site's data feeds and publish them, after something changed a state.

Why this exists
---------------
The public queue is generated from the review ledger, so it is only ever as current as
the last time somebody remembered to run ``build_site_feeds.py`` and then
``publish/push_site.sh``. In practice that meant the queue lagged real decisions: the
first real submission was approved at 05:05Z on 2026-08-20 and the published queue went
on saying "Under review" because the feed had been built two minutes earlier.

So every program that changes a submission's state calls :func:`refresh` when it is done.

Three rules that make an automatic publish safe
-----------------------------------------------
**1. It publishes data, never pages.** ``publish/push_feeds.sh`` can only move three
generated JSON files. The full publisher syncs all of ``docs/``, and running *that*
automatically would put whatever else was in the working tree onto the public web the
moment a curator recorded a verdict -- a half-edited page, or a ``curating.html`` left
stale by an aborted build.

**2. It never fails the thing that called it.** The ledger write has already happened and
is the record; a publish is a downstream convenience. A dead network must leave the
verdict recorded and print a warning, not raise into a caller that has no way to undo the
write it just made. Every failure here is reported and swallowed.

**3. It does nothing on a dry run.** A caller previewing a change publishes nothing,
which is what ``--dry-run`` means everywhere else in this project.

What a curator will actually see
--------------------------------
Not necessarily what just happened. The queue applies its own rule -- an approval is not
public until the publish hold has elapsed -- so calling this immediately after an approval
correctly publishes *no change at all*. It flips a day later, on whichever run happens
next. That is the design, not a lag: see ``build_site_feeds.public_review_state``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .config import repo_root

# The two generators, in the order they are run. Both read the ledger and write into
# docs/assets/data/; both write by default and take --dry-run to preview.
BUILDERS = ("build_site_feeds.py", "build_name_reservations.py")

# The narrow publisher. Deliberately NOT push_site.sh -- see rule 1 above.
PUBLISHER = Path("publish") / "push_feeds.sh"

# The exit code a builder uses to say "the feed IS written, and a person has to act".
# build_name_reservations.py exits with it when two submissions claim one name: the
# earliest claim is published, which is the right feed, and the other submitter has to
# be offered another name, which no program can do. Until 2026-09-02 refresh() read any
# non-zero exit as a failed rebuild and withheld every feed, queue.json included -- so one
# stale collision silently froze the public queue. build_site_feeds.py does not use this
# code today; if it ever does, it has to mean the same thing there.
NEEDS_A_PERSON = 2


def _run(command: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run one step, capturing everything. Never raises on a non-zero exit."""
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                          check=False)


def refresh(dry_run: bool = False, publish: bool = True,
            python: Optional[str] = None) -> bool:
    """Rebuild the public feeds and, unless asked not to, publish them.

    Returns True if everything it attempted succeeded. Prints a one-line note for each
    step that did not, and never raises: see rule 2 in the module docstring.

    A builder exiting :data:`NEEDS_A_PERSON` has succeeded as far as publishing goes: its
    feed is written and is published like any other. What it printed is repeated here in
    full, because it is a message for a curator and this may be the only place it shows.

    ``publish=False`` rebuilds the feeds locally and stops, for a caller that wants the
    files current without an outward-facing action.
    """
    if dry_run:
        print("  [dry-run] the public feeds were not rebuilt or published.")
        return True

    root = repo_root()
    interpreter = python or sys.executable
    ok = True

    for builder in BUILDERS:
        script = root / "curation" / builder
        if not script.is_file():
            print(f"  NOTE: {builder} is missing; the public feeds were not rebuilt.")
            ok = False
            continue
        result = _run([interpreter, str(script)], root)
        if result.returncode == NEEDS_A_PERSON:
            # Written, publishable, and somebody has to be told. The builder's stderr is
            # the message; it is repeated whole rather than summarized to one line.
            print(f"  NOTE: {builder} wrote its feed but needs a person:")
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            for line in detail or [f"exit {result.returncode}, no message"]:
                print(f"        {line}")
            continue
        if result.returncode != 0:
            # The generator's own diagnostics are worth more than a summary of them.
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else f"exit {result.returncode}"
            print(f"  NOTE: {builder} failed ({tail}); the public feeds may be stale.")
            ok = False

    if not publish:
        return ok

    if not ok:
        # Publishing feeds a builder just failed to write would push a half-updated set.
        print("  NOTE: not publishing, because a feed did not rebuild cleanly.")
        return False

    publisher = root / PUBLISHER
    if not publisher.is_file():
        print(f"  NOTE: {PUBLISHER} is missing; the feeds were rebuilt but not published.")
        return False

    result = _run(["bash", str(publisher)], root)
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {result.returncode}"
        print(f"  NOTE: publishing the feeds failed ({tail}).")
        print(f"        The change IS recorded. Publish it with: {PUBLISHER}")
        return False

    # Say which of the two outcomes happened, because "nothing to push" is the normal
    # result right after an approval and should not read as a failure.
    if "nothing to push" in result.stdout:
        print("  public feeds rebuilt; the published queue was already current.")
    else:
        print("  public feeds rebuilt and published.")
    return True
