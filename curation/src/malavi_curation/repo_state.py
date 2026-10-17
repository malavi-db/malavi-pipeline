"""Where the review state lives, and whether this checkout may write it.

Since 2026-10-16 the review ledger, the id map, the applied-verdicts record and every
screen's outputs are committed to the private repository (``intake.state_in_repo`` in
config/project.yml), so that the intake cycle can run on GitHub Actions as well as on
BIOMIX. Two checkouts then hold the same state, and the rule that keeps them from
disagreeing is: whoever pulled last, writes.

The intake cycle enforces that rule itself (it pulls first and refuses on a checkout it
cannot fast-forward). The programs it runs did not, so a person running one of them by
hand on a stale checkout -- ``fetch_verdicts.py`` after Actions had already applied the
same responses -- would see every response as new, apply it a second time and announce
it (review of 2026-10-16, finding 17: the one repeat of the duplicate-notice incident that
passes every other guard). :func:`require_fresh` is what each of those programs calls
before writing: with the state in the repository, it fetches and refuses to go on when
the checkout is behind its remote.

Two ways to skip it, both deliberate: the cycle sets ``MALAVI_IN_CYCLE=1`` for the
programs it runs (it has already pulled), and a person may pass ``--standalone`` to a
program when the remote is unreachable and they accept the risk.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .config import load_config, repo_root

# The settings, from config/project.yml (``intake:``), each overridable by an environment
# variable so a CI job can turn them on without editing tracked configuration that a
# hand run on BIOMIX also reads.
STATE_SETTINGS = {
    # Commit the review state to the repository at the end of a live run, and refuse to
    # start a live run on a repository that is behind its remote or has uncommitted state.
    "state_in_repo": ("MALAVI_STATE_IN_REPO", False),
    # After committing, push. A hand run on BIOMIX pushes when the maintainer does; the
    # scheduled job must push or its state is lost with the runner.
    "push_state": ("MALAVI_PUSH_STATE", False),
}

# Set by the intake cycle for the programs it runs: the cycle pulled already.
IN_CYCLE_ENV = "MALAVI_IN_CYCLE"

# A date (YYYY-MM-DD) until which the COUNT refusals of the three mail-sending programs
# are lifted, for the unattended cycle where nobody can type --allow-bulk. On GitHub it
# is a repository variable the workflow passes through. It never lifts the age rule in
# fetch_verdicts.py (code review 2026-10-16, finding 6): that one takes --allow-old by hand.
ALLOW_BULK_UNTIL_ENV = "MALAVI_ALLOW_BULK_UNTIL"


def bulk_allowed_by_environment(today: Optional[date] = None) -> bool:
    """Whether MALAVI_ALLOW_BULK_UNTIL names today or a later date. A value that is not
    a date authorizes nothing (one parser for all three programs; finding 7)."""
    raw = os.environ.get(ALLOW_BULK_UNTIL_ENV, "").strip()
    if not raw:
        return False
    try:
        until = date.fromisoformat(raw)
    except ValueError:
        return False
    return (today or date.today()) <= until

Runner = Callable[[Sequence[str]], Tuple[int, str]]


class StaleCheckout(RuntimeError):
    """This checkout is behind its remote (or cannot prove it is not), so writing the
    review state here could re-apply what another machine already applied."""


def state_settings(configuration: Optional[Dict[str, object]] = None) -> Dict[str, bool]:
    """The two state settings, environment first, then config, then the default."""
    section = ((configuration if configuration is not None else load_config())
               .get("intake") or {})
    out: Dict[str, bool] = {}
    for name, (variable, default) in STATE_SETTINGS.items():
        raw = os.environ.get(variable, "").strip().lower()
        if raw:
            out[name] = raw in ("1", "true", "yes", "on")
        else:
            out[name] = bool(section.get(name, default))
    return out


def _git(command: Sequence[str]) -> Tuple[int, str]:
    """Run one git command in the repository; (exit code, combined output)."""
    # GIT_TERMINAL_PROMPT=0: a fetch that would ask for a credential fails instead of
    # hanging an unattended run (finding 18).
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(["git", *command], cwd=str(repo_root()), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
                              env=environment)
    except OSError as exc:
        return 127, f"git could not be started: {exc}"
    return done.returncode, (done.stdout or "").strip()


def behind_remote(runner: Runner = _git) -> Optional[str]:
    """A sentence saying why this checkout may not write, or None when it is current.

    "Current" means origin's copy of the current branch is an ancestor of HEAD after a
    fetch. A fetch that fails counts as not current: freshness cannot be proven, and the
    cost of refusing is a retry, while the cost of guessing is a duplicate email.
    """
    code, output = runner(["fetch", "--quiet", "origin"])
    if code != 0:
        return f"could not fetch origin ({output or 'no output'}), so this checkout cannot " \
               f"be shown to be current"
    code, branch = runner(["rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0 or not branch or branch == "HEAD":
        return "not on a branch, so there is no remote branch to compare with"
    code, output = runner(["rev-parse", "--verify", "--quiet", f"origin/{branch}"])
    if code != 0:
        return f"origin has no branch {branch}, so there is nothing to compare with"
    code, output = runner(["merge-base", "--is-ancestor", f"origin/{branch}", "HEAD"])
    if code == 0:
        return None
    if code == 1:
        return (f"this checkout is behind origin/{branch}. Run `git pull --ff-only` (or "
                f"the intake cycle, which pulls first), then try again")
    return f"git merge-base failed ({output or 'no output'})"


def require_fresh(program: str, *, standalone: bool = False,
                  configuration: Optional[Dict[str, object]] = None,
                  runner: Runner = _git) -> None:
    """Refuse to let ``program`` write the review state from a stale checkout.

    Does nothing when the state is not kept in the repository, when the intake cycle is
    the caller (``MALAVI_IN_CYCLE`` set: it pulled already), or when the person passed
    ``--standalone``. Raises :class:`StaleCheckout` otherwise if the checkout is behind.
    """
    if standalone or os.environ.get(IN_CYCLE_ENV, "").strip():
        return
    if not state_settings(configuration)["state_in_repo"]:
        return
    problem = behind_remote(runner)
    if problem:
        raise StaleCheckout(
            f"{program}: {problem}. Nothing was written. With the review state in the "
            f"repository, a program run on a checkout that is behind would re-apply what "
            f"another machine already applied and announce it again. Pass --standalone "
            f"only if the remote is unreachable and you accept that.")
