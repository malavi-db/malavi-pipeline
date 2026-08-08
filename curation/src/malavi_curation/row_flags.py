# @title Per-row validation flags and curator triage tiers
# @purpose Attach curator-facing flags to every extracted row and sort the rows
#          into tiers by how much curator attention each one needs.
# @why A paper can yield thousands of rows (Fecchio et al 2023b: 2,683). A flat
#      list of rows plus a flat list of gate findings is not reviewable; the
#      curator needs to know which rows are candidate new records and which are
#      already-known re-observations.
# @input extracted rows (from table_extract, after lineage/host resolution)
# @input the pinned release index (release_index.load_release_index)
# @output the same rows, with ``flags`` and ``tier`` fields added
# @program python
# @critical-var TIERS
# @critical-var FLAGS
"""Row-level validation as flags, and the tiers built from them.

**Nothing here ever removes, rewrites or rejects a row.** Every function is
additive: it attaches ``flags`` and a ``tier`` and returns. This is a hard
constraint, not a style preference, and it comes from two measured findings:

*Fail-closed host validation deletes correct records.* Astur gentilis,
Agelaioides badius and Casmerodius albus are real birds that the MalAvi
gazetteer does not hold. A row-level rule that dropped unknown hosts would throw
away correct data, so ``host_not_in_malavi`` is information for a curator and
nothing more.

*Precision is structurally capped, so a "wrong-looking" row is often right.*
MalAvi credits the **first** reference to report an association, verified
2026-07-31 against the release: ``ACCNIS07 x Accipiter nisus`` is filed under
Harl et al 2024, not under the 2026 follow-up that also reports it. A follow-up
study therefore produces mostly associations MalAvi already holds. Those rows
were read perfectly; they are simply not new. Tiering separates them out
(``confirms``) instead of treating them as failures.

The tiers answer the curator's real question -- *where do I spend my attention?*

    new         complete, everything resolves, and MalAvi has no record of this
                association: the candidate additions, the actual product
    confirms    complete and resolves, but MalAvi already records the pair under
                one or more references: correctly read, adds nothing new
    review      complete, but something needs a human judgment
    incomplete  no lineage or no host, so it is not an association yet

A row lands in exactly one tier, and ``review`` beats ``new``/``confirms``: if
anything at all needs judgment the row is not presented as a finished answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .release_index import ReleaseIndex, load_release_index, norm_lineage, norm_species

# Every flag this module can raise, and what a curator should read into it. The
# severity is advisory only -- no severity anywhere causes a row to be dropped.
#
#   "warn"  something is probably wrong, or the row disagrees with itself
#   "info"  something a curator must decide, but which is not evidence of error
FLAGS: Dict[str, Dict[str, str]] = {
    "lineage_missing": {
        "severity": "warn",
        "description": "the row carries no lineage name, so it records no association",
    },
    "lineage_unresolved": {
        "severity": "info",
        "description": "the lineage name is in the paper's own namespace and its "
                       "sequence did not resolve to a MalAvi name",
    },
    "lineage_not_in_malavi": {
        "severity": "info",
        "description": "the lineage name is not one MalAvi holds — either genuinely "
                       "new (it must then arrive with a sequence and accession) or "
                       "a name read wrongly",
    },
    "lineage_accession_conflict": {
        "severity": "warn",
        "description": "MalAvi curates this row's accession under a different "
                       "lineage than the row names",
    },
    "host_missing": {
        "severity": "warn",
        "description": "the row carries no host species, so it records no association",
    },
    "vector_missing": {
        "severity": "warn",
        "description": "the row carries no vector species, so it records no association",
    },
    "host_not_in_malavi": {
        "severity": "info",
        "description": "the host binomial is not one MalAvi holds — it may be a host "
                       "new to the database, or a synonym still to be mapped. This is "
                       "NOT evidence the host is wrong",
    },
    "country_missing": {
        "severity": "info",
        "description": "no country on the row, so the association cannot be placed "
                       "geographically",
    },
    "country_not_in_malavi": {
        "severity": "info",
        "description": "MalAvi holds no record from this country yet",
    },
    "source_reprinted": {
        "severity": "info",
        "description": "by the paper's own account this row is another study's data, "
                       "so MalAvi would file it under that reference, not this one",
    },
    "source_uncertain": {
        "severity": "info",
        "description": "the paper pools earlier studies and the row does not say "
                       "which one it came from",
    },
    "prevalence_inconsistent": {
        "severity": "warn",
        "description": "number_found exceeds number_tested",
    },
    "pair_already_in_malavi": {
        "severity": "info",
        "description": "MalAvi already records this lineage × host association under "
                       "another reference, so the row confirms rather than adds",
    },
}

# The tiers, in the order a curator should work through them.
TIERS: Dict[str, str] = {
    "new": "complete and fully resolved, and MalAvi has no record of this "
           "association — the candidate additions",
    "confirms": "complete and fully resolved, but MalAvi already records this "
                "association under another reference",
    "review": "complete, but at least one flag needs a curator's judgment",
    "incomplete": "no lineage, or no host/vector species, so this is not an "
                  "association yet",
}

# Flags that, on their own, send a row to ``review``. Everything not listed here
# is reported but does not by itself demand judgment: a country MalAvi has never
# seen, or a missing country, does not make the *association* doubtful.
#
# ``source_uncertain`` is deliberately **not** here, and the reason is worth
# stating because the opposite choice looks safer and is not. That flag comes
# from a paper-level declaration ("we gathered records from [refs]"), so when it
# fires it fires on every row equally -- all 2,683 of Fecchio et al 2023b's. A
# trigger that selects every row selects nothing, and it would bury the ~170 rows
# there that MalAvi does *not* already hold, which are the only ones a curator
# needs to look at. The caveat belongs once at the paper level, not 2,683 times
# in the queue. ``source_reprinted`` stays a trigger because it is row-level
# evidence: that row's own accession sits outside the set the paper declared.
# The flags that can only be raised by consulting the pinned release. If the release
# tables are not on disk, `load_release_index` returns an EMPTY index rather than
# raising, and `flag_row` guards every lookup with `not index.is_empty` -- so these four
# raise nothing and, without this list, get reported as passed. "MalAvi does not have
# this lineage" and "nobody looked" are opposite statements, and a curator reading a green
# check is entitled to assume the first.
INDEX_BACKED_FLAGS = frozenset({
    "lineage_not_in_malavi",
    "host_not_in_malavi",
    "country_not_in_malavi",
    "pair_already_in_malavi",
})

_REVIEW_FLAGS = frozenset({
    "lineage_unresolved",
    "lineage_not_in_malavi",
    "lineage_accession_conflict",
    "host_not_in_malavi",
    "source_reprinted",
    "prevalence_inconsistent",
})


@dataclass
class RowFlag:
    """One flag raised on one row."""

    code: str                         # a key of FLAGS
    severity: str                     # "warn" | "info"
    message: str                      # curator-facing, specific to this row
    detail: Optional[str] = None      # supporting evidence, when there is any

    def as_dict(self) -> Dict[str, Any]:
        out = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


def _flag(code: str, message: str, detail: Optional[str] = None) -> RowFlag:
    """Build a flag, taking its severity from the FLAGS table."""
    return RowFlag(code=code, severity=FLAGS[code]["severity"],
                   message=message, detail=detail)


def _as_number(value: Any) -> Optional[float]:
    """Parse a cell as a number, or None when it is not one.

    Supplement cells carry all sorts of things in a count column ('n/a', '12
    (3)'), so a value that will not parse is simply not checked rather than
    treated as an error.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def flag_row(row: Dict[str, Any], index: ReleaseIndex,
             own_reference: Optional[str] = None,
             kind: str = "records") -> List[RowFlag]:
    """Every flag raised on one row. Does not mutate the row.

    ``index`` supplies the MalAvi lookups. An empty index (no release tables in
    the checkout) simply means the lookups raise nothing, which is why each one
    is guarded rather than assumed.

    ``own_reference`` is the paper's own MalAvi REFERENCE_NAME, when it has one.
    It only matters for a paper MalAvi has **already** curated -- which is never
    the case for real intake, but is the case for every paper in the ground-truth
    benchmark by construction. Without it the release index reports the paper's
    own rows back to it, and every row it contributed would be labelled "MalAvi
    already records this", crediting the paper to itself.

    ``kind`` is ``"records"`` (lineage in a bird) or ``"vectors"`` (lineage in an
    arthropod), matching ``host_names.canonicalize_rows``. A vector row's second
    half lives in ``vector_species``, and the MalAvi lookups this module has --
    the host table -- say nothing about arthropods, so the host-side and
    pair-membership checks are simply not run for one. Checking a vector row
    against bird taxonomy would flag every correct row in the sheet.
    """
    flags: List[RowFlag] = []
    is_vector = kind == "vectors"

    lineage = (row.get("lineage_name") or "").strip()
    species_field = "vector_species" if is_vector else "host_species"
    species = (row.get(species_field) or "").strip()
    country = (row.get("country") or "").strip()

    # --- the lineage half --------------------------------------------------
    if not lineage:
        flags.append(_flag("lineage_missing",
                           "no lineage name on this row"))
    else:
        # A row whose printed name was in the paper's own namespace and whose
        # sequence did not resolve cannot join to MalAvi by construction. The
        # resolution verdict says exactly which of those happened, so it is
        # reported verbatim rather than re-derived.
        resolution = row.get("lineage_resolution")
        if resolution not in (None, "resolved", "already_named"):
            flags.append(_flag(
                "lineage_unresolved",
                f"lineage '{lineage}' did not resolve from its sequence "
                f"({resolution})",
                detail=row.get("lineage_resolution_note")))
        elif not index.is_empty and not index.knows_lineage(lineage):
            # Only worth saying when the name was *not* the unresolved case
            # above, which would otherwise report the same row twice.
            flags.append(_flag(
                "lineage_not_in_malavi",
                f"lineage '{lineage}' is not a name MalAvi holds"))

        # The row's own accession is an independent witness to which lineage the
        # row is about. When MalAvi already curates that accession under a
        # different name, one of the two is wrong and only a curator can say
        # which -- so this is raised, never acted on.
        curated = index.lineage_for_accession(row.get("accession"))
        if curated and norm_lineage(curated) != norm_lineage(lineage):
            flags.append(_flag(
                "lineage_accession_conflict",
                f"row names lineage '{lineage}', but MalAvi curates its accession "
                f"{row.get('accession')} under '{curated}'"))

    # --- the host (or vector) half -----------------------------------------
    if not species:
        flags.append(_flag("vector_missing" if is_vector else "host_missing",
                           f"no {'vector' if is_vector else 'host'} species on "
                           f"this row"))
    elif not is_vector and not index.is_empty and not index.knows_host(species):
        # Deliberately phrased as a question for the curator. See the module
        # docstring: this must never read as a rejection.
        flags.append(_flag(
            "host_not_in_malavi",
            f"host '{species}' is not a binomial MalAvi holds — new host, or a "
            f"synonym still to map?"))

    # --- geography ---------------------------------------------------------
    if not country:
        flags.append(_flag("country_missing",
                           "no country on this row"))
    elif not index.is_empty and not index.knows_country(country):
        flags.append(_flag("country_not_in_malavi",
                           f"MalAvi holds no record from '{country}' yet"))

    # --- provenance --------------------------------------------------------
    scope = row.get("source_scope")
    if scope == "reprinted":
        flags.append(_flag(
            "source_reprinted",
            "the paper's own data-availability statement places this row outside "
            "the set it deposited",
            detail=row.get("source_scope_evidence")))
    elif scope == "scope_uncertain":
        flags.append(_flag(
            "source_uncertain",
            "the paper pools earlier studies and this row does not say which one "
            "it came from",
            detail=row.get("source_scope_evidence")))

    # --- prevalence --------------------------------------------------------
    tested = _as_number(row.get("number_tested"))
    found = _as_number(row.get("number_found"))
    if tested is not None and found is not None and found > tested:
        flags.append(_flag(
            "prevalence_inconsistent",
            f"number_found ({found:g}) exceeds number_tested ({tested:g})"))

    # --- is this association already MalAvi's? -----------------------------
    # Only meaningful for a complete host row, and only when the names are ones
    # MalAvi could match in the first place. The index is built from the host
    # table, so it cannot answer this for a vector row.
    if lineage and species and not is_vector and not index.is_empty:
        references = index.references_for_pair(lineage, species)
        # A paper cannot confirm itself: drop its own reference before asking
        # whether anyone else holds the association (see ``own_reference``).
        if own_reference:
            references.discard(own_reference)
        if references:
            shown = ", ".join(sorted(references)[:3])
            if len(references) > 3:
                shown += f", +{len(references) - 3} more"
            flags.append(_flag(
                "pair_already_in_malavi",
                f"MalAvi already records {norm_lineage(lineage)} × "
                f"{norm_species(species)}",
                detail=f"credited to: {shown}"))

    return flags


def tier_of(flags: Sequence[RowFlag]) -> str:
    """The tier a row belongs to, given its flags.

    Order matters and is deliberate:

    1. A row missing either half of the association is ``incomplete`` — there is
       nothing to review yet, so it must not clutter the review queue.
    2. Anything needing judgment is ``review``, even if MalAvi already holds the
       pair. A conflict is worth a curator's time whether or not the association
       is novel.
    3. A row MalAvi already holds ``confirms``.
    4. Everything else is ``new``.
    """
    codes = {flag.code for flag in flags}

    if codes & {"lineage_missing", "host_missing", "vector_missing"}:
        return "incomplete"
    if codes & _REVIEW_FLAGS:
        return "review"
    if "pair_already_in_malavi" in codes:
        return "confirms"
    return "new"


def flag_rows(rows: Iterable[Dict[str, Any]],
              index: Optional[ReleaseIndex] = None,
              own_reference: Optional[str] = None,
              kind: str = "records") -> Dict[str, Any]:
    """Flag and tier every row in place. Returns a summary for the run report.

    Mutates each row by adding two fields and nothing else:

    * ``flags`` — a list of flag dicts (empty when the row is clean)
    * ``tier``  — one of :data:`TIERS`

    No row is dropped, reordered or otherwise altered. ``kind`` selects the row
    shape (``"records"`` or ``"vectors"``); see :func:`flag_row`.
    """
    index = index if index is not None else load_release_index()

    tier_counts: Dict[str, int] = {name: 0 for name in TIERS}
    flag_counts: Dict[str, int] = {}

    for row in rows:
        flags = flag_row(row, index, own_reference=own_reference, kind=kind)
        tier = tier_of(flags)
        row["flags"] = [flag.as_dict() for flag in flags]
        row["tier"] = tier

        tier_counts[tier] += 1
        for flag in flags:
            flag_counts[flag.code] = flag_counts.get(flag.code, 0) + 1

    return {
        "tiers": tier_counts,
        "flags": dict(sorted(flag_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "release_index_available": not index.is_empty,
    }


def rows_in_tier(rows: Iterable[Dict[str, Any]], tier: str) -> List[Dict[str, Any]]:
    """The rows sitting in one tier, for a report that renders them separately."""
    return [row for row in rows if row.get("tier") == tier]
