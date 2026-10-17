#!/usr/bin/env python3
# @title Fetch open-access article PDFs + supplements for a DOI list
# @purpose For each DOI, get the open-access article PDF via Unpaywall and scrape
#   the publisher landing page for supplementary files, saving one folder per DOI.
# @why Feeds the curation intake: papers (PDF + supplementary tables) grouped per
#   DOI drop straight into the one-paper-at-a-time intake via batch_intake.py.
# @input a DOI list (--dois-file, else the forward-run dois_to_download.txt, else built-in)
# @output <outdir>/<doi_slug>/ (article_pdf + supplement_NN files) + download_report.csv
# @program python3
# @program requests
# @program bs4
# @critical-var EMAIL
# @critical-var OUTDIR
"""Open-access article + supplement downloader.

Uses Unpaywall (needs a contact email) to find each DOI's open-access PDF, then
resolves the DOI to its landing page and scrapes likely supplement links. Only
open-access PDFs are retrievable; paywalled articles yield landing-page metadata
and whatever supplements the publisher exposes without authentication.
"""
from __future__ import annotations

import argparse
import csv
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Contact email required by the Unpaywall API and sent in the User-Agent. The
# curator's address; override with --email.
EMAIL = "vaellis@udel.edu"

# Built-in fallback DOI list (the 35 forward-look papers). Prefer --dois-file or
# the saved dois_to_download.txt so this stays in sync with the watcher output.
BUILTIN_DOIS = """
10.1007/s11230-026-10281-z
10.1016/j.avrs.2026.100348
10.1007/s10336-026-02398-1
10.1292/jvms.25-0363
10.1186/s12936-026-05924-x
10.1016/j.actatropica.2026.108187
10.71424/azb78.2.002811
10.1186/s12936-026-05879-z
10.1007/s11686-026-01301-5
10.1016/j.ijppaw.2026.101212
10.1007/s00436-026-08667-5
10.1111/mec.70374
10.1016/j.ijppaw.2026.101222
10.1002/ece3.73560
10.1007/s00436-026-08695-1
10.1645/24-88
10.1038/s41598-026-51361-w
10.1002/ece3.73550
10.1111/1749-4877.70126
10.1007/s10336-026-02407-3
10.1177/17581559261449040
10.1016/j.actatropica.2026.108081
10.1002/jav.03580
10.1093/jme/tjag098
10.1186/s12936-026-05925-w
10.14202/vetworld.2026.1459-1469
10.1016/j.actatropica.2026.108178
10.3390/vetsci13050457
10.1007/s10336-026-02414-4
10.1002/vms3.71047
10.64898/2026.04.15.718266
10.21203/rs.3.rs-9368372/v1
10.1007/s10336-026-02392-7
10.1111/1749-4877.70095
10.1007/s10336-026-02417-1
"""

SUPPLEMENT_HINTS = [
    "supplement", "supplementary", "supporting", "additional file",
    "additional-file", "additional_file", "appendix", "esm", "mediaobjects",
    "mediaobject", "mmc", "dataset", "data set", "figshare", "zenodo", "dryad",
    "datadryad", "supporting information", "supporting-information",
    "supporting_information",
]

# Downloadable attachment extensions. PDFs are treated specially (see
# looks_like_supplement_link) so the main article PDF is not misfiled.
DOWNLOAD_EXTENSIONS = [
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".tsv", ".txt", ".zip",
    ".rar", ".7z", ".gz", ".tar", ".fasta", ".fas", ".fa", ".nwk", ".nex",
    ".tre", ".xml", ".json", ".rtf", ".ppt", ".pptx",
]
# URL path segments that mark a PDF as a supplement container across publishers
# (Springer ESM "MOESM", Elsevier "mmc", generic "suppl"/"supporting"/"additional").
_SUPP_PDF_PATH_MARKERS = (
    "moesm", "/esm", "mediaobject", "mmc", "suppl", "supporting", "additional",
)


def make_headers(email: str) -> dict:
    return {
        "User-Agent": f"OA-PDF-and-supplement-downloader/1.0 mailto:{email}",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def clean_filename(text: str, max_len: int = 140) -> str:
    text = (text or "").strip()
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^\w\-\.]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:max_len].strip("_")


