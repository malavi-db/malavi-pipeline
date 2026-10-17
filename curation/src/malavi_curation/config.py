"""Load the central project configuration (config/project.yml).

Single source of truth shared with the R side and the watcher. Walks up from this
file to the repo root so it works regardless of the current working directory.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml


def repo_root() -> Path:
    """Return the repository root (the dir that contains config/project.yml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "project.yml").is_file():
            return parent
    raise FileNotFoundError("Could not locate config/project.yml above " + str(here))


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    """Parse and cache config/project.yml as a dict."""
    with open(repo_root() / "config" / "project.yml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
