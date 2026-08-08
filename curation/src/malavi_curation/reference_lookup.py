"""Fill in a reference's citation details from its DOI, via Crossref.

Mining a PDF reliably yields a DOI and very little else: the title line of a
two-column PDF is often split, prefixed with a journal banner, or -- when the
file is a publisher download -- absent from the text layer entirely. The result
is a submission whose reference is the filename.

Crossref is the DOI registration authority, so asking it what a DOI resolves to
returns the publisher's own deposited metadata. That is a lookup, not a guess:
the same DOI returns the same citation, and nothing is inferred from the text.

Failure is always silent and non-destructive. No network, a 404, a malformed
response: whatever was mined stays exactly as it was. A missing title is a
curator's problem to fix; a wrong one introduced by this module would be worse.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

CROSSREF_URL = "https://api.crossref.org/works/"
TIMEOUT = 20


def _get_json(url: str, mailto: str = "") -> Optional[dict]:
    if mailto:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"mailto": mailto})
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "malavi_rebuild/curation (+https://github.com/vincenzoaellis)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _clean_text(value: Any) -> Optional[str]:
    """Crossref titles carry JATS markup and hard line breaks; flatten them."""
    if not value:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def lookup_doi(doi: str, mailto: str = "") -> Optional[Dict[str, Any]]:
    """Return citation fields for a DOI, or None if it cannot be resolved."""
    doi = (doi or "").strip()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi, flags=re.I)
    if not doi:
        return None
    payload = _get_json(CROSSREF_URL + urllib.parse.quote(doi), mailto=mailto)
    if not payload or "message" not in payload:
        return None
    m = payload["message"]

    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (m.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break

    authors = []
    for a in m.get("author") or []:
        family = (a.get("family") or "").strip()
        if family:
            authors.append(family)

    return {
        "doi": m.get("DOI") or doi,
        "title": _clean_text(m.get("title")),
        "journal": _clean_text(m.get("container-title")),
        "year": year,
        "volume": m.get("volume"),
        "pages": m.get("page"),
        "authors": authors,
        # MalAvi's own citation key convention: "Surname et al YYYY".
        "reference_name": (f"{authors[0]} et al {year}"
                           if authors and year else None),
    }


def enrich_reference(reference: Dict[str, Any], mailto: str = "") -> Dict[str, Any]:
    """Fill blank citation fields on `reference` in place; never overwrite.

    Anything a human or an earlier step already supplied wins. This only fills
    holes, so re-running it cannot quietly change a curator's correction.

    The mined title is treated as a hole when it is obviously a filename rather
    than a title -- no spaces and looking like an accession or DOI suffix -- since
    that is the failure this module exists to repair.
    """
    if not reference.get("doi"):
        return reference
    found = lookup_doi(reference["doi"], mailto=mailto)
    if not found:
        return reference

    title = (reference.get("title") or "").strip()
    looks_like_filename = bool(title) and " " not in title and re.search(r"[-_.]\d", title)
    if looks_like_filename:
        reference.pop("title", None)

    for key in ("title", "journal", "year", "volume", "pages", "reference_name"):
        if found.get(key) and not reference.get(key):
            reference[key] = found[key]
    if found.get("authors") and not reference.get("authors"):
        reference["authors"] = found["authors"]
    return reference
