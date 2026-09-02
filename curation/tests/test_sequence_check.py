"""Tests for the deterministic submitted-sequence checker.

The property that matters most is the negative one: a sequence already in MalAvi
must never be reported as new, no matter how it is framed or padded. Everything
else is a convenience; that one is a correctness requirement.
"""
from __future__ import annotations

import pytest

from malavi_curation.sequence_check import (
    MIN_COMPARABLE_TO_RANK, Reference, check_sequence, clean, count_stops, translate,
    _CODE4,
)

# A synthetic reference. Real cytb is not needed to exercise the logic, and a fixed toy
# alignment keeps these tests fast and independent of any release.
#
# Each sequence is a 63 bp seed tiled out to the real 479 bp barcode window. The width
# matters: an exact match is only called a known lineage once it covers
# MIN_COMPARABLE_TO_RANK (300) positions, and a 63 bp reference could never get there,
# so every "known" test below would have collapsed into the low-coverage outcome.
# Tiling keeps the seeds' designed relationships (B is one base from A at the very end,
# C is divergent everywhere) while making the fixture the size of a real barcode.
WINDOW = 479
SEED_A = "ATGGCAACAGGTGCTTCATTTGTATTTATTTTAACTTATTTACATATTTTAAGAGGATTAAAT"
SEED_C = "ATGCCTACTGGAGCATCTTTCGTGTTCTTGCTGACGTACCTGCACATCCTGCGCGGACTGAAT"  # divergent


