# @title Independently validate the reading frame used by the release-wide QC sweep
# @purpose Confirm, against annotated haemosporidian mitochondrial genomes, that
#          (a) NCBI genetic code 4 is the right code, and (b) column 1 of the
#          MalAvi 479 bp alignment is the first position of a codon.
# @why export/build_reports.R counts stop codons by translating the MalAvi
#      alignment from column 1 in frame 1 with code 4. If either assumption were
#      wrong, the QC report would flag nonsense for the whole release. This
#      script checks both against an external source of truth (GenBank CDS
#      annotations and their curator-supplied protein translations) rather than
#      against the same assumption it is testing.
# @input /mnt/biostore-all/Vellis/malaviTree/data/raw/backbone_mtdna_2026-06-15/backbone_avian.gb
# @input docs/assets/downloads/malavi_alignment_<release>.fasta
# @output (report to stdout; exits non-zero if any check fails)
# @program python
# @program biopython
# @critical-var GENETIC_CODE
# @critical-var EXPECTED_FRAME_OFFSET
# =============================================================================
import sys
import glob
import collections

from Bio import SeqIO
from Bio.Seq import Seq

GENBANK = ("/mnt/biostore-all/Vellis/malaviTree/data/raw/"
           "backbone_mtdna_2026-06-15/backbone_avian.gb")

# NCBI translation table 4 (mold/protozoan/coelenterate mitochondrial). The
# distinguishing feature versus the standard code is TGA = Trp, not a stop.
GENETIC_CODE = 4

# The claim under test: the MalAvi window begins at a codon boundary, i.e. the
# offset of the window within the cytb CDS is a multiple of 3.
EXPECTED_FRAME_OFFSET = 0

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# =============================================================================
# 1. Collect annotated cytb CDS records
# =============================================================================
print("1. Reading annotated cytb CDS features")
cds_records = []
for rec in SeqIO.parse(GENBANK, "genbank"):
    for feat in rec.features:
        if feat.type != "CDS":
            continue
        gene = " ".join(feat.qualifiers.get("gene", []) +
                        feat.qualifiers.get("product", [])).lower()
        if "cytb" not in gene and "cytochrome b" not in gene:
            continue
        nt = feat.extract(rec.seq)
        aa = feat.qualifiers.get("translation", [None])[0]
        if aa is None or len(nt) % 3 != 0:
            continue
        cds_records.append((rec.id, str(nt).upper(), aa,
                            feat.qualifiers.get("transl_table", ["?"])[0]))

print(f"   {len(cds_records)} cytb CDS with a curator-supplied translation")
check("found annotated cytb CDS records", len(cds_records) >= 10,
      f"n={len(cds_records)}")
if not cds_records:
    sys.exit(1)

# =============================================================================
# 2. Does genetic code 4 reproduce the curators' own protein?
# =============================================================================
# This is the external check on the code: GenBank's /translation was produced by
# the submitter/NCBI, not by us. If code 4 reproduces it and the standard code
# does not, code 4 is confirmed as correct for these organisms.
print("\n2. Genetic code")

# GenBank states the code outright on each CDS. This is the strongest single
# piece of evidence and needs no inference.
declared = collections.Counter(t for _, _, _, t in cds_records)
print(f"   /transl_table declared on the CDS features: {dict(declared)}")
check("every CDS declares genetic code 4", set(declared) == {str(GENETIC_CODE)})

# Independently, check that code 4 reproduces the curators' protein.
#
# Residue 1 is excluded deliberately. GenBank renders the initiation codon as M
# whatever it actually codes for, so a faithful translation legitimately differs
# there; counting that as a mismatch would reject the correct code. Every
# position after the start codon must match exactly.
agree4 = agree1 = 0
body_mismatch = 0
for _, nt, aa_ref, _ in cds_records:
    aa4 = str(Seq(nt).translate(table=4)).rstrip("*")
    aa1 = str(Seq(nt).translate(table=1)).rstrip("*")
    agree4 += (aa4[1:] == aa_ref[1:])
    agree1 += (aa1[1:] == aa_ref[1:])
    if aa4[1:] != aa_ref[1:]:
        body_mismatch += 1
n = len(cds_records)
print(f"   after the initiation codon, code 4 reproduces {agree4}/{n}; "
      f"standard code 1 reproduces {agree1}/{n}")
check("genetic code 4 reproduces the annotated protein", body_mismatch == 0,
      f"{body_mismatch} mismatched beyond residue 1")
check("code 4 is at least as good as the standard code", agree4 >= agree1)

