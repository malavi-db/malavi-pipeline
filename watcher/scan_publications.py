#!/usr/bin/env python3
"""Scan for new haemosporidian publications and emit a batch for the curator.

Runs on a schedule (GitHub Actions). Queries a literature source for recent papers,
de-duplicates against a persisted seen-list, and writes a batch the notifier turns
into an email or a GitHub issue.

Design mirrors malaviTree's NCBI scanner (scripts/05_ncbi_scan.sh): a tunable query
in config, a lookback window, and a de-dupe-against-seen step. The difference is the
target — PAPER-level hits for the curation queue, not mt-genome records.

Sources are configurable (config `watcher.sources`) and queried in one pass, then
merged on a shared DOI-based de-dup key so a paper found in more than one source
appears once (tagged with every source that found it). Currently supported, all
keyless/open REST APIs:
  - europepmc — PubMed/MEDLINE + PMC + preprints + Agricola (boolean query)
  - openalex  — broad cross-publisher index (keyword full-text search)
  - crossref  — DOI-level cross-publisher metadata (keyword relevance search)

The query path is live. Delivery (watcher/notify.py) is still a Phase 1 stub, so the
CI workflow runs the --dry-run path until issue/email delivery is wired.

Usage:
    python watcher/scan_publications.py --out batch.json [--dry-run]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Literature-source REST endpoints (all paper-level, all keyless/open).
EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_WORKS = "https://api.openalex.org/works"
CROSSREF_WORKS = "https://api.crossref.org/works"

# Sent on every request. Europe PMC/OpenAlex/Crossref all use a contact address to
# route callers into a faster, more reliable "polite" pool.
CONTACT_EMAIL = "vaellis@udel.edu"
USER_AGENT = f"malavi-rebuild-watcher/1.0 ({CONTACT_EMAIL})"

# Allow running as a plain script (no install): reuse the curation config loader.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curation" / "src"))
from malavi_curation.config import load_config, repo_root  # noqa: E402

SEEN_PATH = Path(__file__).resolve().parent / "seen.json"


def load_seen() -> set:
    """Load the set of already-reported publication IDs."""
    if SEEN_PATH.is_file():
        return set(json.loads(SEEN_PATH.read_text() or "[]"))
    return set()


def save_seen(seen: set) -> None:
    """Persist the seen-set (sorted, for stable diffs)."""
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2) + "\n")


def normalize_doi(doi: str) -> str:
    """Return a bare, lowercase DOI (no resolver prefix) or "" if there is none.

    DOIs are the one identifier shared across Europe PMC, OpenAlex, and Crossref,
    so they are our cross-source de-dupe key. Different sources decorate them
    differently (``https://doi.org/10.x``, ``doi:10.x``, mixed case), so strip the
    resolver prefix and lowercase before comparing.
    """
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://dx.doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


# Default title-relevance terms (case-insensitive substrings) for the rank-only
# sources. Kept parasite-/disease-specific so front-matter and off-topic papers are
# dropped; recall for generically-titled papers is covered by Europe PMC (boolean,
# searches abstracts) and OpenAlex (full-text search), which are left unfiltered.
DEFAULT_RELEVANCE_TERMS = [
    "haemospor", "haemoproteus", "plasmodium", "leucocytozoon",
    "avian malaria", "malaria", "blood parasite", "parasit",
    "cytochrome b", "cytb",
]


def title_is_relevant(title: str, terms: List[str]) -> bool:
    """True if ``title`` contains any relevance term (case-insensitive substring)."""
    t = (title or "").lower()
    return any(term.lower() in t for term in terms)


def dedup_key(hit: Dict[str, Any]) -> str:
    """Stable key for de-duping a hit within a batch and against the seen-list.

    Prefer the normalized DOI (shared across sources); fall back to the
    source-specific ``id`` when a record has no DOI (common for preprints/patents).
    """
    doi = normalize_doi(hit.get("doi", ""))
    return f"doi:{doi}" if doi else hit["id"]


def clean_title(title: str) -> str:
    """Human-readable title: unescape HTML entities and strip markup tags.

    The literature APIs (notably Crossref) return titles containing HTML entities
    and inline markup such as ``&lt;i&gt;Plasmodium&lt;/i&gt;``. Left as-is, the
    entity decodes to ``<i>`` and, once the tag is removed naively, glues an ``i``
    onto the genus (``iPlasmodium``) — which then defeats genus/relevance matching.
    Unescape entities first, then drop any ``<...>`` tags, then collapse whitespace.
    """
    t = html.unescape(title or "")      # &lt;i&gt; -> <i>, &amp; -> &
    t = re.sub(r"<[^>]+>", "", t)        # remove <i>, </i>, etc.
    return re.sub(r"\s+", " ", t).strip()


def normalize_title(title: str) -> str:
    """Collapse a title to a comparison key: unescaped, lowercase, alnum-only.

    MalAvi stores titles with HTML entities (e.g. ``&amp;``) and varied
    punctuation, and the literature APIs return their own punctuation/casing. To
    match "same paper" across them we unescape entities, lowercase, replace every
    run of non-alphanumeric characters with a single space, and trim.
    """
    t = html.unescape(title or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def load_malavi_titles(path: Path) -> Set[str]:
    """Load MalAvi reference titles as a set of normalized strings for exclusion.

    Accepts either the DataTables export JSON (``{"columns": [...], "data": [[...]]}``)
    that export/build_datatables_json.R produces, or a plain JSON list of titles.
    Titles without a value are skipped. Since MalAvi has no DOI column (yet), a
    normalized-title match is currently the only "already in MalAvi" signal.
    """
    raw = json.loads(Path(path).read_text())
    titles: List[str] = []
    if isinstance(raw, dict) and "columns" in raw and "data" in raw:
        cols = raw["columns"]
        if "TITLE" not in cols:
            return set()
        idx = cols.index("TITLE")
        titles = [row[idx] for row in raw["data"] if idx < len(row)]
    elif isinstance(raw, list):
        titles = raw
    return {normalize_title(t) for t in titles if t}


def query_europepmc(query: str, lookback_days: int) -> List[Dict[str, Any]]:
    """Query Europe PMC for recent papers matching ``query``.

    Restricts ``query`` to a first-publication-date window of the last
    ``lookback_days`` days, pages through all hits with a cursor mark, and returns
    one normalized dict per hit: {id, doi, title, journal, year, url}. ``id`` is
    ``SOURCE:ID`` (e.g. ``MED:38123456``) so it is stable across runs for the
    seen-list de-dupe. Uses only the standard library (urllib).
    """
    # Bound the search to the lookback window on first publication date. Europe PMC
    # supports an inline range filter: FIRST_PDATE:[YYYY-MM-DD TO YYYY-MM-DD].
    end = date.today()
    start = end - timedelta(days=lookback_days)
    dated_query = f"({query}) AND (FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}])"

    results: List[Dict[str, Any]] = []
    cursor = "*"  # Europe PMC cursor paging starts at "*".
    while True:
        params = {
            "query": dated_query,
            "format": "json",
            "resultType": "lite",
            "pageSize": "100",
            "cursorMark": cursor,
        }
        url = EUROPEPMC_SEARCH + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        page = payload.get("resultList", {}).get("result", [])
        for r in page:
            source = r.get("source", "")
            rid = r.get("id", "")
            doi = r.get("doi", "")
            # Prefer the stable SOURCE:ID key; fall back to DOI if either is missing.
            key = f"{source}:{rid}" if source and rid else (rid or doi)
            article_url = (
                f"https://europepmc.org/article/{source}/{rid}"
                if source and rid
                else (f"https://doi.org/{doi}" if doi else "")
            )
            results.append(
                {
                    "id": key,
                    "doi": doi,
                    "title": clean_title(r.get("title", "")),
                    "journal": r.get("journalTitle", ""),
                    "year": r.get("pubYear", ""),
                    "url": article_url,
                    "sources": ["europepmc"],
                }
            )

        # Advance the cursor; stop when the API stops moving it or returns no rows.
        next_cursor = payload.get("nextCursorMark")
        if not page or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return results


def query_openalex(keywords: str, lookback_days: int) -> List[Dict[str, Any]]:
    """Query OpenAlex for recent works matching ``keywords``.

    OpenAlex's ``search`` parameter is a genuine full-text filter (title + abstract
    + indexed full text), so the result set is already narrowed and cursor paging
    through all of it is bounded. We restrict to the lookback window on publication
    date and return normalized hits tagged ``sources: ["openalex"]``.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)

    results: List[Dict[str, Any]] = []
    cursor = "*"  # OpenAlex cursor paging starts at "*".
    while True:
        params = {
            "search": keywords,
            "filter": f"from_publication_date:{start.isoformat()},to_publication_date:{end.isoformat()}",
            "per-page": "100",
            "cursor": cursor,
            "mailto": CONTACT_EMAIL,  # polite-pool routing
        }
        url = OPENALEX_WORKS + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        page = payload.get("results", [])
        for w in page:
            # OpenAlex IDs and DOIs are full URLs; strip to bare forms.
            oa_id = (w.get("id") or "").rsplit("/", 1)[-1]  # e.g. "W4401234567"
            doi = normalize_doi(w.get("doi", ""))
            # Journal name lives under the primary location's source.
            source_obj = (w.get("primary_location") or {}).get("source") or {}
            journal = source_obj.get("display_name", "")
            results.append(
                {
                    "id": f"OPENALEX:{oa_id}" if oa_id else (doi or ""),
                    "doi": doi,
                    "title": clean_title(w.get("title") or w.get("display_name") or ""),
                    "journal": journal,
                    "year": w.get("publication_year", ""),
                    "url": w.get("id", "") or (f"https://doi.org/{doi}" if doi else ""),
                    "sources": ["openalex"],
                }
            )

        # Advance the cursor; OpenAlex returns null once results are exhausted.
        next_cursor = payload.get("meta", {}).get("next_cursor")
        if not page or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return results


