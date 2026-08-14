"""The `all` extra must stay the union of every other extra."""
import tomllib
from pathlib import Path

from malavi_curation.config import repo_root


def test_all_extra_covers_every_other_extra():
    """A new extra that `all` forgets is the bug `dev` already had once.

    `dev` was missing `tables` until 2026-08-08, and CI tested a different package than
    developers ran. `all` is what RUNBOOK §0 tells a person to install, so an extra missing
    from it is an environment nobody can rebuild from the instructions.
    """
    with open(repo_root() / "curation" / "pyproject.toml", "rb") as handle:
        extras = tomllib.load(handle)["project"]["optional-dependencies"]

    referenced = {name.split("[", 1)[1].rstrip("]")
                  for name in extras["all"] if "[" in name}
    others = set(extras) - {"all"}

    assert others - referenced == set(), \
        f"`all` does not reference: {sorted(others - referenced)}"