# =============================================================================
# 3. Where does the MalAvi 479 bp window sit inside the CDS?
# =============================================================================
# Build an unambiguous consensus of the MalAvi alignment, then slide it along
# each cytb CDS and take the offset with the fewest mismatches. The offset's
# remainder mod 3 is the frame of alignment column 1 within the coding sequence.
print("\n3. Position of the MalAvi window within the cytb CDS")
aln_files = sorted(glob.glob("docs/assets/downloads/malavi_alignment_*.fasta"))
if not aln_files:
    print("   MalAvi alignment FASTA not found; run export/build_downloads.R first.")
    sys.exit(1)
aln_path = aln_files[-1]

seqs = [str(r.seq).upper() for r in SeqIO.parse(aln_path, "fasta")]
width = len(seqs[0])
print(f"   {len(seqs)} sequences x {width} bp from {aln_path.split('/')[-1]}")

consensus = []
for col in range(width):
    counts = collections.Counter(s[col] for s in seqs if s[col] in "ACGT")
    consensus.append(counts.most_common(1)[0][0] if counts else "N")
consensus = "".join(consensus)

offsets = []
for rec_id, nt, _, _ in cds_records:
    if len(nt) < width:
        continue
    best_off, best_mm = None, None
    for off in range(len(nt) - width + 1):
        window = nt[off:off + width]
        mm = sum(1 for a, b in zip(window, consensus) if a != b)
        if best_mm is None or mm < best_mm:
            best_off, best_mm = off, mm
    # Only trust confidently-placed windows: cytb is conserved, so a real match
    # sits far below the ~75% mismatch of a random placement.
    if best_mm is not None and best_mm < width * 0.25:
        offsets.append((rec_id, best_off, best_mm))

print(f"   confidently placed in {len(offsets)}/{len(cds_records)} genomes")
check("window placed in most genomes", len(offsets) >= 10, f"n={len(offsets)}")

frames = collections.Counter(off % 3 for _, off, _ in offsets)
print(f"   offset mod 3 distribution: {dict(frames)}")
example = offsets[:3]
for rec_id, off, mm in example:
    print(f"     {rec_id}: starts at CDS position {off + 1} "
          f"(offset {off}, {mm} mismatches vs consensus)")

check("window starts on a codon boundary in every genome",
      set(frames) == {EXPECTED_FRAME_OFFSET},
      f"frames seen: {sorted(frames)}")

# =============================================================================
# 4. Does the window translate to a real slice of the cytb protein?
# =============================================================================
# The decisive end-to-end check: translate the MalAvi consensus exactly the way
# build_reports.R does (frame 1, code 4) and confirm the peptide really occurs
# in the curators' annotated cytb protein.
print("\n4. Translation of the MalAvi window")
consensus_aa = str(Seq(consensus[:width - width % 3]).translate(table=GENETIC_CODE))
print(f"   consensus peptide ({len(consensus_aa)} aa): {consensus_aa[:60]}...")
check("consensus has no internal stop codon", "*" not in consensus_aa,
      f"{consensus_aa.count('*')} stops")

# Allow for real sequence variation: require a long exact stretch rather than
# the whole peptide, since the consensus spans many species.
hits = 0
for _, _, aa_ref, _ in cds_records:
    probe = consensus_aa[10:40]
    if probe and probe in aa_ref:
        hits += 1
print(f"   a 30 aa stretch of the consensus occurs verbatim in {hits}/{n} proteins")
check("consensus peptide matches annotated cytb protein", hits >= 1, f"n={hits}")

# Frames 2 and 3 should look clearly worse, or the test above proves nothing.
print("\n5. Control: the two wrong frames should be worse")
for shift in (1, 2):
    sub = consensus[shift:]
    aa = str(Seq(sub[:len(sub) - len(sub) % 3]).translate(table=GENETIC_CODE))
    print(f"   frame {shift + 1}: {aa.count('*')} stop codons")
    check(f"frame {shift + 1} is worse than frame 1", aa.count("*") > 0)

# =============================================================================
# 6. The number the QC report actually publishes
# =============================================================================
# build_reports.R counts stop codons per lineage. Show what that count is under
# the correct code versus the standard code, so the cost of getting the code
# wrong is visible rather than assumed.
print("\n6. Release-wide stop-codon counts, by genetic code")
trimmed = [s_[:width - width % 3] for s_ in seqs]
for table in (1, GENETIC_CODE):
    with_stop = sum(1 for s_ in trimmed
                    if "*" in str(Seq(s_.replace("-", "N")).translate(table=table)))
    print(f"   code {table}: {with_stop}/{len(trimmed)} sequences contain a stop codon")
    if table == GENETIC_CODE:
        check("stop codons are rare under code 4",
              with_stop < len(trimmed) * 0.05,
              f"{with_stop}/{len(trimmed)}")

print("\n" + "=" * 60)
if failures:
    print(f"FAILED: {len(failures)} check(s): " + "; ".join(failures))
    sys.exit(1)
print("All frame checks passed.")