def doi_slug(doi: str) -> str:
    return clean_filename(doi.replace("/", "_"))


def resolve_doi(doi: str, headers: dict) -> "tuple[str, str]":
    r = requests.get(f"https://doi.org/{doi}", headers=headers, timeout=30,
                     allow_redirects=True)
    r.raise_for_status()
    return r.url, r.text


def get_unpaywall_pdf_url(doi: str, email: str, headers: dict):
    url = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}"
           f"?email={urllib.parse.quote(email)}")
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        return "", "", "", "not_found_in_unpaywall"
    r.raise_for_status()
    data = r.json()
    title = data.get("title") or ""
    is_oa = data.get("is_oa")
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or ""
    landing_url = best.get("url") or ""
    if not pdf_url:
        for loc in data.get("oa_locations") or []:
            if loc.get("url_for_pdf"):
                pdf_url = loc.get("url_for_pdf") or ""
                landing_url = loc.get("url") or ""
                break
    if not is_oa:
        return title, landing_url, "", "not_open_access"
    if not pdf_url:
        return title, landing_url, "", "oa_but_no_direct_pdf"
    return title, landing_url, pdf_url, "pdf_url_found"


# Citation/indexing/navigation links that publisher pages label near supplement
# wording but are NOT files (Google Scholar, PubMed, author pages, reference DOIs).
_EXCLUDE_MARKERS = (
    "scholar.google", "scholar_lookup", "pubmed", "/author", "view author",
    "google scholar", "citation", "crossref.org", "/cas/redirect",
)

# Publisher policy, help and service pages. These are boilerplate that has
# nothing to do with the article, but they are worded like supplements and so
# survive every other filter.
#
# The case that motivated this: Cambridge journal pages link to
# "Data and supporting evidence for journals" at
# /core/services/publishing-ethics/data-and-supporting-evidence-for-journals.
# The anchor text contains "supporting" (a SUPPLEMENT_HINT) and the URL path
# contains "supporting" (a _SUPP_PDF_PATH_MARKERS entry), so it was captured as
# a supplement for every Cambridge paper -- ~770 KB of HTML filed as an article
# supplement, silently inflating supplement counts.
_POLICY_PATH_MARKERS = (
    "/core/services/", "publishing-ethics", "/about/", "/policies", "/policy",
    "/terms", "/legal", "/help/", "/faq", "/permissions", "/rights",
    "/information-for-authors", "/for-authors", "/author-guidelines",
    "/open-access-policies", "/researcher-services",
)


def looks_like_supplement_link(text: str, href: str) -> bool:
    """True if a link is a likely supplementary file (and not the main article PDF,
    a citation link, or page navigation)."""
    combined = f"{text or ''} {href or ''}".lower()
    path = urlparse(href or "").path.lower()
    # Drop citation/indexing/navigation links outright.
    if any(mark in combined for mark in _EXCLUDE_MARKERS):
        return False
    # Drop publisher policy/help/service pages. Checked against the path only:
    # matching the anchor text would reject real supplements whose captions
    # happen to mention terms or permissions.
    if any(mark in path for mark in _POLICY_PATH_MARKERS):
        return False
    # A real downloadable attachment (any extension) is the strongest signal;
    # accept it even if the anchor text lacks supplement wording.
    if any(path.endswith(ext) for ext in DOWNLOAD_EXTENSIONS if ext != ".pdf"):
        return True
    # Explicit supplement wording anywhere in the link text or URL. Require it to
    # also look like a file/handler (has an extension or a download path segment)
    # so journal "Supplement <issue>" archive links are not mistaken for this
    # paper's supplement.
    if any(hint in combined for hint in SUPPLEMENT_HINTS):
        if ("download" in combined or "." in path.rsplit("/", 1)[-1]
                or any(seg in path for seg in _SUPP_PDF_PATH_MARKERS)):
            return True
        return False
    # A .pdf counts only when the URL path shows a supplement container, so the
    # main article PDF is not misfiled as a supplement.
    if path.endswith(".pdf") and any(seg in path for seg in _SUPP_PDF_PATH_MARKERS):
        return True
    return False


def guess_extension(url: str, response) -> str:
    ext = os.path.splitext(urlparse(url).path)[1]
    if ext and len(ext) <= 10:
        return ext
    ctype = response.headers.get("Content-Type", "").split(";")[0].strip()
    return mimetypes.guess_extension(ctype) or ".bin"


