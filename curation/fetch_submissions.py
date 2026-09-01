#!/usr/bin/env python3
# @title Fetch community submissions from the public Google Form
# @purpose Read the form's responses sheet, download every file each submission
#   uploaded to Drive, and lay each one out as a self-contained folder the
#   curation intake can process.
# @why Submissions arrive as a spreadsheet row plus files in Drive. Curating them
#   by hand means opening the sheet, clicking through to Drive, downloading each
#   file and renaming it -- slow, and easy to mis-associate a file with the wrong
#   submission. This turns that into one deterministic command.
# @input config/project.yml (submissions.responses_sheet)
# @output curation/intake/submissions/<timestamp>_<slug>/ (files + metadata.json)
# @output curation/intake/submissions/fetched.json (ledger of downloaded file ids)
# @program python3
# @critical-var RESPONSES_SHEET
# @critical-var INBOX_DIR
# @critical-flag fetch_submissions.py "" --dry-run
"""Pull community submissions from the public Google Form into the intake.

No credentials required. The responses sheet and the upload folders are shared
read-only with "anyone with the link", so everything here is a plain HTTPS GET.

Determinism and idempotence
---------------------------
Every downloaded file id is recorded in a ledger. Re-running only fetches what is
new, so the command is safe to run on a schedule or by hand at any time, and a
re-run never duplicates or rewrites an existing submission folder.

The responses sheet is the authority for which files belong to which submission:
the file ids come from the response row itself, not from listing a Drive folder.
Listing a folder would lose that association and would also depend on scraping
Drive's HTML, which is not a stable interface.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import load_config, repo_root  # noqa: E402
from malavi_curation import google_auth  # noqa: E402

# A Drive file id as it appears in the sheet, e.g.
# "https://drive.google.com/open?id=1UuTIxYku5cn-4Orsv1AOnmQxCs2tHx_8".
DRIVE_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})")

# Columns whose cells hold Drive links. Matched case-insensitively on a
# substring so that light rewording of a question does not silently stop the
# fetcher from finding the uploads.
FILE_COLUMN_HINTS = ("template file", "supplementary", "pdf")

TIMEOUT = 60


def _get(url: str, token: Optional[str] = None) -> tuple[bytes, Dict[str, str]]:
    headers = {"User-Agent": "malavi_rebuild/fetch_submissions"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(), {k.lower(): v for k, v in resp.headers.items()}


def fetch_responses(sheet_id: str, token: Optional[str] = None) -> List[Dict[str, str]]:
    """Return the responses sheet as a list of {question: answer} dicts.

    With a token this goes through the Drive API's export endpoint, which is the documented
    way to read a private Sheet as CSV. Without one it falls back to the old
    docs.google.com export URL, which only works while the sheet is shared with "anyone
    with the link" -- an arrangement MalAvi has moved away from, and which is kept here
    only so an older link-shared sheet can still be read.
    """
    if token:
        url = (f"https://www.googleapis.com/drive/v3/files/{sheet_id}/export"
               f"?mimeType=text%2Fcsv")
    else:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    raw, _ = _get(url, token)
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def drive_ids(cell: str) -> List[str]:
    """Every Drive file id in one spreadsheet cell (a cell may list several)."""
    return DRIVE_ID_RE.findall(cell or "")


def download_drive_file(file_id: str, dest_dir: Path,
                        token: Optional[str] = None) -> Optional[Path]:
    """Download one Drive file; returns the path written, or None.

    The authenticated path is both required now and simpler: the Drive API returns the
    bytes directly, with none of the virus-scan interstitial handling below, which exists
    only because the unauthenticated download endpoint serves an HTML page instead of the
    file once a file is large enough.

    The original filename matters: a curator reads these directories by eye, and
    ``check_template.py`` finds the workbook by looking for an ``.xlsx``. The two download
    paths report it differently, and getting that wrong costs the extension, not just the
    name:

    * unauthenticated -- ``drive.google.com/uc`` sets Content-Disposition, read below;
    * authenticated -- ``/drive/v3/files/{id}?alt=media`` returns the bytes and **no**
      Content-Disposition at all, so the name has to be asked for separately.

    Until 2026-08-19 only the first was implemented, and every file fetched through the
    service account landed as ``<file id>.bin``. It went unnoticed because each rehearsal
    before then ran against a link-shared sheet, which takes the other branch.
    """
    name = None
    if token:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        # Ask for the name first; ?alt=media answers with bytes and nothing else.
        try:
            meta, _ = _get(f"https://www.googleapis.com/drive/v3/files/{file_id}"
                           f"?fields=name", token)
            name = (json.loads(meta).get("name") or "").strip() or None
        except Exception:
            # A missing name is recoverable -- the id-based fallback below still
            # produces a usable file. Failing the whole download would not be.
            name = None
    else:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
    body, headers = _get(url, token)

    # Files above Drive's virus-scan threshold return an HTML interstitial
    # instead of the file, with a confirm token that has to be echoed back.
    if body[:15].lstrip().lower().startswith(b"<!doctype html") or b"<html" in body[:200].lower():
        # Named `confirm_token` so it cannot shadow the OAuth `token` parameter -- they
        # are unrelated, and one silently replacing the other would drop authentication
        # exactly on the retry path.
        confirm_token = re.search(rb'name="confirm"\s+value="([^"]+)"', body)
        uuid = re.search(rb'name="uuid"\s+value="([^"]+)"', body)
        if confirm_token:
            params = {"export": "download", "id": file_id,
                      "confirm": confirm_token.group(1).decode()}
            if uuid:
                params["uuid"] = uuid.group(1).decode()
            body, headers = _get("https://drive.google.com/uc?" + urllib.parse.urlencode(params), token)
        else:
            return None

    if not name:
        disp = headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp)
        if m:
            name = urllib.parse.unquote(m.group(1)).strip()
    if not name:
        name = f"{file_id}.bin"
    # Never let a supplied filename escape the destination directory.
    name = Path(name).name

    dest = dest_dir / name
    dest.write_bytes(body)
    return dest


def slugify(value: str, limit: int = 48) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip()).strip("_")
    return (out[:limit] or "submission")


def submission_dir_name(row: Dict[str, str], index: int) -> str:
    """Stable, sortable directory name for one response row."""
    ts = (row.get("Timestamp") or "").strip()
    try:
        stamp = datetime.strptime(ts, "%m/%d/%Y %H:%M:%S").strftime("%Y%m%dT%H%M%S")
    except ValueError:
        try:
            stamp = datetime.fromisoformat(ts).strftime("%Y%m%dT%H%M%S")
        except ValueError:
            stamp = f"row{index:03d}"
    name = ""
    for k, v in row.items():
        if "name" in k.lower() and "institution" not in k.lower():
            name = v
            break
    return f"{stamp}_{slugify(name, 32)}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched; download nothing")
    ap.add_argument("--all", action="store_true",
                    help="re-fetch submissions already in the ledger")
    args = ap.parse_args(argv)

    cfg = load_config()
    sub_cfg = cfg.get("submissions") or {}
    sheet_id = sub_cfg.get("responses_sheet")
    if not sheet_id:
        print("config/project.yml has no submissions.responses_sheet", file=sys.stderr)
        return 1

    root = repo_root()
    inbox = root / sub_cfg.get("inbox_dir", "curation/intake/submissions")
    inbox.mkdir(parents=True, exist_ok=True)
    ledger_path = inbox / "fetched.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.is_file() else {}

    print("== malavi_rebuild :: fetch_submissions ==")

    # Say which identity is being used before anything is read. A fetch that finds nothing
    # because it could not authenticate looks exactly like a quiet week, and the difference
    # matters: one means no new science, the other means submissions are piling up unseen.
    print(google_auth.describe())
    try:
        token = google_auth.access_token()
    except google_auth.CredentialError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    if token is None:
        print("\nNo Google credential configured, so the only readable sheet is one shared\n"
              "with 'anyone with the link'. MalAvi's sheets are not, since 2026-08-06.\n"
              "Set up read-only access first — see curation/GOOGLE_ACCESS.md.\n"
              "Continuing anyway would report an empty inbox, which is indistinguishable\n"
              "from a week with no submissions.", file=sys.stderr)
        return 1

    try:
        rows = fetch_responses(sheet_id, token)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            # These two are the same mistake wearing different hats, and neither error
            # names the address that needs to be granted access.
            print(f"\nDrive refused the responses sheet ({exc.code}). The sheet must be\n"
                  f"shared with the service account:\n"
                  f"    {google_auth.service_account_email() or 'unknown address'}\n"
                  f"Viewer access is enough.", file=sys.stderr)
            return 1
        raise
    print(f"responses sheet: {len(rows)} row(s)\n")

    new_files = fetched_subs = 0
    for i, row in enumerate(rows, 1):
        dir_name = submission_dir_name(row, i)
        wanted: List[str] = []
        for question, cell in row.items():
            if any(h in (question or "").lower() for h in FILE_COLUMN_HINTS):
                wanted.extend(drive_ids(cell))
        todo = [f for f in wanted if args.all or f not in ledger]

        if not wanted:
            print(f"  {dir_name}: no uploaded files")
            continue
        if not todo:
            print(f"  {dir_name}: up to date ({len(wanted)} file(s))")
            continue

        print(f"  {dir_name}: {len(todo)} new file(s) of {len(wanted)}")
        if args.dry_run:
            for f in todo:
                print(f"      [dry-run] would download {f}")
            continue

        sub_dir = inbox / dir_name
        sub_dir.mkdir(parents=True, exist_ok=True)

        # The form answers are curation context (who submitted, published or not,
        # leaderboard consent, free-text notes). Keep them beside the files.
        meta = {k: v for k, v in row.items() if v}
        meta["_fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (sub_dir / "metadata.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        for file_id in todo:
            try:
                path = download_drive_file(file_id, sub_dir, token)
            except Exception as exc:                      # noqa: BLE001
                print(f"      FAILED {file_id}: {exc}")
                continue
            if path is None:
                print(f"      FAILED {file_id}: could not resolve a download")
                continue
            print(f"      {path.name}  ({path.stat().st_size:,} bytes)")
            ledger[file_id] = {"submission": dir_name, "filename": path.name,
                               "fetched_at": meta["_fetched_at"]}
            new_files += 1
        fetched_subs += 1

    if not args.dry_run:
        ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")

    print(f"\nDone. {fetched_subs} submission(s) touched, {new_files} file(s) downloaded.")
    print(f"Inbox: {inbox}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