def query_crossref(keywords: str, lookback_days: int) -> List[Dict[str, Any]]:
    """Query Crossref for recent works matching ``keywords``.

    IMPORTANT: Crossref's ``query`` parameter only *ranks* results by relevance — it
    does NOT filter them. With just a date filter, the result set is every DOI
    published in the window (which cursor paging would walk in full). So we take
    only the first relevance-ranked page (``rows=100``) rather than paging, which
    surfaces the most on-topic recent DOIs without pulling the entire window.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)

    params = {
        "query": keywords,
        # Crossref filters on the publication date range; query then ranks within it.
        "filter": f"from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()}",
        "rows": "100",
        "mailto": CONTACT_EMAIL,  # polite-pool routing
    }
    url = CROSSREF_WORKS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    results: List[Dict[str, Any]] = []
    for item in payload.get("message", {}).get("items", []):
        doi = normalize_doi(item.get("DOI", ""))
        # Crossref title/container-title are lists; take the first entry if present.
        title_list = item.get("title") or []
        journal_list = item.get("container-title") or []
        # Year lives in nested date-parts; published covers online-or-print.
        date_parts = (item.get("published") or {}).get("date-parts") or [[]]
        year = date_parts[0][0] if date_parts and date_parts[0] else ""
        results.append(
            {
                "id": f"DOI:{doi}" if doi else (item.get("URL", "")),
                "doi": doi,
                "title": clean_title(title_list[0]) if title_list else "",
                "journal": journal_list[0] if journal_list else "",
                "year": year,
                "url": item.get("URL", "") or (f"https://doi.org/{doi}" if doi else ""),
                "sources": ["crossref"],
            }
        )
    return results


# Dispatch table so scan() can iterate the configured `sources` list. Europe PMC
# takes the boolean query; OpenAlex/Crossref take the simpler keyword query.
SOURCE_FUNCS = {
    "europepmc": query_europepmc,
    "openalex": query_openalex,
    "crossref": query_crossref,
}


def scan(
    dry_run: bool = False,
    lookback_days: Optional[int] = None,
    malavi_titles: Optional[Set[str]] = None,
    save: bool = True,
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Run one scan: query every configured source, merge/de-dup, return new items.

    Hits are merged across sources on their :func:`dedup_key` (normalized DOI when
    present) so a paper indexed by more than one source appears once, tagged with
    every source that found it. The merged set is then filtered against the
    seen-list, capped, and the survivors are recorded as seen.

    Args:
        dry_run: print the plan and make no network call.
        lookback_days: override the config lookback window (for ad-hoc local runs).
        malavi_titles: normalized titles already in MalAvi; matching hits are
            dropped (see :func:`load_malavi_titles`). ``None`` disables the check.
        save: persist the seen-list after the run. Set False for exploratory runs
            so repeated scans keep returning the same items.
        max_items: cap on batch size. ``None`` uses the config
            ``max_items_per_batch``; pass a large value for a full catch-up run so
            the title-descending sort does not silently drop early-alphabet papers.
    """
    cfg = load_config()
    wcfg = cfg["watcher"]
    seen = load_seen()
    lookback = lookback_days if lookback_days is not None else wcfg["lookback_days"]
    # Default to Europe PMC alone if no explicit source list is configured.
    sources = wcfg.get("sources", ["europepmc"])
    # Rank-only sources (e.g. Crossref) return relevance-ranked but UNfiltered hits,
    # so their top page is mostly journal front-matter. Post-filter those sources'
    # hits by a title-relevance check; leave server-side-filtered sources untouched.
    relevance_filter_sources = set(wcfg.get("relevance_filter_sources", ["crossref"]))
    relevance_terms = wcfg.get("relevance_terms", DEFAULT_RELEVANCE_TERMS)

    if dry_run:
        # Report the plan without touching the network.
        print("[--dry-run] watcher configuration:")
        print("  sources        :", ", ".join(sources))
        print("  europepmc_query:", wcfg["europepmc_query"])
        print("  keyword_query  :", wcfg.get("keyword_query", "(unset)"))
        print("  lookback_days  :", lookback)
        print("  delivery       :", wcfg["delivery"])
        print("  seen IDs       :", len(seen))
        print("  repo_root      :", repo_root())
        return []

    # Query each configured source. A single source failing (transient API outage,
    # syntax error) should not abort the whole scan — log it and keep the others.
    all_hits: List[Dict[str, Any]] = []
    for src in sources:
        fn = SOURCE_FUNCS.get(src)
        if fn is None:
            print(f"WARNING: unknown watcher source {src!r}; skipping.", file=sys.stderr)
            continue
        # Europe PMC uses its boolean query; the others use the plain keyword query.
        query = wcfg["europepmc_query"] if src == "europepmc" else wcfg.get("keyword_query", "")
        try:
            hits = fn(query, lookback)
            # Drop off-topic noise from rank-only sources via the title filter.
            if src in relevance_filter_sources:
                kept = [h for h in hits if title_is_relevant(h["title"], relevance_terms)]
                print(f"  {src}: {len(hits)} hits, {len(kept)} pass relevance filter", file=sys.stderr)
                hits = kept
            else:
                print(f"  {src}: {len(hits)} hits", file=sys.stderr)
            all_hits.extend(hits)
        except Exception as exc:  # noqa: BLE001 — never let one source kill the run
            print(f"WARNING: source {src!r} failed: {exc}", file=sys.stderr)

    # Merge duplicates across sources on the shared de-dup key, unioning provenance.
    merged: Dict[str, Dict[str, Any]] = {}
    for h in all_hits:
        key = dedup_key(h)
        if key in merged:
            existing = merged[key]
            existing["sources"] = sorted(set(existing["sources"]) | set(h["sources"]))
        else:
            merged[key] = h

    # Drop papers already in MalAvi (matched on normalized title) when a reference
    # list was supplied — the whole point is surfacing what is NOT yet in MalAvi.
    if malavi_titles:
        before = len(merged)
        merged = {
            k: h for k, h in merged.items()
            if normalize_title(h.get("title", "")) not in malavi_titles
        }
        print(f"  MalAvi filter: dropped {before - len(merged)} already-in-MalAvi", file=sys.stderr)

    # Keep only items we have not reported before.
    new = [h for key, h in merged.items() if key not in seen]
    # Deterministic order: newest year first, then title, so batches are stable.
    new.sort(key=lambda h: (str(h.get("year", "")), h.get("title", "")), reverse=True)
    cap = max_items if max_items is not None else wcfg.get("max_items_per_batch", 50)
    new = new[:cap]

    for h in new:
        seen.add(dedup_key(h))
    if save:
        save_seen(seen)
    return new


