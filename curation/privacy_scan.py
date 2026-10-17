#!/usr/bin/env python3
# @title Refuse to publish anything private: scan a tree bound for a public destination
# @purpose Build, at run time, the list of everything that must never appear in public
#   (Google ids, email addresses, submitters' names, intake directory names, the ledger's
#   reason text, Drive file ids, unpublished sequences, the board password, credential
#   shapes) and refuse a staged tree, a set of files, or a git-history dump that carries
#   any of it. Never prints the value it found: the class, the file and the line only.
# @why The site, the feeds, the public pipeline repository and malaviR are each pushed by
#   a different script with a different, partial check (results/GUARDRAILS_PLAN_2026-10-16.md,
#   section 2). On 2026-10-16 the public config example was found to carry a comment naming
#   a volunteer and a decision about her. One scanner, one forbidden list, every destination.
# @input config/project.yml (privacy.public_addresses, privacy.public_ids; every id in it)
# @input CUSTODY_PRIVATE.md (every id in it)
# @input curation/intake/submissions/ (names, directory names, sequences, the ledger)
# @input config/curators.yml (curators' names, for the pipeline destination only)
# @input publish/privacy_allow.txt (per-destination, per-file exceptions, each with a reason)
# @output the exit code: 0 clean, 1 something private is present, 2 could not run
# @program python3
# @critical-var MIN_SEQUENCE_LENGTH
# @critical-var MIN_REASON_LENGTH
# @critical-var ID_PATTERN
# @critical-flag privacy_scan.py "" --destination
"""Refuse to publish anything private.

    .venv/bin/python curation/privacy_scan.py --destination site  <staged docs tree>
    .venv/bin/python curation/privacy_scan.py --destination feeds docs/assets/data/queue.json ...
    .venv/bin/python curation/privacy_scan.py --destination pipeline <staging dir>
    .venv/bin/python curation/privacy_scan.py --destination malaviR <malaviR tree>
    .venv/bin/python curation/privacy_scan.py --destination history <git log -p dump>
    .venv/bin/python curation/privacy_scan.py --classes          # how many values per class

What is forbidden, and where it comes from
------------------------------------------
Nothing in this file names a private value. The list is built when it runs:

    class            source                                             allowed where
    google_id        every id-shaped token and Google URL in            privacy.public_ids
                     config/project.yml and CUSTODY_PRIVATE.md
    address          any email address (a pattern, not a list)          privacy.public_addresses
    submitter_name   submitter.name in every intake submission.json     contributors.json (the
                     and the name part of every intake directory        feed applies consent)
    intake_dir       every intake directory name                        privacy.public_ids
    reason_text      the ledger's verdict reasons, messages to          nowhere
                     submitters and correction texts
    drive_id         file ids in the ledger's report links and the      nowhere
                     fetch records
    sequence         sequences of LIVE submissions that are not in      nowhere
                     the release (docs/assets/data/lineage_sequences.json)
    board_password   ~/.config/malavi/board_password.txt, if present    nowhere
    credential       token and key shapes (GitHub tokens, a PRIVATE    nowhere
                     KEY block, a service-account key's field name,
                     an Apps Script deployment id)
    curator_name     names in config/curators.yml, minus the            everywhere except the
                     project's own authors (CITATION.cff)              pipeline repository's HEAD

Exceptions beyond the two config lists live in publish/privacy_allow.txt, one per line:
``destination <TAB> class <TAB> path glob <TAB> reason``. The destination may be ``*``.

Why names are matched the way they are
--------------------------------------
A full name, on word boundaries, case-insensitively: "Jane Smith" is found in "jane
smith" and not in "Jane Smithson". For the site, the release-derived data files under
assets/data/ (tables, references, lineage sequences) are exempt from the name class:
a submitter is usually also an author of papers MalAvi already cites (review of
2026-10-16, finding 7). The four feeds are scanned by name as the ``feeds`` destination.

What it prints
--------------
One line per hit: the class, the file and line, and the first four hex characters of
the value's SHA-256, so the maintainer can find which value it was with ``--reveal`` on
their own machine. The value itself is never printed: this program's output goes into
publish logs and CI logs.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation.config import load_config, repo_root  # noqa: E402

# A Google id, a Drive file id or a deployment id: 25+ of these characters with both an
# uppercase letter and a digit. Long snake_case names have neither (the same rule
# publish/push_pipeline.sh has used since 2026-08-08).
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{25,}")
ADDRESS_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Addresses under the reserved documentation domains (RFC 2606 and RFC 6761) belong to
# nobody; the tests and the documentation use them on purpose.
EXAMPLE_DOMAIN = re.compile(r"@(?:[A-Za-z0-9-]+\.)*(?:example\.(?:com|net|org|edu)|"
                            r"[A-Za-z0-9-]+\.(?:test|example|invalid|localhost))$",
                            re.IGNORECASE)
INTAKE_DIR_PATTERN = re.compile(r"^\d{8}T\d{6}_")
CREDENTIAL_PATTERNS = (
    ("github token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("private key block", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("service-account key", re.compile(r'"pri' 'vate_key"')),
    ("apps script deployment id", re.compile(r"AKfycb[A-Za-z0-9_-]{20,}")),
)
MIN_SEQUENCE_LENGTH = 100
MIN_REASON_LENGTH = 24
DESTINATIONS = ("site", "feeds", "pipeline", "malaviR", "history", "any")
FEED_FILES = ("queue.json", "contributors.json", "reserved_names.json", "board.json")
# The ledger's live states (ledger.LIVE_STATES, repeated here so this program does not
# import the ledger's locking machinery to read one list).
LIVE_STATES = ("received", "screening_failed", "ready_for_review", "in_review", "held",
               "awaiting_submitter", "approved")
TEXT_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class Forbidden:
    """One thing that must not appear: a class and either a literal or a regex."""

    cls: str
    value: str = ""                  # the literal, for value classes
    pattern: Optional[re.Pattern] = None
    label: str = ""                  # for pattern classes, what the pattern is

    @property
    def digest(self) -> str:
        return hashlib.sha256((self.value or self.label).encode("utf-8")).hexdigest()[:4]


@dataclass
class Hit:
    cls: str
    path: str
    line: int
    digest: str
    value: str = field(default="", repr=False)   # printed only with --reveal

    def describe(self, reveal: bool = False) -> str:
        shown = f" = {self.value!r}" if reveal and self.value else ""
        return f"  {self.cls:<15} {self.path}:{self.line}  (#{self.digest}){shown}"


@dataclass
class AllowRule:
    destination: str
    cls: str
    glob: str
    reason: str

    def covers(self, destination: str, cls: str, path: str) -> bool:
        return ((self.destination == "*" or self.destination == destination)
                and (self.cls == "*" or self.cls == cls)
                and fnmatch.fnmatch(path, self.glob))


# ======================================================================================
# Building the forbidden list
# ======================================================================================

def id_tokens(text: str) -> Set[str]:
    """Every id-shaped token in a text (see ID_PATTERN)."""
    return {token for token in ID_PATTERN.findall(text)
            if re.search(r"[A-Z]", token) and re.search(r"[0-9]", token)}


def google_ids(root: Path, public_ids: Iterable[str]) -> Set[str]:
    """Every id and deployment URL in the private config and the custody file."""
    found: Set[str] = set()
    for name in ("config/project.yml", "CUSTODY_PRIVATE.md"):
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found |= id_tokens(text)
        found |= set(re.findall(r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]+", text))
    allowed = set(public_ids)
    return {value for value in found if not any(ok in value or value in ok for ok in allowed)}


def _json_file(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def submitter_names(inbox: Path, public_ids: Iterable[str]) -> Set[str]:
    """Full names of every submitter, from submission.json and the directory names."""
    names: Set[str] = set()
    if not inbox.is_dir():
        return names
    public = set(public_ids)
    for directory in sorted(p for p in inbox.iterdir() if p.is_dir()):
        if directory.name in public or not INTAKE_DIR_PATTERN.match(directory.name):
            continue
        data = _json_file(directory / "submission.json")
        name = str(((data.get("submitter") or {}).get("name")) or "").strip()
        if len(name.split()) >= 2:
            names.add(" ".join(name.split()))
        suffix = INTAKE_DIR_PATTERN.sub("", directory.name).replace("_", " ").strip()
        if len(suffix.split()) >= 2 and not suffix.upper().startswith("DEMO"):
            names.add(suffix)
    return names


def intake_directory_names(inbox: Path, public_ids: Iterable[str]) -> Set[str]:
    if not inbox.is_dir():
        return set()
    public = set(public_ids)
    return {p.name for p in inbox.iterdir()
            if p.is_dir() and INTAKE_DIR_PATTERN.match(p.name) and p.name not in public}


def ledger_texts(inbox: Path) -> Tuple[Set[str], Set[str], Dict[str, str]]:
    """(reason texts, Drive ids, {submission id: state}) from the review ledger."""
    reasons: Set[str] = set()
    drive: Set[str] = set()
    states: Dict[str, str] = {}
    data = _json_file(inbox / "review_ledger.json")
    for submission_id, entry in (data.get("entries") or {}).items():
        if not isinstance(entry, dict):
            continue
        states[submission_id] = str(entry.get("state") or "")
        for verdict in entry.get("verdicts") or []:
            for key in ("reason_text", "note_to_submitter", "note"):
                text = str((verdict or {}).get(key) or "").strip()
                if len(text) >= MIN_REASON_LENGTH:
                    reasons.add(text)
        for correction in entry.get("corrections") or []:
            text = str((correction or {}).get("change") or "").strip()
            if len(text) >= MIN_REASON_LENGTH:
                reasons.add(text)
        disposition = entry.get("final_disposition") or {}
        text = str(disposition.get("note") or "").strip()
        if len(text) >= MIN_REASON_LENGTH:
            reasons.add(text)
        drive |= drive_ids_in(entry.get("history") or [])
    for name in ("fetched.json", "fetched_updates.json"):
        drive |= drive_ids_in(_json_file(inbox / name))
    return reasons, drive, states


def drive_ids_in(node) -> Set[str]:
    """Drive ids in a JSON structure: the values of keys that name an id, and the id part
    of any Drive URL. Never every id-shaped token: file NAMES are in the same records and
    a template's file name is public."""
    found: Set[str] = set()

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            if "drive.google.com" in value or "docs.google.com" in value:
                found.update(re.findall(r"/d/([A-Za-z0-9_-]{25,})", value))
                found.update(re.findall(r"[?&]id=([A-Za-z0-9_-]{25,})", value))
            elif "id" in key.lower() and ID_PATTERN.fullmatch(value) \
                    and re.search(r"[A-Z]", value) and re.search(r"[0-9]", value):
                found.add(value)
    walk(node)
    return found


