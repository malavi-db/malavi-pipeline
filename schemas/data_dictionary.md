# MalAvi data dictionary

Human-readable column glossary for the published tables. Authoritative as of malaviR
release **2026-03-23**. The machine-readable contracts are the `*.schema.json` files
alongside this one. All tables are sourced from `malaviR::extract_table()`; the 479 bp
cytochrome *b* alignment comes from `malaviR::extract_alignment()`.

Tables join on two keys:
- **`LINEAGE_NAME`** — the MalAvi parasite lineage code (links hosts ↔ lineage summary ↔ morpho ↔ vector ↔ alignment tip).
- **`REFERENCE_NAME`** — citation key (links host/vector/morpho rows ↔ the references table).

---

## Hosts and Sites (`hosts_and_sites`) — 20 columns, ~18,493 rows

Individual host infection records.

| Column | Meaning |
| --- | --- |
| LINEAGE_NAME | Parasite lineage code (e.g. SGS1). |
| ALT_NAME | Alternate / synonymous lineage name — the name the *publication* used for this lineage. May hold several comma-separated names (e.g. `SA15,SA16`). Populated on 5,487 of 18,493 rows. |
| PARASITE_GENUS | *Plasmodium*, *Haemoproteus*, *Leucocytozoon*, or unresolved. |
| ORDER_NAME / FAMILY_NAME / GENUS_NAME / SPECIES_NAME / SUB_SPECIES_NAME | Host taxonomy. |
| HOST_STATUS | Migratory behavior of the host. Observed values: `Resident`, `Migratory`, `Unknown` (plus `NA`). |
| HOST_AGE | Host age class, if recorded. Observed values: `Adult`, `Juvenile`, `Nestling`, `Adult + Juvenile`, `Adult + Nestling`, `Unknown` (plus `NA`). |
| HOST_ENVIRONMENT | Whether the host was free-living or held. Observed values: `Wild`, `Captivity` (no `NA`). The Grand Lineage Summary excludes non-natural (`Captivity`) records. |
| CONTINENT_NAME / COUNTRY_NAME / COUNTRY_REGION_NAME / SITE_NAME | Geography, coarse → fine. |
| SITE_COORDINATES | Latitude/longitude as recorded (free text). |
| NUMBER_FOUND | Count of infected individuals (numeric). |
| NUMBER_TESTED | Count of individuals tested (numeric). |
| REFERENCE_NAME | Citation key into the references table. |
| COMMENT | Free-text note. |

## Grand Lineage Summary (`grand_lineage_summary`) — 24 columns, ~5,368 rows

One row per lineage. **Note:** malaviR ships every column here as character/string,
including the numeric-looking tallies — exports preserve that.

| Column | Meaning |
| --- | --- |
| LINEAGE_NAME | Parasite lineage code. |
| GENBANK_ACC | GenBank accession for the lineage sequence. |
| SEQ_LENGTH | Reported sequence length. |
| GENUS_NAME / SPECIES_NAME | Parasite genus and linked morphospecies, if any. |
| SUM_VECTORS / SUM_HOST / SUM_GENUS / SUM_FAMILY / SUM_ORDER | Tallies of vector records and host taxon ranks. |
| PASSERIFORMES | Recorded in Passeriformes (tally/flag). |
| EUROPE, SUB_SAHARAN_AFRICA, NORTH_AFRICA_AND_MIDDLE_EAST, NORTH_AMERICA, HAWAI, CENTRAL_AMERICA, SOUTH_AMERICA, ASIA, AUSTRALIA_AND_NEW_ZEALAND, OCEANIA, ANTARCTICA, UNKNOWN_REGION | Per-region occurrence tallies. |
| SEQUENCE | Full cytochrome *b* nucleotide sequence for the lineage. |

## Morpho Species Summary (`morpho_species`) — 5 columns, ~256 rows

Lineages linked to morphologically described species.

| Column | Meaning |
| --- | --- |
| LINEAGE_NAME | Parasite lineage code. |
| GENUS_NAME / SPECIES_NAME | Morphologically described parasite name. |
| REFERENCE_NAME | Citation key for the morphological link. |
| MORPHOLOGY_COMMENT | Free-text note on the identification. |

## Table of References (`references`) — 6 columns, ~526 rows (+ optional DOI)

| Column | Meaning |
| --- | --- |
| REFERENCE_NAME | Citation key (joins to host/vector/morpho tables). |
| PUBLICATION_YEAR | Year (numeric). |
| TITLE | Article title. |
| JOURNAL_NAME | Journal. |
| VOLUME_PAGES | Volume and pages. |
| STUDY_TYPE | Study type classification. |
| DOI *(optional)* | Bare DOI (e.g. `10.1111/mec.12345`). Not in the bundled malaviR release; captured going forward by the curation pipeline and used as an exact de-dup key by the publication watcher. |

## Vector Data (`vector_data`) — 6 columns, ~610 rows

| Column | Meaning |
| --- | --- |
| LINEAGE_NAME | Parasite lineage code. |
| VECTOR_SPECIES | Vector species the lineage was detected in. |
| VECTOR_METHOD | Detection method / evidence type. |
| COUNTRY_NAME / SITE_NAME | Geography of the vector record. |
| REFERENCE_NAME | Citation key into the references table. |

## Alignment

479 bp cytochrome *b* barcode, one sequence per lineage. Tip labels follow
`<GENUS_PREFIX>_<LINEAGE>[_<MORPHO_SPECIES>]`, where the prefix is `P_` (*Plasmodium*),
`H_` (*Haemoproteus*), or `L_` (*Leucocytozoon*). Distributed as FASTA.