def main() -> int:
    global SEEN_PATH  # may be repointed by --seen below
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default="batch.json", help="where to write the new-items batch")
    ap.add_argument("--dry-run", action="store_true", help="show config/plan, make no network call")
    ap.add_argument(
        "--lookback-days", type=int, default=None,
        help="override config lookback window (useful for a one-off wide local scan)",
    )
    ap.add_argument(
        "--max-items", type=int, default=None,
        help="override config max_items_per_batch (use a large value for a full "
             "catch-up run so the title sort does not drop early-alphabet papers)",
    )
    ap.add_argument(
        "--malavi-refs", default=None,
        help="path to a MalAvi references JSON (DataTables export or title list); "
             "hits whose title is already in MalAvi are dropped",
    )
    ap.add_argument(
        "--seen", default=None,
        help=f"path to the seen-list JSON (default: {SEEN_PATH}); use a scratch path "
             "for exploratory runs so the committed one is untouched",
    )
    ap.add_argument(
        "--no-save-seen", action="store_true",
        help="do not persist the seen-list (exploratory runs; keeps re-finding items)",
    )
    args = ap.parse_args()

    # Point the module's seen-list at a caller-supplied path if given.
    if args.seen:
        SEEN_PATH = Path(args.seen)

    # Load the MalAvi title exclusion set once, if requested.
    malavi_titles = load_malavi_titles(Path(args.malavi_refs)) if args.malavi_refs else None
    if malavi_titles is not None:
        print(f"Loaded {len(malavi_titles)} MalAvi reference titles for exclusion.", file=sys.stderr)

    batch = scan(
        dry_run=args.dry_run,
        lookback_days=args.lookback_days,
        malavi_titles=malavi_titles,
        save=not args.no_save_seen,
        max_items=args.max_items,
    )
    if not args.dry_run:
        Path(args.out).write_text(json.dumps(batch, indent=2) + "\n")
        print(f"Wrote {len(batch)} new items to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
