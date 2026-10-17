# @title Synonymy and ambiguity review: the candidate pairs, the facts per side, the proposal
# @purpose Screen every pair of stored lineages for a zero pairwise-deletion distance,
#          classify each such pair (duplicate / contained / ambiguous), attach the record
#          counts, hosts, references and sequence facts per side, apply the curators' rule
#          to propose an action, and turn the curators' written decisions back into
#          lineage-history events.
# @why The curators voted on 2026-09-24 to simplify MalAvi's synonymies and ambiguities,
#      but only pair by pair and only with a public trail. This module is the candidate
#      list (the same definitions as malaviR's synonymy_report() and ambiguous_pairs(),
#      computed from the record store) and the bridge from a filled-in decision sheet to
#      record_lineage_event's four events, so nothing is applied that was not decided.
# @input data/records/*.csv (through the caller)
# @output review rows (dicts) and lineage_history.Event objects (through the caller)
# @program python
# @critical-var MIN_COMPARABLE
# @critical-var SMALL_LINEAGE_MAX_HOST_RECORDS
# @critical-var DECISIONS
"""Synonymy and ambiguity review for the lineage cleanup pass.

Definitions (the same as malaviR, on the store's 479-position frame):

* A position is **determined** in a sequence when it carries A, C, G or T. Gaps, N and
  every other IUPAC code are **undetermined**: absence of evidence, not agreement.
* Two lineages have **distance 0** when they carry the same base at every position that is
  determined in both. Only such pairs are candidates; a real base-against-base difference
  anywhere makes two lineages distinct and is never listed here.
* A distance-0 pair is a **duplicate** when both determine exactly the same positions (the
  two names carry one sequence), **contained** when one determines a subset of the other's
  positions (the partial one is *contained* in the more complete one, which is what
  malaviR's ``synonymy_report`` groups), or **ambiguous** when each determines at least one
  position the other lacks (``ambiguous_pairs``: neither can stand for both).

The proposal rule (:func:`propose`) is the curators' vote of 2026-09-24 made mechanical:
a synonym that adds little may be merged into the more complete lineage. Everything the
rule cannot decide is marked for the curators, never proposed. The knobs are
``MIN_COMPARABLE`` (below it the evidence is too thin to propose anything; the same floor
the ingest uses to call a submitted sequence a known lineage) and
``SMALL_LINEAGE_MAX_HOST_RECORDS`` (a partial with at most this many host records is
"adds little").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import lineage_history, normalize
from .sequence_check import MIN_COMPARABLE_TO_RANK

# Below this many positions determined in both, a zero distance says little, so the
# review lists the pair but proposes nothing. One constant with the ingest's known-lineage
# rule, so the pass and the screen agree on what "identical over the overlap" needs.
MIN_COMPARABLE = MIN_COMPARABLE_TO_RANK

# "Adds little" (the curators' example was one host record): a contained lineage with at
# most this many host records is proposed for merging into its unique container.
SMALL_LINEAGE_MAX_HOST_RECORDS = 1

# What a curator may write in the DECISION column of the review sheet. ``merge``: LINEAGE
# folds into INTO and its name becomes an alternative name. ``absorb``: the other way round,
# INTO folds into LINEAGE, and when INTO's sequence determines more bases, LINEAGE's stored
# sequence is first completed from it (Rule 1 applied inside the database: the name with
# the literature survives, the more complete sequence is kept). ``remove``, ``rename``,
# ``keep`` as they read.
DECISIONS = ("merge", "absorb", "remove", "keep", "rename")

KINDS = ("duplicate", "contained", "ambiguous")

# IUPAC codes, for the one soft column (``ambiguity_code_matches``): how many of a pair's
# private positions are an ambiguity code in the undetermined member that is compatible
# with the base in the determined one. Informational; the classification ignores codes.
_IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}

_popcount = getattr(int, "bit_count", None) or (lambda x: bin(x).count("1"))

# Columns a curator fills in. They travel with the review sheet and come back through
# :func:`decisions_to_events`.
DECISION_COLUMNS = ("DECISION", "INTO", "CURATOR_NOTE")


class ReviewError(ValueError):
    """A decision sheet that cannot be turned into events; the message says why."""


# --------------------------------------------------------------------------- the screen

@dataclass(frozen=True)
class Pair:
    """One zero-distance pair. ``a`` is the member that would change (the partial one for
    a contained pair, the one with fewer records for a duplicate); ``b`` the other."""
    a: str
    b: str
    kind: str
    comparable: int          # positions determined in both (all agreeing)
    a_private: int           # positions determined in a but not in b
    b_private: int
    ambiguity_code_matches: int = 0


def _masks(sequence: str) -> Tuple[Dict[str, int], int]:
    """Per-base bitmasks over the frame, plus the union (the determined positions)."""
    masks = {b: 0 for b in "ACGT"}
    for index, char in enumerate(sequence.upper()):
        if char in masks:
            masks[char] |= 1 << index
    determined = masks["A"] | masks["C"] | masks["G"] | masks["T"]
    return masks, determined


def _code_matches(seq_a: str, seq_b: str) -> int:
    """Private positions where the undetermined side carries an IUPAC code that includes
    the other side's base (an R against an A, say). The ingest's checker counts these
    as matches; the classification here does not, so the sheet says how many there are."""
    count = 0
    for x, y in zip(seq_a.upper(), seq_b.upper()):
        if x in "ACGT" and y not in "ACGT" and y in _IUPAC and y != "N":
            count += x in _IUPAC[y]
        elif y in "ACGT" and x not in "ACGT" and x in _IUPAC and x != "N":
            count += y in _IUPAC[x]
    return count


def screen_pairs(sequences: Dict[str, str]) -> List[Pair]:
    """Every unordered pair of ``sequences`` (name -> frame-aligned sequence) with
    distance 0 and at least one position determined in both.

    The orientation of a contained pair puts the partial member in ``a``. Duplicates and
    ambiguous pairs come out with ``a < b`` alphabetically; the caller may reorder a
    duplicate once it knows the record counts. Pure Python (bitmasks over the frame):
    ~20 s for the whole store, and no dependency the curation package does not have.
    """
    names = sorted(sequences)
    coded = [_masks(sequences[n]) for n in names]
    pairs: List[Pair] = []
    for i in range(len(names)):
        masks_i, det_i = coded[i]
        for j in range(i + 1, len(names)):
            masks_j, det_j = coded[j]
            both = det_i & det_j
            if not both:
                continue
            agree = ((masks_i["A"] & masks_j["A"]) | (masks_i["C"] & masks_j["C"]) |
                     (masks_i["G"] & masks_j["G"]) | (masks_i["T"] & masks_j["T"]))
            if agree != both:
                continue
            i_private = _popcount(det_i & ~det_j)
            j_private = _popcount(det_j & ~det_i)
            if i_private == 0 and j_private == 0:
                kind, a, b, ap, bp = "duplicate", names[i], names[j], 0, 0
            elif i_private == 0:
                kind, a, b, ap, bp = "contained", names[i], names[j], 0, j_private
            elif j_private == 0:
                kind, a, b, ap, bp = "contained", names[j], names[i], 0, i_private
            else:
                kind, a, b, ap, bp = "ambiguous", names[i], names[j], i_private, j_private
            pairs.append(Pair(a, b, kind, _popcount(both), ap, bp,
                              _code_matches(sequences[a], sequences[b])))
    return pairs


# ------------------------------------------------------------------ facts per lineage

@dataclass
class Profile:
    name: str
    genus: str = ""
    accession: str = ""
    seq_class: str = ""          # the store's SEQ_LENGTH label (Full / Partial / a number)
    bases: int = 0               # determined positions
    host_records: int = 0
    vector_records: int = 0
    hosts: int = 0               # distinct host species
    references: int = 0          # distinct references across host and vector records
    first_year: str = ""         # earliest publication year among those references
    countries: int = 0
    morpho_links: int = 0
    alt_names: int = 0
    reference_names: List[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _name(value: Any) -> str:
    return normalize.lineage_name(_text(value)) or ""


def profiles(store: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Profile]:
    """One :class:`Profile` per stored lineage, from every table that cites it."""
    out: Dict[str, Profile] = {}
    for row in store.get("lineages", []):
        name = _name(row.get("LINEAGE_NAME"))
        if not name or name in out:
            continue                         # a second row under one name: keep the first
        seq = _text(row.get("SEQUENCE")).upper()
        out[name] = Profile(name=name, genus=_text(row.get("GENUS_NAME")),
                            accession=_text(row.get("GENBANK_ACC")),
                            seq_class=_text(row.get("SEQ_LENGTH")),
                            bases=sum(1 for ch in seq if ch in "ACGT"))
    year_of = {_text(r.get("REFERENCE_NAME")): _text(r.get("PUBLICATION_YEAR"))
               for r in store.get("references", [])}
    hosts: Dict[str, set] = {n: set() for n in out}
    refs: Dict[str, set] = {n: set() for n in out}
    countries: Dict[str, set] = {n: set() for n in out}
    for row in store.get("host_records", []):
        name = _name(row.get("LINEAGE_NAME"))
        if name not in out:
            continue
        out[name].host_records += 1
        host = _text(row.get("SPECIES_NAME")) or _text(row.get("GENUS_NAME"))
        if host:
            hosts[name].add(host)
        if _text(row.get("REFERENCE_NAME")):
            refs[name].add(_text(row.get("REFERENCE_NAME")))
        if _text(row.get("COUNTRY_NAME")):
            countries[name].add(_text(row.get("COUNTRY_NAME")))
    for row in store.get("vector_records", []):
        name = _name(row.get("LINEAGE_NAME"))
        if name not in out:
            continue
        out[name].vector_records += 1
        if _text(row.get("REFERENCE_NAME")):
            refs[name].add(_text(row.get("REFERENCE_NAME")))
    for row in store.get("morpho_species", []):
        name = _name(row.get("LINEAGE_NAME"))
        if name in out:
            out[name].morpho_links += 1
    for row in store.get("alt_names", []):
        name = _name(row.get("LINEAGE_NAME"))
        if name in out:
            out[name].alt_names += 1
    for name, p in out.items():
        p.hosts = len(hosts[name])
        p.references = len(refs[name])
        p.countries = len(countries[name])
        p.reference_names = sorted(refs[name])
        years = []
        for ref in refs[name]:
            digits = "".join(ch for ch in year_of.get(ref, "") if ch.isdigit())[:4]
            if len(digits) == 4:
                years.append(int(digits))
        p.first_year = str(min(years)) if years else ""
    return out


# ------------------------------------------------------------------------ the proposal

def _strength(p: Profile) -> Tuple[int, int]:
    """How established a name is: records first, then the earlier first year (an unknown
    year counts as latest)."""
    return (p.host_records + p.vector_records,
            -(int(p.first_year) if p.first_year else 9999))


def _stronger(a: Profile, b: Profile) -> bool:
    """Whether ``a`` should outlive ``b`` in a duplicate group: more records, then the
    earlier first year, then the alphabetically earlier name (usually the lower number,
    which is the name handed out first)."""
    return (_strength(a), b.name) > (_strength(b), a.name)


def duplicate_groups(pairs: Sequence[Pair], prof: Dict[str, Profile]) -> Dict[str, str]:
    """``{member: survivor}`` over the connected components of the duplicate pairs, so a
    sequence held under three names gets ONE proposed survivor rather than three pairwise
    proposals that contradict each other. The survivor is the member with the most
    records (then the earliest first year, then the alphabetically earlier name)."""
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p.kind == "duplicate":
            parent[find(p.a)] = find(p.b)
    members: Dict[str, List[str]] = {}
    for name in parent:
        members.setdefault(find(name), []).append(name)
    survivor: Dict[str, str] = {}
    for group in members.values():
        best = group[0]
        for name in group[1:]:
            if _stronger(prof[name], prof[best]):
                best = name
        for name in group:
            survivor[name] = best
    return survivor


def propose(pair: Pair, pa: Profile, pb: Profile, containers: Sequence[str],
            min_comparable: int = MIN_COMPARABLE,
            small_max: int = SMALL_LINEAGE_MAX_HOST_RECORDS,
            group: Sequence[str] = (), survivor: str = "") -> Tuple[str, str]:
    """``(proposal, target)`` for one pair. ``target`` is non-empty only for a merge.

    ``group`` and ``survivor`` describe a duplicate group (every name that carries this
    sequence, and the one proposed to keep it).
    """
    if pa.genus and pb.genus and pa.genus != pb.genus:
        return (f"decide: genus labels differ ({pa.genus} against {pb.genus}); settle the "
                f"label before any merge", "")
    if pair.comparable < min_comparable:
        return (f"none: only {pair.comparable} positions comparable "
                f"(floor {min_comparable})", "")
    if pair.kind == "duplicate":
        names = ", ".join(sorted(group)) if len(group) > 2 else "two names"
        return (f"merge into {survivor}: one sequence under {names}", survivor)
    if pair.kind == "contained":
        if len(containers) > 1:
            return (f"decide: compatible with {len(containers)} more complete lineages "
                    f"({', '.join(containers)}); the sequence does not say which", "")
        if pa.host_records + pa.vector_records <= small_max:
            return (f"merge into {pb.name}: adds {pa.host_records} host record(s) and is "
                    f"contained in it", pb.name)
        if pa.host_records + pa.vector_records > pb.host_records + pb.vector_records:
            return (f"decide: contained in {pb.name} but better established "
                    f"({pa.host_records} host records against {pb.host_records}); absorb "
                    f"would keep the name {pa.name} and complete its sequence from {pb.name}", "")
        return (f"decide: contained in {pb.name}, but {pa.host_records} host record(s) "
                f"would move", "")
    return (f"review by hand: each determines positions the other lacks "
            f"({pair.a_private} and {pair.b_private})", "")


# ------------------------------------------------------------------------ the review

REVIEW_COLUMNS = (
    "KIND", "LINEAGE", "OTHER", "PROPOSAL", "PROPOSED_INTO", *DECISION_COLUMNS,
    "COMPARABLE", "LINEAGE_PRIVATE", "OTHER_PRIVATE", "AMBIGUITY_CODE_MATCHES",
    "N_CONTAINERS", "CONTAINERS", "ALREADY_ALT_NAME", "PRIOR_DECISION",
    "LINEAGE_GENUS", "LINEAGE_ACCESSION", "LINEAGE_SEQ_CLASS", "LINEAGE_BASES",
    "LINEAGE_HOST_RECORDS", "LINEAGE_VECTOR_RECORDS", "LINEAGE_HOSTS",
    "LINEAGE_REFERENCES", "LINEAGE_FIRST_YEAR", "LINEAGE_COUNTRIES",
    "LINEAGE_MORPHO_LINKS", "LINEAGE_ALT_NAMES",
    "OTHER_GENUS", "OTHER_ACCESSION", "OTHER_SEQ_CLASS", "OTHER_BASES",
    "OTHER_HOST_RECORDS", "OTHER_VECTOR_RECORDS", "OTHER_HOSTS",
    "OTHER_REFERENCES", "OTHER_FIRST_YEAR", "OTHER_COUNTRIES",
    "OTHER_MORPHO_LINKS", "OTHER_ALT_NAMES",
    "LINEAGE_REFERENCE_NAMES", "OTHER_REFERENCE_NAMES",
)


def _side(prefix: str, p: Profile) -> Dict[str, Any]:
    return {
        f"{prefix}_GENUS": p.genus, f"{prefix}_ACCESSION": p.accession,
        f"{prefix}_SEQ_CLASS": p.seq_class, f"{prefix}_BASES": p.bases,
        f"{prefix}_HOST_RECORDS": p.host_records, f"{prefix}_VECTOR_RECORDS": p.vector_records,
        f"{prefix}_HOSTS": p.hosts, f"{prefix}_REFERENCES": p.references,
        f"{prefix}_FIRST_YEAR": p.first_year, f"{prefix}_COUNTRIES": p.countries,
        f"{prefix}_MORPHO_LINKS": p.morpho_links, f"{prefix}_ALT_NAMES": p.alt_names,
        f"{prefix}_REFERENCE_NAMES": "; ".join(p.reference_names),
    }


def build_review(store: Dict[str, List[Dict[str, Any]]],
                 min_comparable: int = MIN_COMPARABLE,
                 small_max: int = SMALL_LINEAGE_MAX_HOST_RECORDS,
                 prior: Optional[Dict[str, str]] = None
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The review table (one row per pair, columns :data:`REVIEW_COLUMNS`) and a summary.

    ``prior`` maps a lineage name to a decision already taken (from an earlier filled-in
    sheet), shown in PRIOR_DECISION so a second pass does not re-ask.
    """
    prior = prior or {}
    sequences: Dict[str, str] = {}
    twice: List[str] = []
    for row in store.get("lineages", []):
        name = _name(row.get("LINEAGE_NAME"))
        if not name:
            continue
        if name in sequences:
            twice.append(name)              # the store's TUPHI01 case: not a pair
            continue
        sequences[name] = _text(row.get("SEQUENCE")).upper()
    prof = profiles(store)
    pairs = screen_pairs(sequences)

    alt_pairs = set()
    for row in store.get("alt_names", []):
        alt_pairs.add((_name(row.get("LINEAGE_NAME")), _name(row.get("ALT_NAME"))))

    containers: Dict[str, List[str]] = {}
    for p in pairs:
        if p.kind == "contained":
            containers.setdefault(p.a, []).append(p.b)
    for name in containers:
        containers[name].sort()

    survivor = duplicate_groups(pairs, prof)
    groups: Dict[str, List[str]] = {}
    for name, best in survivor.items():
        groups.setdefault(best, []).append(name)

    rows: List[Dict[str, Any]] = []
    for p in pairs:
        pa, pb = prof[p.a], prof[p.b]
        group: List[str] = []
        best = ""
        if p.kind == "duplicate":
            # The weaker name sits in LINEAGE, so the survivor (the strongest of its group)
            # never does: nothing is decided about it.
            best = survivor[p.a]
            group = sorted(groups[best])
            if _stronger(pa, pb):
                pa, pb = pb, pa
            p = Pair(pa.name, pb.name, p.kind, p.comparable, 0, 0, p.ambiguity_code_matches)
        mine = containers.get(p.a, []) if p.kind == "contained" else []
        proposal, target = propose(p, pa, pb, mine, min_comparable, small_max, group, best)
        row: Dict[str, Any] = {
            "KIND": p.kind, "LINEAGE": p.a, "OTHER": p.b,
            "PROPOSAL": proposal, "PROPOSED_INTO": target,
            "DECISION": "", "INTO": target, "CURATOR_NOTE": "",
            "COMPARABLE": p.comparable, "LINEAGE_PRIVATE": p.a_private,
            "OTHER_PRIVATE": p.b_private, "AMBIGUITY_CODE_MATCHES": p.ambiguity_code_matches,
            "N_CONTAINERS": len(mine), "CONTAINERS": ", ".join(mine),
            "ALREADY_ALT_NAME": "yes" if ((p.a, p.b) in alt_pairs or (p.b, p.a) in alt_pairs) else "",
            "PRIOR_DECISION": prior.get(p.a, ""),
        }
        row.update(_side("LINEAGE", pa))
        row.update(_side("OTHER", pb))
        rows.append(row)

    order = {k: i for i, k in enumerate(KINDS)}
    rows.sort(key=lambda r: (order[r["KIND"]], r["LINEAGE"], r["OTHER"]))

    by_kind = {k: sum(1 for r in rows if r["KIND"] == k) for k in KINDS}
    proposed = sum(1 for r in rows if r["PROPOSED_INTO"])
    lineages_in_pairs = {r["LINEAGE"] for r in rows} | {r["OTHER"] for r in rows}
    cross = sum(1 for r in rows if r["LINEAGE_GENUS"] and r["OTHER_GENUS"]
                and r["LINEAGE_GENUS"] != r["OTHER_GENUS"])
    summary = {
        "n_lineages": len(sequences),
        "n_pairs": len(rows),
        "by_kind": by_kind,
        "n_lineages_in_pairs": len(lineages_in_pairs),
        "n_contained_lineages": len(containers),
        "n_contained_unique": sum(1 for v in containers.values() if len(v) == 1),
        "n_proposed_merges": proposed,
        "n_below_floor": sum(1 for r in rows if r["COMPARABLE"] < min_comparable),
        "n_cross_genus": cross,
        "n_already_alt_name": sum(1 for r in rows if r["ALREADY_ALT_NAME"]),
        "names_stored_twice": sorted(set(twice)),
        "min_comparable": min_comparable,
        "small_max": small_max,
    }
    return rows, summary