def unpublished_sequences(inbox: Path, states: Dict[str, str], release_sequences: Set[str],
                          id_map: Dict[str, str]) -> Set[str]:
    """Sequences of live submissions that are not in the release.

    "In the release" includes being a FRAGMENT of a released sequence: a submitted
    partial read of a known lineage is public already (the partial-lineage rule makes
    these common), and forbidding it would block the site, whose lineage table contains
    it (code review 2026-10-16, finding 9). A read LONGER than the stored one is new.
    """
    found: Set[str] = set()
    if not inbox.is_dir():
        return found
    release_blob = "|" + "|".join(sorted(release_sequences)) + "|"
    for directory in sorted(p for p in inbox.iterdir() if p.is_dir()):
        public_id = id_map.get(directory.name, "")
        if states.get(public_id, "") not in LIVE_STATES:
            continue
        data = _json_file(directory / "submission.json")
        for item in data.get("sequences") or []:
            raw = item.get("sequence") if isinstance(item, dict) else item
            sequence = re.sub(r"[^ACGTRYKMSWBDHVN]", "", str(raw or "").upper())
            if len(sequence) >= MIN_SEQUENCE_LENGTH and sequence not in release_blob:
                found.add(sequence)
    return found


def release_sequence_set(root: Path) -> Set[str]:
    """Every sequence the public site already serves (the release)."""
    data = _json_file(root / "docs" / "assets" / "data" / "lineage_sequences.json")
    out: Set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= MIN_SEQUENCE_LENGTH:
            cleaned = re.sub(r"[^ACGTRYKMSWBDHVN]", "", node.upper())
            if len(cleaned) >= MIN_SEQUENCE_LENGTH:
                out.add(cleaned)
    walk(data)
    return out


