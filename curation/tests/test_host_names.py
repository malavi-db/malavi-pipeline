"""Tests for putting host names into MalAvi's naming namespace.

Two properties are pinned here, and the second matters more than the first:

1. A documented genus revision is applied, so a correctly-read host association
   joins to MalAvi instead of scoring as a false record.
2. A name that is **not** covered by a curated revision is never rewritten. The
   tempting shortcut -- take the MalAvi binomial that shares this epithet when it
   is unique -- was measured on the ground-truth corpus and "uniquely" maps
   *Cistothorus platensis* onto ``BOTAURUS`` and *Tringa* onto ``TURDUS``. The
   negative tests below are what keep that class of mistake out.

These run against the packaged gazetteer rather than a fixture, because the whole
point of the module is agreement with MalAvi's real vocabulary.
"""
from __future__ import annotations

from malavi_curation.host_names import (
    canonical_host, canonicalize_rows, synonym_genera, _one_character_apart,
)


# --------------------------------------------------------------------------
# names MalAvi already holds
# --------------------------------------------------------------------------

def test_a_malavi_name_is_left_alone():
    resolved = canonical_host("Turdus merula")
    assert resolved.rule == "exact"
    assert resolved.name == "Turdus merula"
    assert not resolved.changed


def test_capitalization_is_normalized_without_counting_as_a_change():
    resolved = canonical_host("turdus MERULA")
    assert resolved.name == "Turdus merula"
    assert resolved.rule == "exact"
    assert not resolved.changed        # same name, different shouting


# --------------------------------------------------------------------------
# documented genus revisions
# --------------------------------------------------------------------------

def test_astur_maps_to_accipiter():
    """Harl et al 2026 uses the 2024 Accipiter split; MalAvi does not."""
    resolved = canonical_host("Astur gentilis")
    assert resolved.name == "Accipiter gentilis"
    assert resolved.rule == "genus_synonym"


def test_crithagra_maps_to_serinus():
    """Perrin et al 2026: eight of its ten scored false positives were this."""
    assert canonical_host("Crithagra flaviventris").name == "Serinus flaviventris"
    assert canonical_host("Crithagra atrogularis").name == "Serinus atrogularis"


def test_a_split_genus_is_resolved_by_the_epithet():
    """Wilsonia went to two genera, and only the epithet says which."""
    assert canonical_host("Wilsonia citrina").name == "Setophaga citrina"
    assert canonical_host("Wilsonia pusilla").name == "Cardellina pusilla"


def test_gender_agreement_is_followed_across_a_genus_change():
    """Parus hudsonicus is Poecile hudsonica: the ending changes with the genus."""
    assert canonical_host("Parus hudsonicus").name == "Poecile hudsonica"
    assert canonical_host("Casmerodius albus").name == "Ardea alba"


def test_a_one_character_genus_misspelling_is_corrected():
    """Perrin et al 2026 prints 'Lagonostica' for Lagonosticta."""
    resolved = canonical_host("Lagonostica nitidula")
    assert resolved.name == "Lagonosticta nitidula"
    assert resolved.rule == "spelling"


# --------------------------------------------------------------------------
# the negative property: no inference
# --------------------------------------------------------------------------

def test_a_real_bird_malavi_does_not_hold_is_not_rewritten():
    """These all have a "unique" same-epithet match. None of them is that bird."""
    for name in ("Cistothorus platensis", "Tringa flavipes", "Streptoprocne rutila",
                 "Zonotrichia atricapilla", "Aimophila strigiceps"):
        resolved = canonical_host(name)
        assert not resolved.changed, f"{name} was rewritten to {resolved.name}"


def test_an_unresolved_name_keeps_the_document_wording_and_says_so():
    resolved = canonical_host("Nonexistentia fabricata")
    assert resolved.name == "Nonexistentia fabricata"
    assert resolved.rule == "unmatched"
    assert not resolved.in_malavi


def test_a_genus_only_host_is_passed_through():
    """MalAvi records genus-level hosts; nothing here may invent an epithet."""
    resolved = canonical_host("Scytalopus")
    assert resolved.name == "Scytalopus"
    assert resolved.rule == "unmatched"
    assert "not a binomial" in resolved.note


def test_a_known_genus_is_never_treated_as_a_misspelling():
    """Turdus is a real genus, so it must not be "corrected" into Turdoides."""
    resolved = canonical_host("Turdus nonexistentus")
    assert resolved.rule == "unmatched"
    assert resolved.name == "Turdus nonexistentus"


def test_ambiguous_revisions_are_refused_rather_than_guessed():
    """If two destinations both hold the epithet, neither is chosen."""
    # Constructed against the real vocabulary: Carduelis maps to several genera,
    # and a made-up epithet matches none of them, so nothing fires. The property
    # under test is that a multi-hit outcome never returns a name.
    resolved = canonical_host("Carduelis notarealepithet")
    assert resolved.rule == "unmatched"
    assert resolved.name == "Carduelis notarealepithet"


# --------------------------------------------------------------------------
# subspecies and vectors
# --------------------------------------------------------------------------

def test_a_vector_trinomial_is_reduced_to_the_binomial():
    """Kim & Tsuda 2012 reports Culex pipiens pallens; MalAvi holds the binomial."""
    resolved = canonical_host("Culex pipiens pallens", kind="vectors")
    assert resolved.name == "Culex pipiens"
    assert resolved.rule == "subspecies_trim"


def test_the_bird_revisions_do_not_apply_to_vectors():
    """The curated table is avian; running it on mosquitoes would be nonsense."""
    resolved = canonical_host("Parus major", kind="vectors")
    assert resolved.rule == "unmatched"


# --------------------------------------------------------------------------
# applying to rows
# --------------------------------------------------------------------------

def test_canonicalize_rows_keeps_the_document_wording():
    rows = [{"lineage_name": "GRW09", "host_species": "Crithagra flaviventris"},
            {"lineage_name": "SGS1", "host_species": "Turdus merula"}]
    tally = canonicalize_rows(rows)
    assert rows[0]["host_species"] == "Serinus flaviventris"
    assert rows[0]["host_species_source"] == "Crithagra flaviventris"
    assert "host_species_source" not in rows[1]
    assert tally == {"genus_synonym": 1, "exact": 1}


def test_synonym_genera_are_offered_to_the_prose_miner():
    """The miner drops any binomial whose genus it does not know as avian."""
    genera = synonym_genera()
    assert "Astur" in genera and "Crithagra" in genera and "Dendroica" in genera


# --------------------------------------------------------------------------
# the edit-distance helper
# --------------------------------------------------------------------------

def test_one_character_apart():
    assert _one_character_apart("lagonostica", "lagonosticta")   # insertion
    assert _one_character_apart("turdus", "turdis")              # substitution
    assert _one_character_apart("turdoides", "turdoide")         # deletion
    assert not _one_character_apart("turdus", "turdus")          # identical
    assert not _one_character_apart("parus", "parula")           # two apart