# ------------------------------------------------------------- decisions -> events

def decisions_to_events(rows: Iterable[Dict[str, Any]],
                        store: Dict[str, List[Dict[str, Any]]],
                        date: str, release: str, source: str
                        ) -> Tuple[List[lineage_history.Event], List[str]]:
    """Turn the filled-in review rows into history events, in an order that applies.

    Rules a sheet must satisfy (a :class:`ReviewError` names the first breach):

    * DECISION is one of :data:`DECISIONS` or blank; ``keep`` makes no event.
    * One lineage gets at most one non-keep decision; the same decision written on two
      rows of the same lineage (a partial with two containers, say) is fine when it names
      the same INTO.
    * ``merge``, ``absorb`` and ``rename`` need INTO. A merge or absorb target must be a
      stored lineage; a rename target must not be one. ``absorb`` is recorded as INTO
      merged into LINEAGE, preceded by a ``sequence_updated`` on LINEAGE when INTO's
      sequence determines more bases.
    * A merge whose target is itself merged in this sheet follows the chain to the
      surviving name (and the reason says so). A chain that ends in a removal, or a
      cycle, is refused.
    * ``remove`` and ``rename`` need CURATOR_NOTE, which becomes the public REASON.
      The merge reason is composed from the sequence facts.

    Returns the events (merges and removals of a chain's tail first) and one note per
    event, in the same order.
    """
    stored = {_name(r.get("LINEAGE_NAME")) for r in store.get("lineages", [])}
    bases_of = {_name(r.get("LINEAGE_NAME")): sum(1 for ch in _text(r.get("SEQUENCE")).upper() if ch in "ACGT")
                for r in store.get("lineages", [])}
    sequence_of = {_name(r.get("LINEAGE_NAME")): _text(r.get("SEQUENCE")).upper()
                   for r in store.get("lineages", [])}
    chosen: Dict[str, Dict[str, Any]] = {}
    completions: Dict[str, Dict[str, Any]] = {}     # absorb: name -> the row whose INTO completes it

    def choose(name: str, decision: str, into: str, row: Dict[str, Any], how: str) -> None:
        earlier = chosen.get(name)
        if earlier and (earlier["decision"] != decision or earlier["into"] != into):
            raise ReviewError(f"{name}: two different decisions on the sheet "
                              f"({earlier['how']} and {how})")
        chosen[name] = {"decision": decision, "into": into, "row": row, "how": how}

    for row in rows:
        decision = _text(row.get("DECISION")).lower()
        name = _name(row.get("LINEAGE"))
        if not decision:
            continue
        if decision not in DECISIONS:
            raise ReviewError(f"{name}: DECISION {decision!r} is not one of {', '.join(DECISIONS)}")
        if decision == "keep":
            continue
        into = _name(row.get("INTO"))
        how = f"{decision} {into}".strip()
        if name not in stored:
            raise ReviewError(f"{name}: not a lineage in the store")
        if decision in ("merge", "absorb", "rename") and not into:
            raise ReviewError(f"{name}: {decision} needs INTO")
        if decision in ("merge", "absorb") and into not in stored:
            raise ReviewError(f"{name}: {decision} target {into} is not a lineage in the store")
        if decision == "rename" and into in stored:
            raise ReviewError(f"{name}: rename target {into} is already a lineage; use merge")
        if decision in ("remove", "rename") and not _text(row.get("CURATOR_NOTE")):
            raise ReviewError(f"{name}: {decision} needs CURATOR_NOTE (it becomes the public reason)")
        if decision in ("merge", "absorb") and into == name:
            raise ReviewError(f"{name}: cannot {decision} a lineage into itself")
        if decision == "absorb":
            # INTO folds into LINEAGE; LINEAGE's sequence is completed from INTO's first
            # when that determines more bases.
            choose(into, "merge", name, row, f"absorbed by {name}")
            if bases_of.get(into, 0) > bases_of.get(name, 0):
                if name in completions and completions[name]["from"] != into:
                    raise ReviewError(f"{name}: absorbs both {completions[name]['from']} and "
                                      f"{into}, whose sequences would both complete it; "
                                      f"absorb one and merge the other into it")
                completions[name] = {"from": into, "row": row}
            continue
        choose(name, decision, into, row, how)

    for name in completions:
        if name in chosen:
            raise ReviewError(f"{name}: absorbs {completions[name]['from']} but is itself "
                              f"{chosen[name]['how']}")

    # Follow merge chains to the surviving name.
    def survivor(name: str) -> Tuple[str, List[str]]:
        trail: List[str] = []
        here = name
        seen = set()
        while here in chosen and chosen[here]["decision"] in ("merge", "rename"):
            if here in seen:
                raise ReviewError(f"{name}: the merge chain loops ({' -> '.join(trail)})")
            seen.add(here)
            trail.append(here)
            here = chosen[here]["into"]
        if here in chosen and chosen[here]["decision"] == "remove":
            raise ReviewError(f"{name}: merges into {here}, which the sheet removes")
        return here, trail

    events: List[lineage_history.Event] = []
    notes: List[str] = []
    # Removals first (they depend on nothing), then the sequence completions an absorb
    # asks for (the absorbed lineage still exists, so its sequence is at hand), then
    # merges and renames, each pointing at its chain's survivor.
    for name, d in sorted(chosen.items()):
        if d["decision"] == "remove":
            events.append(lineage_history.Event(
                event="removed", lineage=name, date=date, release=release, source=source,
                reason=_text(d["row"].get("CURATOR_NOTE"))))
            notes.append(f"{name}: removed")
    for name in sorted(completions):
        origin = completions[name]["from"]
        row = completions[name]["row"]
        events.append(lineage_history.Event(
            event="sequence_updated", lineage=name, new_sequence=sequence_of[origin],
            date=date, release=release, source=source,
            reason=(f"Completed from the sequence of {origin}, identical over all "
                    f"{row.get('COMPARABLE')} positions both cover and determining "
                    f"{bases_of[origin] - bases_of[name]} more base(s) "
                    f"({bases_of[name]} -> {bases_of[origin]}); {origin} is merged into "
                    f"{name} in the same edition")))
        notes.append(f"{name}: sequence completed from {origin}")
    depth = {}
    for name, d in chosen.items():
        if d["decision"] in ("merge", "rename"):
            depth[name] = len(survivor(name)[1])
    for name in sorted(depth, key=lambda n: (-depth[n], n)):
        d = chosen[name]
        row = d["row"]
        if d["decision"] == "rename":
            events.append(lineage_history.Event(
                event="renamed", lineage=name, new_name=d["into"], date=date,
                release=release, source=source, reason=_text(row.get("CURATOR_NOTE"))))
            notes.append(f"{name}: renamed to {d['into']}")
            continue
        final, trail = survivor(name)
        target = d["into"]
        kind = _text(row.get("KIND"))
        comparable = row.get("COMPARABLE")
        if d["how"].startswith("absorbed by"):
            # name is the row's OTHER; target is the row's LINEAGE, which keeps its name.
            reason = (f"Identical to {target} over all {comparable} positions both cover; "
                      f"the name {target} is kept"
                      + (f" and its sequence completed from {name}"
                         if target in completions and completions[target]["from"] == name else ""))
        elif kind == "duplicate":
            reason = (f"Identical to {target} over all {comparable} positions both cover; "
                      f"the two names carried one sequence")
        elif kind == "contained":
            reason = (f"Identical to {target} over all {comparable} positions both cover; "
                      f"{target} is the more complete sequence "
                      f"({row.get('OTHER_BASES')} determined bases against "
                      f"{row.get('LINEAGE_BASES')})")
        else:
            reason = (f"Identical to {target} over all {comparable} positions both cover; "
                      f"each determined positions the other did not, and the curators "
                      f"judged them one lineage")
        if final != target:
            reason += f"; {target} was itself merged into {final} in the same edition"
        if _text(row.get("CURATOR_NOTE")):
            reason += f". {_text(row.get('CURATOR_NOTE')).rstrip('.')}"
        events.append(lineage_history.Event(
            event="merged_into", lineage=name, new_name=final, date=date,
            release=release, source=source, reason=reason))
        notes.append(f"{name}: merged into {final}"
                     + (f" (through {target})" if final != target else ""))
    return events, notes