def submission_id_map(inbox: Path) -> Dict[str, str]:
    data = _json_file(inbox / "submission_ids.json")
    return {directory: str((entry or {}).get("id") or "")
            for directory, entry in (data.get("ids") or {}).items()}


def public_people(root: Path, configuration: Optional[Dict] = None) -> Set[str]:
    """People the project itself publishes as its authors: CITATION.cff, plus
    privacy.public_names in the config. Their names are not private anywhere."""
    names: Set[str] = set()
    path = root / "CITATION.cff"
    if path.is_file():
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:                                          # noqa: BLE001
            data = {}

        def walk(node):
            if isinstance(node, dict):
                family = str(node.get("family-names") or "").strip()
                given = str(node.get("given-names") or "").strip()
                if family and given:
                    names.add(f"{given.split()[0]} {family}".lower())
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(data)
    for value in ((configuration or {}).get("privacy") or {}).get("public_names") or []:
        names.add(" ".join(str(value).split()).lower())
    return names


def curator_names(root: Path) -> Set[str]:
    path = root / "config" / "curators.yml"
    if not path.is_file():
        return set()
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                                              # noqa: BLE001
        return set()
    names = set()
    for person in data.get("curators") or []:
        name = str((person or {}).get("name") or "").strip()
        if len(name.split()) >= 2:
            names.add(" ".join(name.split()))
    return names


