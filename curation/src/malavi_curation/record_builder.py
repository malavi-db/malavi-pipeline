"""Assemble parser outputs into a submission record (schemas/submission.schema.json).

Combines reference metadata, mined accessions, and extracted host/geography into
one candidate submission dict marked ``needs_review=True``. The result is staged
for a curator and for malaviR validation; nothing is auto-ingested.

Host↔lineage↔locality associations are NOT invented here: the main text gives the
*set* of hosts and localities, but the per-row matrix usually lives in a paper's
supplementary table. Each candidate record therefore carries a host (and the sole
country when unambiguous), with lineage/genus left null for the curator.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .accession_mine import AccessionHits
from .config import repo_root
from .hosts_geography import HostGeography
# Genus and count coercion live in ``normalize`` so that the template adapter and this
# builder cannot drift into disagreeing about what a count or a parasite genus is --
# which is exactly the kind of silent divergence that makes two intake paths produce
# different objects from the same facts. The private aliases are kept because they read
# as local helpers everywhere below.
from .normalize import clean_count as _clean_count
from .normalize import clean_genus as _clean_genus


@lru_cache(maxsize=1)
def _submission_schema() -> Dict[str, Any]:
    path = repo_root() / "schemas" / "submission.schema.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


_RECORD_FIELDS = ("lineage_name", "host_species", "country", "site",
                  "coordinates", "parasite_genus", "number_tested", "number_found",
                  "notes", "source_scope", "tier", "flags")
_VECTOR_FIELDS = ("lineage_name", "vector_species", "vector_method", "country",
                  "site", "notes", "source_scope", "tier", "flags")


def _row_notes(raw: Dict[str, Any]) -> List[str]:
    """Provenance from a structured row that has no field of its own in the schema.

    The submission schema fixes the record fields (``additionalProperties: false``),
    so anything the curator needs to *see* but MalAvi does not store goes here.
    """
    notes: List[str] = []
    # An accession on the row shows the lineage<->accession link (it is also
    # collected top-level, where the gate checks it against INSDC).
    if raw.get("accession"):
        notes.append(f"accession: {raw['accession']}")
    # Where the lineage name came from, when it did not come from the page. A
    # curator must never see a name they cannot trace: this row said "T001", and
    # the name beside it was derived from the sequence the paper printed.
    # Where the host or vector name came from, when the document's wording is not
    # the name MalAvi files the species under.
    for field_name in ("host_species", "vector_species"):
        original = raw.get(f"{field_name}_source")
        if original:
            detail = raw.get(f"{field_name}_note") or ""
            notes.append(f"{field_name} '{original}' mapped to MalAvi's name"
                         + (f": {detail}" if detail else ""))
    verdict = raw.get("lineage_resolution")
    if verdict:
        printed = raw.get("lineage_name_source")
        origin = (f"lineage name resolved from sequence"
                  + (f" (paper printed '{printed}')" if printed else "")
                  if verdict == "resolved" else
                  f"lineage unresolved from sequence [{verdict}]")
        detail = raw.get("lineage_resolution_note")
        notes.append(f"{origin}: {detail}" if detail else origin)
    # Provenance: say so in the notes whenever the paper's own words indicate the
    # row is not this study's result. "unknown" is the ordinary case and would
    # only add noise, so it is left unsaid.
    scope = raw.get("source_scope")
    if scope in ("reprinted", "scope_uncertain"):
        headline = ("reprinted from another study -- do NOT file under this "
                    "reference without checking"
                    if scope == "reprinted" else
                    "this paper pools earlier studies; which study this row came "
                    "from is not stated")
        evidence = raw.get("source_scope_evidence")
        notes.append(f"{headline}: {evidence}" if evidence else headline)
    return notes


def _normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a structured row (e.g. from a supplementary table) into a schema record."""
    rec = {f: raw.get(f) for f in _RECORD_FIELDS}
    # Constrain fields with a fixed type/enum so a messy supplement row degrades to
    # a review-flagged None instead of crashing schema validation for the paper.
    rec["parasite_genus"] = _clean_genus(rec["parasite_genus"])
    rec["number_tested"] = _clean_count(rec["number_tested"])
    rec["number_found"] = _clean_count(rec["number_found"])
    notes = _row_notes(raw)
    if notes and not rec["notes"]:
        rec["notes"] = "; ".join(notes)
    return rec


