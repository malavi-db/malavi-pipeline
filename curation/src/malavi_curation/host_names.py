"""Put an extracted host name into MalAvi's own naming namespace.

MalAvi is internally consistent: a host is filed under one name, and joining a
new record to the database means using that name. Papers do not oblige. Avian
genus limits have been revised repeatedly, so the same bird is *Astur gentilis*
in a 2026 paper and ``ACCIPITER GENTILIS`` in MalAvi, *Crithagra flaviventris* in
the field and ``SERINUS FLAVIVENTRIS`` in the database, *Dendroica striata* in a
2005 dataset and ``SETOPHAGA STRIATA`` since the 2011 revision.

Measured on the ten-paper ground-truth corpus (2026-07-29), this one mismatch
accounted for roughly 165 scored false positives -- host associations that were
read correctly off the page and then failed to join purely on the name. It is not
a benchmark artifact: a curator receiving those submissions has to do the same
translation by hand on every row.

**The authority is a curated table, never an inference.** The tempting shortcut is
to look for a MalAvi binomial sharing the epithet and take it when it is unique.
That was measured on the corpus's 61 unmatched names and is unsafe: it "uniquely"
maps *Cistothorus platensis* onto ``BOTAURUS``, *Tringa* onto ``TURDUS`` and
*Spinus* onto ``SPHENISCUS``, because epithets collide across the whole class once
gendered endings are folded together. Every genus mapping below is therefore a
documented revision, and the epithet is used only to *choose* among the genera a
revision splits into -- never to propose a mapping of its own.

A name that no rule resolves is left exactly as the paper wrote it and flagged.
Under-resolving costs a curator one lookup; mis-resolving files a record against
the wrong bird.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

# Genus in a paper -> the MalAvi genus/genera it may correspond to.
#
# Every entry is a published genus revision, and every one was observed in the
# ground-truth corpus (the corpus is what makes the list non-speculative: these
# are names real papers actually used against this database). Where a revision
# split one genus into several, all destinations are listed and the epithet
# decides between them -- `Wilsonia citrina` is `Setophaga citrina` while
# `Wilsonia pusilla` is `Cardellina pusilla`, and only the epithet knows which.
#
# Direction matters and is not always "old name -> new name": the target is
# whatever MalAvi holds. MalAvi files these birds under the older *Diglossopis*
# and *Piculus*, so the mapping points that way.
_GENUS_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    # Accipitridae: Accipiter split, 2024. Harl et al 2026 uses Astur.
    "astur": ("Accipiter",),
    # Parulidae: the 2011 AOU revision of the wood-warblers.
    "dendroica": ("Setophaga", "Parula"),
    "wilsonia": ("Cardellina", "Setophaga"),
    "vermivora": ("Leiothlypis", "Vermivora"),
    "parula": ("Setophaga", "Parula"),
    "basileuterus": ("Myiothlypis", "Basileuterus"),
    "myiothlypis": ("Basileuterus", "Myiothlypis"),
    # Fringillidae: African canaries out of Serinus; redpolls and siskins out of
    # Carduelis (the epithet separates Acanthis from Spinus from Chloris).
    "crithagra": ("Serinus",),
    "carduelis": ("Acanthis", "Spinus", "Chloris", "Carduelis"),
    "acanthis": ("Carduelis", "Acanthis"),
    # Paridae: Parus broken up into Poecile / Periparus / Cyanistes.
    "parus": ("Poecile", "Periparus", "Cyanistes", "Parus"),
    "poecile": ("Parus", "Poecile"),
    # Thraupidae / Emberizidae rearrangements.
    "arremon": ("Buarremon", "Arremon"),
    "buarremon": ("Arremon", "Buarremon"),
    "phrygilus": ("Geospizopsis", "Phrygilus"),
    "geospizopsis": ("Phrygilus", "Geospizopsis"),
    "diglossa": ("Diglossopis", "Diglossa"),
    "diglossopis": ("Diglossa", "Diglossopis"),
    "tiaris": ("Asemospiza", "Tiaris"),
    "oryzoborus": ("Sporophila", "Oryzoborus"),
    "aimophila": ("Rhynchospiza", "Peucaea", "Aimophila"),
    # Furnariidae / Thamnophilidae / Tyrannidae.
    "simoxenops": ("Syndactyla",),
    "asthenes": ("Schizoeaca", "Asthenes"),
    "sakesphorus": ("Thamnophilus", "Sakesphorus"),
    "empidonomus": ("Griseotyrannus", "Empidonomus"),
    "pitangus": ("Philohydor", "Pitangus"),
    "ochthoeca": ("Silvicultrix", "Ochthoeca"),
    "chloropipo": ("Xenopipo",),
    # Troglodytidae: Thryothorus dismantled into Pheugopedius / Cantorchilus.
    "thryothorus": ("Pheugopedius", "Cantorchilus", "Thryothorus"),
    "pheugopedius": ("Thryothorus", "Pheugopedius"),
    # Picidae, Strigidae, Icteridae, Ardeidae, Phasianidae.
    "colaptes": ("Piculus", "Colaptes"),
    "piculus": ("Colaptes", "Piculus"),
    "otus": ("Megascops", "Otus"),
    "cacicus": ("Amblycercus", "Cacicus"),
    "agelaioides": ("Molothrus", "Agelaioides"),
    "casmerodius": ("Ardea",),
    "dendragapus": ("Falcipennis", "Dendragapus"),
    # Trochilidae.
    "basilinna": ("Hylocharis", "Basilinna"),
    "hylocharis": ("Basilinna", "Hylocharis"),
}

# Latin gendered endings. A genus change can change the epithet's gender
# agreement -- Parus hudsonicus becomes Poecile hudsonica, Casmerodius albus
# becomes Ardea alba -- so epithets are compared on the stem left after these.
#
# This is applied ONLY inside a curated genus mapping. Folding endings together
# across the whole database is what produced the false "unique" matches described
# in the module docstring.
_GENDERED_ENDING = re.compile(r"(us|um|is|e|a|i|os|on)$")


def _stem(epithet: str) -> str:
    """Epithet with its gendered ending removed (``albus`` and ``alba`` -> ``alb``)."""
    return _GENDERED_ENDING.sub("", epithet.lower())


@dataclass
class HostName:
    """One host name, as the paper wrote it and as MalAvi files it."""

    name: str                      # the name to use downstream
    source_name: str               # exactly what the document said
    rule: str                      # which rule produced ``name`` (see RULES)
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.name.lower() != self.source_name.lower()

    @property
    def in_malavi(self) -> bool:
        """Did this end up as a name MalAvi actually holds?"""
        return self.rule != "unmatched"


# Every rule, in the order they are tried, and what a curator should read into it.
RULES = {
    "exact": "the name is already a MalAvi host name",
    "subspecies_trim": "a trinomial reduced to the binomial MalAvi holds",
    "genus_synonym": "a documented genus revision, resolved against MalAvi's own names",
    "spelling": "a one-character genus misspelling with a unique MalAvi correction",
    "unmatched": "no rule resolved it; the document's name was kept unchanged",
}


@lru_cache(maxsize=1)
def synonym_genera() -> frozenset:
    """Genus names that are not MalAvi's but are known to map onto it.

    The prose miner keeps a binomial only when its genus is in MalAvi's
    vocabulary, which means a paper using current taxonomy has its hosts thrown
    away before this module ever sees them -- *Astur gentilis* was invisible in
    Harl et al 2026 for exactly that reason. Adding these lets such a name be
    recognized as a bird and then translated.
    """
    return frozenset(genus.capitalize() for genus in _GENUS_SYNONYMS)


def _split(name: str) -> List[str]:
    """Tokenize a host name, dropping qualifiers that are not part of it."""
    cleaned = re.sub(r"[_]+", " ", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return [t for t in cleaned.split(" ") if t]


def _titled(genus: str, epithet: str) -> str:
    return f"{genus.capitalize()} {epithet.lower()}"


# The two vocabularies this works over. MalAvi keeps a vector table of its own
# (lineage x vector species x country), and a vector name needs the same
# translation -- Kim & Tsuda 2012 reports *Culex pipiens pallens* where MalAvi
# holds *Culex pipiens*.
_VOCABULARIES = {"hosts": "binomials", "vectors": "vector_binomials"}


@lru_cache(maxsize=len(_VOCABULARIES))
def _malavi_index(kind: str = "hosts") -> Tuple[frozenset, Dict[str, Dict[str, str]]]:
    """MalAvi names for one vocabulary, plus a genus -> {epithet stem: name} index.

    The stem index is what lets a curated genus mapping be checked against the
    database rather than trusted: a mapping only fires when the destination genus
    really does hold a species with this epithet.
    """
    from .hosts_geography import load_gazetteer

    key = _VOCABULARIES.get(kind, "binomials")
    binomials = [str(b) for b in load_gazetteer().get(key, [])]
    lower = frozenset(b.lower() for b in binomials)
    by_genus: Dict[str, Dict[str, str]] = {}
    for binomial in binomials:
        parts = binomial.split()
        if len(parts) < 2:
            continue
        genus, epithet = parts[0], parts[1]
        by_genus.setdefault(genus.lower(), {}).setdefault(_stem(epithet), binomial)
    return lower, by_genus


def _one_character_apart(a: str, b: str) -> bool:
    """Do these two strings differ by exactly one insertion, deletion or change?

    Written out rather than pulled from difflib so the rule is exact and cheap:
    the caller uses it to repair a genus typo, and "close enough" is not a
    standard a database name can be changed on.
    """
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    for cut in range(len(longer)):
        if longer[:cut] + longer[cut + 1:] == shorter:
            return True
    return False


def canonical_host(name: str, kind: str = "hosts") -> HostName:
    """Map one name onto MalAvi's namespace, or leave it alone and say so.

    ``kind`` selects the vocabulary: ``"hosts"`` (avian) or ``"vectors"``
    (arthropod). The genus-revision table only covers birds; a vector name is
    still worth putting through the trinomial and spelling rules.
    """
    source = (name or "").strip()
    tokens = _split(source)
    known, by_genus = _malavi_index(kind)

    # Not a binomial at all ("Scytalopus", "Sclerurus sp."): nothing to map, and
    # nothing to invent. A genus-only host is a real thing MalAvi records, so it
    # is passed through for the curator rather than dropped here.
    if len(tokens) < 2:
        return HostName(name=source, source_name=source, rule="unmatched",
                        note="not a binomial")

    genus, epithet = tokens[0], tokens[1]

    # 1. Already MalAvi's name.
    if len(tokens) == 2 and f"{genus} {epithet}".lower() in known:
        return HostName(name=_titled(genus, epithet), source_name=source, rule="exact")

    # 2. A trinomial whose binomial MalAvi holds. MalAvi keeps subspecies in a
    #    column of its own, so the record's host name is the binomial.
    if len(tokens) > 2 and f"{genus} {epithet}".lower() in known:
        return HostName(name=_titled(genus, epithet), source_name=source,
                        rule="subspecies_trim",
                        note=f"subspecies '{' '.join(tokens[2:])}' dropped from the host name")

    # 3. A curated genus revision. The destination must actually hold a bird with
    #    this epithet, and exactly one destination may.
    stem = _stem(epithet)
    destinations = _GENUS_SYNONYMS.get(genus.lower(), ()) if kind == "hosts" else ()
    hits = []
    for destination in destinations:
        candidate = by_genus.get(destination.lower(), {}).get(stem)
        if candidate and candidate.lower() != f"{genus} {epithet}".lower():
            hits.append(candidate)
    unique = sorted(set(hits))
    if len(unique) == 1:
        return HostName(name=unique[0], source_name=source, rule="genus_synonym",
                        note=f"{genus} is a synonym of {unique[0].split()[0]} in MalAvi")
    if len(unique) > 1:
        return HostName(name=source, source_name=source, rule="unmatched",
                        note="genus revision is ambiguous here: " + ", ".join(unique))

    # 4. A one-character genus misspelling ("Lagonostica nitidula"). Only fires
    #    when the document's genus is *not* itself a known avian genus, so a real
    #    name is never "corrected" into a different real name.
    if genus.lower() not in by_genus:
        corrections = sorted({
            candidate for known_genus, epithets in by_genus.items()
            if _one_character_apart(genus.lower(), known_genus)
            for candidate in [epithets.get(stem)] if candidate
        })
        if len(corrections) == 1:
            return HostName(name=corrections[0], source_name=source, rule="spelling",
                            note=f"'{genus}' appears to be a misspelling of "
                                 f"'{corrections[0].split()[0]}'")

    return HostName(name=source, source_name=source, rule="unmatched",
                    note="not a MalAvi host name")


def canonicalize_rows(rows: Sequence[Dict[str, Optional[str]]],
                      field_name: str = "host_species",
                      kind: str = "hosts") -> Dict[str, int]:
    """Rewrite ``field_name`` on each row into MalAvi's namespace. Mutates ``rows``.

    The document's own wording is preserved in ``<field>_source`` whenever the
    name changes, for the same reason lineage resolution preserves the printed
    lineage code: a curator has to be able to see what the paper actually said.

    Returns a count per rule, for the run report.
    """
    tally: Dict[str, int] = {}
    for row in rows:
        value = row.get(field_name)
        if not value:
            continue
        resolved = canonical_host(str(value), kind=kind)
        tally[resolved.rule] = tally.get(resolved.rule, 0) + 1
        if resolved.changed:
            row[f"{field_name}_source"] = resolved.source_name
            row[field_name] = resolved.name
            row[f"{field_name}_rule"] = resolved.rule
            row[f"{field_name}_note"] = resolved.note
    return tally