def board_password(home: Optional[Path] = None) -> str:
    path = (home or Path.home()) / ".config" / "malavi" / "board_password.txt"
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def build_forbidden(root: Optional[Path] = None, configuration: Optional[Dict] = None,
                    inbox: Optional[Path] = None, home: Optional[Path] = None
                    ) -> List[Forbidden]:
    """Everything that must not appear, from the private files of this checkout."""
    root = root or repo_root()
    configuration = configuration if configuration is not None else load_config()
    privacy = configuration.get("privacy") or {}
    public_ids = [str(x) for x in (privacy.get("public_ids") or [])]
    if inbox is None:
        inbox = root / str((configuration.get("submissions") or {})
                           .get("inbox_dir", "curation/intake/submissions"))
    out: List[Forbidden] = []
    for value in sorted(google_ids(root, public_ids)):
        out.append(Forbidden("google_id", value))
    out.append(Forbidden("address", pattern=ADDRESS_PATTERN, label="email address"))
    # A curator's name is public by design (the site credits them), and the maintainer's
    # test submissions sit in the intake tree under his own name; the registry's names are
    # therefore not submitter names, whatever the intake tree says.
    curators = curator_names(root)
    for value in sorted(submitter_names(inbox, public_ids)):
        if value.lower() not in {c.lower() for c in curators}:
            out.append(Forbidden("submitter_name", value))
    for value in sorted(intake_directory_names(inbox, public_ids)):
        out.append(Forbidden("intake_dir", value))
    reasons, drive, states = ledger_texts(inbox)
    for value in sorted(reasons):
        out.append(Forbidden("reason_text", value))
    for value in sorted(drive):
        if not any(ok in value or value in ok for ok in public_ids):
            out.append(Forbidden("drive_id", value))
    for value in sorted(unpublished_sequences(inbox, states, release_sequence_set(root),
                                              submission_id_map(inbox))):
        out.append(Forbidden("sequence", value))
    password = board_password(home)
    if password:
        out.append(Forbidden("board_password", password))
    for label, pattern in CREDENTIAL_PATTERNS:
        out.append(Forbidden("credential", pattern=pattern, label=label))
    public = public_people(root, configuration)
    for value in sorted(curators):
        if value.lower() not in public:
            out.append(Forbidden("curator_name", value))
    return out