def classify_http_error(error: "requests.HTTPError") -> str:
    """Turn an HTTP error into a status a human can act on.

    A 403 from a publisher is not "this paper is unavailable" -- MDPI and Wiley
    both refuse scripted requests regardless of the article's license, and the
    same URL succeeds in a browser. Reporting that as a generic error invites
    exactly the wrong conclusion (it reads like "not open access"), so it gets
    its own status naming the remedy.
    """
    status_code = error.response.status_code if error.response is not None else None
    if status_code in (401, 403):
        return f"publisher_blocked_{status_code}_needs_manual_download"
    if status_code == 404:
        return "not_found_404"
    return f"download_error:HTTPError:{status_code or 'unknown'}"


def download_file(url: str, outfile_base: str, headers: dict):
    try:
        with requests.get(url, headers=headers, timeout=60, stream=True,
                          allow_redirects=True) as r:
            r.raise_for_status()

            # Refuse to save a web page as if it were an attachment. Landing
            # pages, policy pages and login walls all return HTML with HTTP 200,
            # and saving them produces a file that looks like a supplement in
            # every listing but carries none of the paper's data. Checked here
            # rather than only at link level so it catches redirects to a login
            # page and any publisher this filter has not seen yet.
            content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            path_extension = os.path.splitext(urlparse(r.url).path)[1].lower()
            if content_type in ("text/html", "application/xhtml+xml") \
                    and path_extension not in DOWNLOAD_EXTENSIONS:
                return "", "skipped_html_not_a_file"

            ext = guess_extension(r.url, r)
            outfile = outfile_base if outfile_base.lower().endswith(ext.lower()) \
                else outfile_base + ext
            with open(outfile, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return outfile, "downloaded"
    except requests.HTTPError as e:
        return "", classify_http_error(e)
    except Exception as e:  # network error -> record and continue
        return "", f"download_error:{type(e).__name__}:{e}"


def find_supplement_links(landing_url: str, html: str):
    """Scrape anchor links (and common publisher data-* download attributes) that
    look like supplements. Restricted to <a> tags to stay fast and avoid matching
    stray CSS/JS asset URLs."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a"):
        text = " ".join(a.get_text(" ", strip=True).split())
        for attr in ("href", "data-href", "data-url", "data-download-url"):
            val = a.get(attr)
            if not val:
                continue
            abs_url = urljoin(landing_url, val)
            if looks_like_supplement_link(text, abs_url):
                found.append({"text": text, "url": abs_url, "source": attr})
    seen, unique = set(), []
    for item in found:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    return unique


def build_candidate_extra_pages(landing_url: str):
    """Some publishers put supplements on a separate page (Springer/Nature)."""
    candidates = []
    if "/article/" in landing_url:
        candidates.append(landing_url.rstrip("/") + "/supplementary-information")
    if "nature.com/articles/" in landing_url:
        candidates.append(landing_url.rstrip("/") + "/supplementary-information")
    return candidates


def fetch_html(url: str, headers: dict):
    r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        return r.url, ""
    return r.url, r.text


def load_dois(dois_file: str) -> "list[str]":
    if dois_file:
        raw = Path(dois_file).read_text()
    else:
        # Prefer the saved forward-run DOI list if present, else the built-in one.
        saved = (Path(__file__).resolve().parent.parent / "watcher" / "runs")
        found = sorted(saved.glob("forward_*/dois_to_download.txt")) if saved.is_dir() else []
        raw = found[-1].read_text() if found else BUILTIN_DOIS
    dois = []
    for line in raw.splitlines():
        d = line.strip()
        if d and not d.startswith("#") and d.lower().startswith("10."):
            dois.append(d)
    return dois


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", default=EMAIL, help="contact email for Unpaywall")
    ap.add_argument("--dois-file", default=None,
                    help="file of DOIs (one per line); default: saved forward-run list")
    ap.add_argument("--outdir", default="curation/intake/downloads",
                    help="where per-DOI folders are written")
    ap.add_argument("--report", default=None, help="CSV report path (default: <outdir>/download_report.csv)")
    args = ap.parse_args(argv)

    headers = make_headers(args.email)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report_path = args.report or str(outdir / "download_report.csv")

    dois = load_dois(args.dois_file)
    print(f"Fetching {len(dois)} DOIs -> {outdir}/ (email {args.email})")
    report_rows = []

    for idx, doi in enumerate(dois, start=1):
        print(f"\n[{idx}/{len(dois)}] {doi}")
        article_dir = outdir / doi_slug(doi)
        article_dir.mkdir(parents=True, exist_ok=True)
        title = landing_url = resolved_url = pdf_url = ""
        article_pdf_file = article_pdf_status = ""
        # --- Phase 1: the article PDF, via Unpaywall -------------------------
        # Kept in its own try so that a later failure on the publisher's landing
        # page cannot discard what we learned here. Before this split, a 403
        # from the landing page aborted the whole DOI and the article_pdf row
        # was never written, so the report could not say whether the PDF itself
        # had downloaded -- the failure mode that made three held-out papers
        # look unobtainable when only their landing pages were blocked.
        try:
            title, upw_landing, pdf_url, upw_status = get_unpaywall_pdf_url(
                doi, args.email, headers)
            landing_url = upw_landing or ""
            if pdf_url:
                article_pdf_file, article_pdf_status = download_file(
                    pdf_url, str(article_dir / "article_pdf"), headers)
            else:
                article_pdf_status = upw_status
        except requests.HTTPError as e:
            article_pdf_status = classify_http_error(e)
        except Exception as e:
            article_pdf_status = f"error:{type(e).__name__}:{e}"

        # --- Phase 2: supplements, scraped from the landing page -------------
        try:
            resolved_url, html = resolve_doi(doi, headers)
            landing_url = landing_url or resolved_url
            supplement_links = find_supplement_links(resolved_url, html)
            for extra_url in build_candidate_extra_pages(resolved_url):
                try:
                    er, eh = fetch_html(extra_url, headers)
                    if eh:
                        supplement_links.extend(find_supplement_links(er, eh))
                except Exception:
                    pass
            # dedup after adding extra pages
            seen, deduped = set(), []
            for link in supplement_links:
                if link["url"] not in seen:
                    seen.add(link["url"])
                    deduped.append(link)
            supplement_links = deduped

            if not supplement_links:
                report_rows.append({"doi": doi, "title": title, "item_type": "supplement",
                                    "item_label": "", "status": "no_supplement_links_found",
                                    "url": "", "file": "", "landing_url": resolved_url})
            for sidx, supp in enumerate(supplement_links, start=1):
                label = clean_filename(supp["text"] or f"supplement_{sidx}", max_len=80) \
                    or f"supplement_{sidx}"
                supp_file, supp_status = download_file(
                    supp["url"], str(article_dir / f"supplement_{sidx:02d}_{label}"), headers)
                print(f"  supplement {sidx}: {supp_status} - {supp['url']}")
                report_rows.append({"doi": doi, "title": title, "item_type": "supplement",
                                    "item_label": supp["text"], "status": supp_status,
                                    "url": supp["url"], "file": supp_file,
                                    "landing_url": resolved_url})
        except requests.HTTPError as e:
            status = classify_http_error(e)
            print(f"  landing page: {status}")
            report_rows.append({"doi": doi, "title": title, "item_type": "supplement",
                                "item_label": "", "status": f"landing_page_{status}",
                                "url": "", "file": "",
                                "landing_url": resolved_url or landing_url})
        except Exception as e:
            report_rows.append({"doi": doi, "title": title, "item_type": "supplement",
                                "item_label": "",
                                "status": f"landing_page_error:{type(e).__name__}:{e}",
                                "url": "", "file": "",
                                "landing_url": resolved_url or landing_url})

        # Always emitted, whatever happened above, so every DOI has an
        # article_pdf row and the report can be read as a complete census.
        report_rows.append({"doi": doi, "title": title, "item_type": "article_pdf",
                            "item_label": "article_pdf", "status": article_pdf_status,
                            "url": pdf_url, "file": article_pdf_file,
                            "landing_url": resolved_url or landing_url})
        time.sleep(0.5)

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["doi", "title", "item_type", "item_label",
                                               "status", "url", "file", "landing_url"])
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"\nDone. Downloads: {outdir}/   Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
