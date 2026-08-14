"""Write a generated JSON feed only when its content has actually changed.

Every feed the site reads carries a `generated` timestamp, which means a plain
write produces a different file on every run even when nothing about the data
moved. The feeds are committed whenever they change -- by a daily GitHub Actions
workflow when this was written, by hand on BIOMIX since 2026-08-13 -- so that
alone was enough to put a commit in the history every day of the year: three
files, three insertions, three deletions, all of them the timestamp line.

`write_feed` compares the new payload against the file already on disk with the
timestamp removed from both. If nothing else differs it leaves the file exactly
as it is, timestamp and all, and reports that it did nothing. The timestamp then
means what a reader would assume it means: when the contents last CHANGED, not
when the job last ran. When the job last ran is in the Actions log, which is a
better place for it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def write_feed(path: Path, payload: Dict[str, Any], timestamp_key: str = "generated",
               indent: int = 2, ensure_ascii: bool = False, newline: str = "") -> bool:
    """Write `payload` to `path` unless only its timestamp would change.

    Returns True if the file was written, False if it was left alone.
    """
    text = json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii) + newline

    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # An unreadable file is not evidence of anything; overwrite it.
            existing = None
        if isinstance(existing, dict):
            before = {k: v for k, v in existing.items() if k != timestamp_key}
            after = {k: v for k, v in payload.items() if k != timestamp_key}
            if before == after:
                return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True