# ======================================================================================
# Scanning
# ======================================================================================

def load_allow_rules(path: Optional[Path]) -> List[AllowRule]:
    rules: List[AllowRule] = []
    if path is None or not path.is_file():
        return rules
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4 or not parts[3]:
            raise SystemExit(f"{path}: every allow line needs destination, class, path "
                             f"glob and a reason, tab-separated: {raw!r}")
        rules.append(AllowRule(parts[0], parts[1], parts[2], parts[3]))
    return rules


def is_text(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"\0" not in handle.read(TEXT_SNIFF_BYTES)
    except OSError:
        return False


def files_under(paths: Sequence[Path]) -> List[Path]:
    out: List[Path] = []
    for given in paths:
        if given.is_dir():
            for path in sorted(given.rglob("*")):
                if path.is_file() and ".git" not in path.parts:
                    out.append(path)
        elif given.is_file():
            out.append(given)
    return out


def class_applies(cls: str, destination: str, relative: str) -> bool:
    """The per-destination rules that are not about a single value."""
    name = os.path.basename(relative)
    if cls == "curator_name":
        # Only the pipeline repository's HEAD: a comment about a person is not code. The
        # histories of the public repositories name the curators legitimately (authors,
        # credits), and the site credits them on purpose.
        return destination == "pipeline"
    if cls == "submitter_name":
        if name == "contributors.json":
            return False                     # consent applied by build_site_feeds
        if destination == "site" and "assets/data/" in relative.replace(os.sep, "/") \
                and name not in FEED_FILES:
            return False                     # release-derived data: authors, not submitters
    return True


def _name_pattern(name: str) -> re.Pattern:
    """"Given Family" on word boundaries, and the "Family, Given" form of an author list."""
    parts = name.split()
    forward = r"\s+".join(re.escape(p) for p in parts)
    reversed_form = re.escape(parts[-1]) + r",\s*" + r"\s+".join(re.escape(p) for p in parts[:-1])
    return re.compile(r"(?<![A-Za-z])(?:" + forward + "|" + reversed_form + r")(?![A-Za-z])",
                      re.IGNORECASE)


def scan_text(text: str, relative: str, destination: str, forbidden: Sequence[Forbidden],
              public_addresses: Iterable[str], rules: Sequence[AllowRule]) -> List[Hit]:
    hits: List[Hit] = []
    allowed_addresses = {a.lower() for a in public_addresses}
    lines = text.split("\n")
    upper_lines: Optional[List[str]] = None
    for item in forbidden:
        if not class_applies(item.cls, destination, relative):
            continue
        if any(rule.covers(destination, item.cls, relative) for rule in rules):
            continue
        if item.pattern is not None:
            for number, line in enumerate(lines, 1):
                for match in item.pattern.finditer(line):
                    value = match.group(0)
                    if item.cls == "address" and (value.lower() in allowed_addresses
                                                  or EXAMPLE_DOMAIN.search(value)):
                        continue
                    hits.append(Hit(item.cls, relative, number,
                                    hashlib.sha256(value.encode()).hexdigest()[:4], value))
            continue
        if item.cls == "submitter_name" or item.cls == "curator_name":
            pattern = _name_pattern(item.value)
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append(Hit(item.cls, relative, number, item.digest, item.value))
            continue
        if item.cls == "sequence":
            if upper_lines is None:
                upper_lines = [re.sub(r"[^ACGTRYKMSWBDHVN]", "", line.upper()) for line in lines]
            if item.value not in "".join(upper_lines):
                continue
            for number, line in enumerate(upper_lines, 1):
                if item.value in line:
                    hits.append(Hit(item.cls, relative, number, item.digest, item.value))
            if not any(h.cls == "sequence" and h.path == relative for h in hits):
                hits.append(Hit(item.cls, relative, 0, item.digest, item.value))
            continue
        if item.value not in text:
            continue
        for number, line in enumerate(lines, 1):
            if item.value in line:
                hits.append(Hit(item.cls, relative, number, item.digest, item.value))
    return hits


def scan(paths: Sequence[Path], destination: str, forbidden: Sequence[Forbidden],
         public_addresses: Iterable[str] = (), rules: Sequence[AllowRule] = (),
         base: Optional[Path] = None) -> List[Hit]:
    """Every hit in the files under ``paths``; paths are reported relative to ``base``."""
    hits: List[Hit] = []
    for path in files_under(paths):
        if not is_text(path):
            continue
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(base)) if base and base in path.parents else str(path)
        hits.extend(scan_text(text, relative, destination, forbidden, public_addresses, rules))
    return hits