def _normalize_vector(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a structured vector row into a schema vector record."""
    rec = {f: raw.get(f) for f in _VECTOR_FIELDS}
    notes = _row_notes(raw)
    if notes and not rec["notes"]:
        rec["notes"] = "; ".join(notes)
    return rec


def build_submission(
    reference: Dict[str, Any],
    accessions: Optional[AccessionHits] = None,
    hostgeo: Optional[HostGeography] = None,
    structured_records: Optional[List[Dict[str, Any]]] = None,
    structured_vectors: Optional[List[Dict[str, Any]]] = None,
    submitter: Optional[Dict[str, str]] = None,
    tool_version: Optional[str] = None,
    validate: bool = True,
) -> Dict[str, Any]:
    """Build a submission dict conforming to schemas/submission.schema.json.

    Args:
        reference: {doi?, pmid?, title?, year?} — the source publication.
        accessions: mined accessions (``AccessionHits``); all candidates are
            included for the curator to prune.
        hostgeo: extracted hosts/vectors/countries (``HostGeography``).
        structured_records: rows parsed from supplementary tables, each with any
            of lineage_name/host_species/country/site/parasite_genus/number_tested/
            number_found/accession. When present these become the records (they
            carry real lineage↔host↔locality associations and any reported
            prevalence); otherwise records are built from ``hostgeo``.
        structured_vectors: vector rows parsed from supplementary tables (each with
            any of lineage_name/vector_species/vector_method/country/site/accession).
            When present these become the vector records; otherwise candidate
            vectors are built from ``hostgeo.vectors``.
        submitter: {name, email?}; defaults to the curation helper.
        tool_version: recorded in provenance (defaults to the package version).
        validate: jsonschema-validate the result before returning.
    """
    accessions = accessions or AccessionHits()
    hostgeo = hostgeo or HostGeography()
    submitter = submitter or {"name": "curation_helper"}

    all_accessions = set(accessions.all())
    # Attach the country only when the paper mentions exactly one (unambiguous);
    # otherwise leave it for the curator (all candidates are kept in provenance).
    sole_country = hostgeo.countries[0] if len(hostgeo.countries) == 1 else None

    if structured_records:
        # Real associations from a supplementary table take precedence.
        records = [_normalize_record(r) for r in structured_records]
        # A MalAvi record is lineage x host x *place*, but supplementary tables
        # very often carry no country column at all -- which left every record
        # geographically anonymous and scored 0% on the full triple. When the
        # paper names exactly one country, that country is the study's country
        # and every record from it belongs there; when it names several, the
        # attribution is ambiguous and stays the curator's call. This copies a
        # fact the document states, and only in the unambiguous case; it does not
        # infer one.
        if sole_country:
            for record in records:
                if not record.get("country"):
                    record["country"] = sole_country
        all_accessions.update(
            r["accession"] for r in structured_records if r.get("accession"))
    else:
        # No structured table, so no records. This is deliberate.
        #
        # A MalAvi record is an ASSOCIATION: this lineage, in this host, at this
        # place. Prose mining yields neither half of that association linked to
        # the other -- it yields a list of binomials that appear somewhere in the
        # document, which includes every species in a comparison table, a
        # phylogeny, or a reference list. Emitting one record per such name does
        # not produce weak records; it produces false ones.
        #
        # Measured on the Gambia hooded-vulture paper: 15 records were emitted
        # and NONE was one of the study's own two records. Among them were a
        # little penguin (*Eudyptula minor*) and two names damaged by line
        # breaks. The paper's real records appear only in its narrative, and
        # neither supplement contains them (one is PCR conditions, the other a
        # distance matrix), so there was nothing better to fall back to.
        #
        # The host names are still valuable as a lead, so they are kept in
        # provenance where a curator can see them, clearly labelled as
        # unconfirmed mentions rather than as data.
        records = []

    # Structured vector rows carry the lineage↔vector association; prose-mined
    # vector species carry no lineage but are still leads a curator wants. Both
    # are kept: a paper's supplement may cover only the mosquitoes it sequenced,
    # while the text names every species it trapped.
    vectors = [_normalize_vector(v) for v in (structured_vectors or [])]
    all_accessions.update(
        v["accession"] for v in (structured_vectors or []) if v.get("accession"))
    if sole_country:
        for vector in vectors:
            if not vector.get("country"):
                vector["country"] = sole_country

    already_named = {(v.get("vector_species") or "").strip().lower() for v in vectors}
    vectors.extend(
        {
            "lineage_name": None,
            "vector_species": vec,
            "vector_method": None,
            "country": sole_country,
            "site": None,
            "notes": None,
        }
        for vec in hostgeo.vectors if vec.strip().lower() not in already_named
    )

    submission: Dict[str, Any] = {
        "submitter": submitter,
        "reference": {
            "doi": reference.get("doi"),
            "pmid": reference.get("pmid"),
            "title": reference.get("title"),
            "year": reference.get("year"),
        },
        "accessions": sorted(all_accessions),
        "records": records,
        "vectors": vectors,
        "provenance": {
            "source": "curation_helper",
            "tool_version": tool_version or __version__,
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "needs_review": True,
            # Extra context for the curator report (nested extra keys are allowed
            # by the schema). Ranges show which accessions were inferred; the
            # candidate countries list lets the curator see all localities even
            # when none could be unambiguously attached to a record; and the flag
            # warns that the host x lineage matrix may be in supplementary data.
            "accession_ranges": accessions.ranges,
            "candidate_countries": list(hostgeo.countries),
            # Host binomials seen anywhere in the document. Leads for a curator,
            # NOT records: nothing here is tied to a lineage, and the list
            # necessarily includes hosts of comparison lineages from phylogenies
            # and reference tables. They become records only once a human or a
            # structured table supplies the lineage they belong to.
            "candidate_hosts": list(hostgeo.hosts),
            "needs_supplement": hostgeo.needs_supplement,
        },
    }

    if validate:
        import jsonschema  # dev dependency

        jsonschema.validate(submission, _submission_schema())

    return submission