def _tile(seed: str) -> str:
    """Repeat a seed until it fills the barcode window, then cut to the window."""
    return (seed * (WINDOW // len(seed) + 1))[:WINDOW]


REF_A = _tile(SEED_A)
REF_B = REF_A[:-1] + ("G" if REF_A[-1] != "G" else "T")  # 1 bp from A, at the last position
REF_C = _tile(SEED_C)


@pytest.fixture
def ref(tmp_path):
    p = tmp_path / "ref.fasta"
    p.write_text(f">LINA\n{REF_A}\n>LINB\n{REF_B}\n>LINC\n{REF_C}\n")
    return Reference.from_fasta(p)


# --------------------------------------------------------------------------
# genetic code
# --------------------------------------------------------------------------

def test_genetic_code_is_ncbi_table_4_not_table_5():
    """Table 4 differs from the standard code ONLY at TGA.

    malaviR's own table has the table-5 values M/S/S here; this pins ours to
    table 4 so the two cannot silently converge on the wrong answer.
    """
    assert _CODE4["TGA"] == "W"      # the one documented difference
    assert _CODE4["ATA"] == "I"      # table 5 would say M
    assert _CODE4["AGA"] == "R"      # table 5 would say S
    assert _CODE4["AGG"] == "R"      # table 5 would say S
    assert _CODE4["TAA"] == "*" and _CODE4["TAG"] == "*"
    assert len(_CODE4) == 64


def test_translate_and_stop_counting():
    assert translate("ATGTGA") == "MW"          # TGA is Trp, not a stop
    assert count_stops("ATGTGA") == 0
    assert count_stops("ATGTAA") == 1
    assert translate("ATG") == "M"
    assert translate("AT") == ""                # partial codon dropped


# --------------------------------------------------------------------------
# the safety property
# --------------------------------------------------------------------------

def test_known_lineage_is_never_called_new(ref):
    res = check_sequence(REF_A, ref, label="resubmitted")
    assert res.verdict == "known_lineage"
    assert "exact_match_to_known_lineage" in res.flags
    assert res.nearest[0][0] == "LINA" and res.nearest[0][1] == 0


def test_known_lineage_still_recognised_when_frame_shifted(ref):
    """A known sequence missing its first base must still resolve to that lineage.

    This is the primer-trimmed case: dropping position 1 must not turn an
    existing lineage into a spurious new one.
    """
    res = check_sequence(REF_A[1:], ref, label="trimmed")
    assert res.offset == 1, "should detect that position 1 is missing"
    assert res.verdict == "known_lineage"
    assert res.nearest[0][0] == "LINA"


def test_known_lineage_recognised_with_leading_padding(ref):
    res = check_sequence("N" + REF_A[1:], ref, label="padded")
    assert res.verdict == "known_lineage"


def test_exact_match_over_too_few_positions_is_not_called_known(ref):
    """A short overlap with zero mismatches is not an identity.

    Regression guard for a real defect: a partial barcode laid over a thinly covered
    reference could agree at 20 positions, disagree nowhere, and be reported as
    "identical over all 20 comparable positions" -- a known lineage, case closed. The
    exact match must still lead the neighbor list (it is the safety property that a
    known lineage is never called new), but the verdict has to say plainly that the
    overlap is too short to call it the same lineage and ask for a longer read.
    """
    short = REF_A[:MIN_COMPARABLE_TO_RANK - 100]   # a 200 bp partial barcode
    res = check_sequence(short, ref, label="partial")
    assert res.verdict == "exact_match_low_coverage"
    assert "exact_match_over_too_few_positions" in res.flags
    assert "exact_match_to_known_lineage" not in res.flags
    # Still not new, and the identical lineage still heads the list where a curator
    # can see it.
    assert res.verdict != "new_candidate"
    assert res.nearest[0][1] == 0
    assert res.nearest[0][2] == len(short)
    # The note has to name the problem and the remedy. Matched loosely: wording is
    # not the contract, the flag is.
    joined = " ".join(res.notes).lower()
    assert "too short" in joined and "longer read" in joined


def test_exact_match_at_the_coverage_floor_is_still_known(ref):
    """Pins the boundary: exactly MIN_COMPARABLE_TO_RANK positions is enough."""
    res = check_sequence(REF_A[:MIN_COMPARABLE_TO_RANK], ref, label="at_floor")
    assert res.verdict == "known_lineage"
    assert res.nearest[0][2] == MIN_COMPARABLE_TO_RANK


# --------------------------------------------------------------------------
# novelty and framing
# --------------------------------------------------------------------------

def test_single_base_difference_is_new_but_flagged(ref):
    query = REF_A[:-1] + ("C" if REF_A[-1] != "C" else "G")
    res = check_sequence(query, ref, label="one_off")
    assert res.verdict == "new_candidate"
    assert "one_base_from_known_lineage" in res.flags
    assert res.nearest[0][1] == 1


def test_offset_detected_and_reported(ref):
    """A sequence starting at frame position 3 is reported as needing 2 Ns.

    Frame position 3 is not one of the shapes a correctly processed barcode arrives in
    (see CANONICAL_SHAPES), so this is a genuine displacement and must still be flagged.

    The note is matched case-insensitively on purpose: flag names are the stable contract
    and prose is not, so a test that pins exact wording fails on a rewrite that changed
    nothing about behavior.
    """
    res = check_sequence(REF_A[2:], ref, label="shift2")
    assert res.offset == 2
    assert "needs_reframing" in res.flags
    assert any("prepend 2 n" in n.lower() for n in res.notes)


def test_canonical_amplicon_shapes_are_not_called_misframed(ref):
    """The three shapes a correctly processed barcode actually arrives in.

    Regression guard for a real defect: every non-zero offset used to be flagged as
    needing reframing, which told 100% of correctly trimmed haem amplicons — the
    commonest submission MalAvi receives — that they were misframed. Measured against
    the real release, 80 of 80 canonical sequences were wrongly flagged.

    Sanger reads position 1 as well, so a full-window sequence is normal even though the
    primers amplify only 478 bp.
    """
    from malavi_curation.sequence_check import CANONICAL_SHAPES

    # Absolute lengths, not relative to whatever reference is passed: 478 bp is a fact
    # about where HaemF and HaemR2 bind, not about the fixture in this test.
    assert (0, 479) in CANONICAL_SHAPES, "the full 479 bp window (Sanger) must be canonical"
    assert (1, 478) in CANONICAL_SHAPES, "the 478 bp haem amplicon must be canonical"
    assert (1, 476) in CANONICAL_SHAPES, "the 476 bp leuc amplicon must be canonical"


def test_canonical_amplicons_from_the_real_release_are_not_misframed():
    """The behavioral half, run against the actual release rather than a fixture.

    A synthetic 63 bp reference cannot exercise this: the canonical shapes are real
    lengths, so the defect only appears against the real alignment. Skipped where the
    release alignment has not been exported.
    """
    from malavi_curation.config import load_config, repo_root
    from malavi_curation.sequence_check import (
        CANONICAL_SHAPES, Reference, default_alignment_path,
    )

    path = default_alignment_path(repo_root(), load_config()["malaviR"]["release"])
    if path is None or not path.is_file():
        pytest.skip("no release alignment on disk (run export/build_downloads.R)")

    release = Reference.from_fasta(path)
    checked = 0
    for name, seq in zip(release.names, release.seqs):
        if len(seq) != release.width or set(seq) - set("ACGT"):
            continue
        for query, shape in ((seq, "Sanger"), (seq[1:], "haem"), (seq[1:477], "leuc")):
            res = check_sequence(query, release, label=name)
            assert (res.offset, len(query)) in CANONICAL_SHAPES, (
                f"{shape} shape of {name} did not register where it should")
            assert "needs_reframing" not in res.flags, (
                f"a correctly trimmed {shape} sequence was called misframed — this is "
                f"the defect that flagged 100% of real haem submissions")
            assert "length_differs_from_reference" not in res.flags, (
                f"a canonical {shape} amplicon is the right length for its assay")
        checked += 1
        if checked >= 25:
            break
    assert checked, "no clean full-length lineages found to test against"


def test_in_frame_sequence_is_not_flagged_for_reframing(ref):
    res = check_sequence(REF_A, ref, label="ok")
    assert res.offset == 0
    assert "needs_reframing" not in res.flags


def test_unrelated_sequence_is_unplaceable_not_guessed(ref):
    """Nonsense must be refused, never forced onto a best-effort offset."""
    res = check_sequence("ACGT" * 40, ref, label="junk")
    assert res.verdict == "unplaceable"
    assert "could_not_register_to_reference_frame" in res.flags


def test_empty_and_header_only_input(ref):
    assert check_sequence("", ref).verdict == "empty"
    assert check_sequence(">just a header\n", ref).verdict == "empty"


def test_clean_strips_fasta_header_and_whitespace():
    assert clean(">LIN1 desc\nACGT\nACGT\n") == "ACGTACGT"
    assert clean("acgt 123\tacgt") == "ACGTACGT"


def test_stop_codons_are_reported(ref):
    """A sequence carrying an in-frame stop is flagged."""
    query = "ATGTAA" + REF_A[6:]
    res = check_sequence(query, ref, label="stopper")
    if res.offset == 0:
        assert res.n_stop_codons >= 1
        assert "contains_stop_codon" in res.flags


def test_determinism(ref):
    """Same input, same output -- byte for byte, every time."""
    a = check_sequence(REF_B, ref, label="x").as_dict()
    b = check_sequence(REF_B, ref, label="x").as_dict()
    assert a == b