# ======================================================================================
# Command line
# ======================================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path,
                        help="files or directories bound for the destination")
    parser.add_argument("--destination", choices=DESTINATIONS, default="any",
                        help="which public destination the paths are bound for")
    parser.add_argument("--allow", type=Path, default=None,
                        help="the allow file (default publish/privacy_allow.txt)")
    parser.add_argument("--base", type=Path, default=None,
                        help="report paths relative to this directory")
    parser.add_argument("--reveal", action="store_true",
                        help="print the matched values (local use only; never in a log)")
    parser.add_argument("--classes", action="store_true",
                        help="print how many forbidden values each class holds, and stop")
    arguments = parser.parse_args(argv)

    root = repo_root()
    configuration = load_config()
    try:
        forbidden = build_forbidden(root, configuration)
    except Exception as exc:                                       # noqa: BLE001
        # The class of the error only: its text could quote a private file.
        print(f"privacy_scan: could not build the forbidden list ({type(exc).__name__}); "
              f"run it by hand to see why.", file=sys.stderr)
        return 2
    if arguments.classes:
        counts: Dict[str, int] = {}
        for item in forbidden:
            counts[item.cls] = counts.get(item.cls, 0) + 1
        for cls in sorted(counts):
            print(f"  {cls:<15} {counts[cls]}")
        return 0
    if not arguments.paths:
        parser.error("give at least one path, or --classes")
    allow_path = arguments.allow if arguments.allow is not None \
        else root / "publish" / "privacy_allow.txt"
    rules = load_allow_rules(allow_path)
    public_addresses = [str(a) for a in
                        ((configuration.get("privacy") or {}).get("public_addresses") or [])]
    base = arguments.base.resolve() if arguments.base else None
    paths = [p.resolve() for p in arguments.paths]
    if base is None and len(paths) == 1 and paths[0].is_dir():
        base = paths[0]
    hits = scan(paths, arguments.destination, forbidden, public_addresses, rules, base=base)
    if not hits:
        print(f"privacy_scan: clean ({arguments.destination}; {len(forbidden)} forbidden "
              f"values and patterns checked).")
        return 0
    print(f"privacy_scan: REFUSING {arguments.destination}: {len(hits)} hit(s). Nothing "
          f"here may be published until each is removed or allow-listed with a reason in "
          f"{allow_path.relative_to(root) if allow_path.is_relative_to(root) else allow_path}.",
          file=sys.stderr)
    seen = set()
    for hit in hits:
        line = hit.describe(arguments.reveal)
        if line in seen:
            continue
        seen.add(line)
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
