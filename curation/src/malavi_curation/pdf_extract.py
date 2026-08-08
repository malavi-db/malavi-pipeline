"""Extract text and tables from a publication PDF.

Text engine: **poppler `pdftotext`** (subprocess). On the two-column journal
layouts these papers use, pdftotext respects column reading order, whereas
pdfplumber's text extractor interleaves columns and splits binomials/accessions.
We then apply conservative de-hyphenation (rejoin word-wrapped fragments like
``Strigi-\\nformes`` -> ``Strigiformes``) without touching accession ranges
(``PV948475-\\nPV948494``), which must keep their dash.

Tables: pdfplumber is still used (optional 'pdf' extra) to pull any *ruled*
tables, since those carry structured host/lineage/locality data when a paper has
them. But many tables — especially in PDF supplementary files — have no ruling
lines, so pdfplumber finds nothing and default pdftotext collapses their columns
into an unreadable jumble. To recover these we also run a second pdftotext pass
with ``-layout`` (``layout_text``), which preserves column alignment. The two text
surfaces are complementary: default mode is best for the running prose body
(reading order across journal columns), ``-layout`` is best for wide column tables
and supplements (host x lineage x accession matrices, per-locality prevalence).

Measured on the ten-paper ground-truth corpus (2026-07-29), pdfplumber found
**zero** ruled tables in Harl et al 2026, Himmel et al 2024, Schmid et al 2017a
and Pacheco et al 2024 — every one of which prints its host x lineage table
unruled. So ``layout_tables()`` carves those tables back out of the ``-layout``
text by their whitespace column structure; see ``carve_layout_tables``.

Extraction is deliberately permissive: keep everything; the downstream miners
(recall-oriented) decide what is relevant. ``mining_text()`` unions the surfaces;
because the miners dedupe, adding the layout surface only raises recall.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# Conservative de-hyphenation: rejoin a word split across a line ONLY when both
# sides are letters and the continuation starts lowercase (a true word wrap).
# This leaves "PV948475-\nPV948494" (digit / uppercase continuation) intact so
# accession ranges survive.
_HYPHEN_WRAP = re.compile(r"([A-Za-z])-\s*\n\s*([a-z])")

# --- Carving tables out of -layout text ------------------------------------
#
# In `pdftotext -layout` output a table survives as a run of lines whose columns
# line up: every column is separated from the next by a run of spaces that is
# blank on *every* line of the table. Finding those all-blank vertical stripes
# recovers the column boundaries, and the table becomes a matrix again.
#
# The detector below is deliberately permissive — it will also propose blocks
# that are really two columns of running prose, or the tip labels of a
# phylogeny. That is by design: the consumer (``table_extract``) already refuses
# any block whose top rows do not map at least two MalAvi fields *and* whose
# values do not fit the shape of those fields. Being strict here instead would
# mean re-implementing that judgement twice, in a place with less information.

# Minimum spaces that count as a column separator. One space is just a word
# break; two or more is a deliberate gap in every layout these journals use.
_MIN_COLUMN_GAP = 2

# A block must have at least this many column-structured lines. Two lines can
# line up by accident; three rarely do.
_MIN_STRUCTURED_LINES = 3

# One "cell" of a layout line: a run of non-space text terminated by a column
# gap or by the end of the line.
_CELL_SPAN = re.compile(r"\S(?:.*?\S)?(?=\s{%d,}|$)" % _MIN_COLUMN_GAP)


def _is_blank(character: str) -> bool:
    """Is this character whitespace for column-carving purposes?

    ``pdftotext -layout`` normally pads with plain spaces, but tabs and
    non-breaking spaces do appear (the latter especially from publisher PDFs that
    encode a thin space between a number and its unit).
    """
    return character.isspace() or character in "     "


def _is_column_structured(line: str) -> bool:
    """Does this line look like a table row rather than running prose?

    A table row has at least two cells separated by a real column gap. A prose
    line has one long cell, because prose is separated by single spaces.
    """
    return len(_CELL_SPAN.findall(line)) >= 2


def _column_spans(structured_lines: List[str], width: int) -> List[Tuple[int, int]]:
    """Infer column boundaries from the vertical stripes of whitespace.

    A character position belongs to a separator only if it is blank on *every*
    structured line (lines shorter than the block are padded, so trailing space
    counts as blank). Runs of at least ``_MIN_COLUMN_GAP`` such positions are the
    separators; the spans between them are the columns.
    """
    is_blank_everywhere = [True] * width
    for line in structured_lines:
        for position, character in enumerate(line.ljust(width)):
            # Any whitespace counts as a gap, not just U+0020. A single tab or
            # non-breaking space inside an otherwise blank stripe destroyed the
            # separator for the whole block, merging two real columns into one
            # cell -- and a merged cell holding both a lineage and a host still
            # looks parseable downstream.
            if not _is_blank(character):
                is_blank_everywhere[position] = False

    separators: List[Tuple[int, int]] = []
    position = 0
    while position < width:
        if not is_blank_everywhere[position]:
            position += 1
            continue
        run_start = position
        while position < width and is_blank_everywhere[position]:
            position += 1
        if position - run_start >= _MIN_COLUMN_GAP:
            separators.append((run_start, position))

    columns: List[Tuple[int, int]] = []
    cursor = 0
    for separator_start, separator_end in separators:
        if separator_start > cursor:
            columns.append((cursor, separator_start))
        cursor = separator_end
    if cursor < width:
        columns.append((cursor, width))
    return columns


def _candidate_blocks(lines: List[str]) -> List[Tuple[str, List[str]]]:
    """Group layout lines into candidate tables, each with a caption.

    A block runs until something clearly ends it: a page break (form feed), two
    blank lines, or two consecutive prose lines. A *single* prose line does not
    end a block, because tables in these papers routinely carry ungapped
    sub-heading rows inside them — Harl et al 2026's Table 1 interleaves
    "Leucocytozoon toddi L2 group" between its data rows.

    The caption is the last prose line seen before the block started, which is
    where "Table 1 Samples for mitochondrial genome analysis" lives.
    """
    blocks: List[Tuple[str, List[str]]] = []
    current: List[str] = []
    caption = ""
    last_prose_line = ""
    consecutive_blanks = 0
    consecutive_prose = 0

    def close_block() -> None:
        nonlocal current
        structured = sum(1 for line in current if _is_column_structured(line))
        if structured >= _MIN_STRUCTURED_LINES:
            blocks.append((caption, list(current)))
        current = []

    for line in lines:
        if "\f" in line:                       # page break always ends a table
            close_block()
            consecutive_blanks = consecutive_prose = 0
            continue

        if not line.strip():
            consecutive_blanks += 1
            if consecutive_blanks >= 2:
                close_block()
            elif current:
                current.append(line)           # a single blank line is internal
            continue
        consecutive_blanks = 0

        if _is_column_structured(line):
            consecutive_prose = 0
            if not current:
                caption = last_prose_line
            current.append(line)
        else:
            consecutive_prose += 1
            last_prose_line = line.strip()
            if consecutive_prose >= 2:
                close_block()
            elif current:
                current.append(line)           # e.g. a sub-heading row
    close_block()
    return blocks


def _merge_continuation_rows(
    rows: List[List[Optional[str]]],
) -> List[List[Optional[str]]]:
    """Fold wrapped rows back into the row they continue.

    A cell too wide for its column wraps onto the following lines, which then
    carry no value in the first column. Himmel et al 2024's Table 2 does this on
    almost every row ("Haemoproteus / fringillae hCCF3, / Haemoproteus / magnus
    hCCF6" is one cell over four lines), and so does the four-line header above
    it. Without the merge, every lineage name in that table is stranded on a row
    with no key of its own.
    """
    merged: List[List[Optional[str]]] = []
    for row in rows:
        is_continuation = merged and not row[0] and any(row)
        if not is_continuation:
            merged.append(list(row))
            continue
        previous = merged[-1]
        for index, value in enumerate(row):
            if not value:
                continue
            previous[index] = f"{previous[index]} {value}" if previous[index] else value
    return merged


def carve_layout_tables(layout_text: str) -> List[Tuple[str, List[List[Optional[str]]]]]:
    """Recover unruled tables from ``pdftotext -layout`` output.

    Returns ``(caption, matrix)`` pairs in document order, in the same shape as
    the sheet/Word-table blocks that ``table_extract`` already consumes, so a PDF
    table and a spreadsheet sheet go down exactly the same parsing path.

    Only the column-structured lines vote on where the columns are; prose lines
    inside a block are then split at those same boundaries, which lands a
    sub-heading in the first column where it harmlessly fails the key-field test.
    """
    tables: List[Tuple[str, List[List[Optional[str]]]]] = []

    for caption, block_lines in _candidate_blocks(layout_text.split("\n")):
        structured = [line for line in block_lines if _is_column_structured(line)]
        width = max(len(line) for line in block_lines)
        columns = _column_spans(structured, width)
        if len(columns) < 2:
            continue

        rows: List[List[Optional[str]]] = []
        for line in block_lines:
            padded = line.ljust(width)
            rows.append([padded[start:end].strip() or None for start, end in columns])

        rows = _merge_continuation_rows([row for row in rows if any(row)])
        if len(rows) >= _MIN_STRUCTURED_LINES:
            tables.append((caption, rows))

    return tables


def _find_pdftotext() -> Optional[str]:
    """Locate the pdftotext binary (PATH, then the user-local install)."""
    found = shutil.which("pdftotext")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "pdftotext"
    return str(fallback) if fallback.is_file() else None


def clean_text(text: str) -> str:
    """Apply conservative de-hyphenation; preserve newlines otherwise."""
    return _HYPHEN_WRAP.sub(r"\1\2", text)


@dataclass
class ExtractedTable:
    """One table detected on one page (0-based page index)."""

    page: int
    rows: List[List[Optional[str]]]

    def as_text(self) -> str:
        """Flatten the table to whitespace-joined text (for token mining)."""
        return "\n".join(
            " ".join("" if c is None else str(c) for c in row) for row in self.rows
        )


@dataclass
class ExtractedDocument:
    """Parsed contents of one PDF."""

    path: Path
    text: str = ""                                    # cleaned pdftotext output (reading order)
    layout_text: str = ""                             # cleaned pdftotext -layout (columns preserved)
    tables: List[ExtractedTable] = field(default_factory=list)
    n_pages: int = 0
    engine: str = ""                                  # which text engine was used

    def flat_text(self) -> str:
        """Whitespace-collapsed reading-order text — best for matching
        line-wrapped binomials and accession ranges (the dash keeps a flanking
        space, which the range pattern tolerates)."""
        return re.sub(r"\s+", " ", self.text)

    def flat_layout_text(self) -> str:
        """Whitespace-collapsed layout-preserved text. Column alignment is lost on
        collapse, but the tokens that matter (binomials, accessions, ranges) stay
        intact and adjacent, which is all the token miners need."""
        return re.sub(r"\s+", " ", self.layout_text)

    def mining_text(self) -> str:
        """Full token-mining surface: reading-order prose + layout-preserved text
        (recovers unruled column tables and PDF supplements) + ruled tables.

        The miners (accessions, hosts/geography) are recall-oriented and dedupe,
        so unioning surfaces only raises recall. This is the surface the pipeline
        and benchmark should feed to the miners.
        """
        parts = [self.flat_text()]
        if self.layout_text:
            parts.append(self.flat_layout_text())
        parts.extend(t.as_text() for t in self.tables)
        return "\n".join(p for p in parts if p)

    def layout_tables(self) -> List[Tuple[str, List[List[Optional[str]]]]]:
        """Unruled tables carved out of the layout text, as (caption, matrix).

        These are the tables pdfplumber cannot see because they have no ruling
        lines — which, in this corpus, is most of them.
        """
        return carve_layout_tables(self.layout_text)

    def all_text(self) -> str:
        """Page text plus every detected table, flattened — the full token surface."""
        parts = [self.text, self.layout_text]
        parts.extend(t.as_text() for t in self.tables)
        return "\n".join(p for p in parts if p)


def _run_pdftotext(path: Path, layout: bool) -> Optional[str]:
    """Return cleaned text via poppler pdftotext, or None if unavailable/failed.

    ``layout=True`` adds ``-layout`` to preserve physical column alignment.
    """
    binary = _find_pdftotext()
    if not binary:
        return None
    cmd = [binary]
    if layout:
        cmd.append("-layout")
    cmd += [str(path), "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return clean_text(out.stdout)


def _extract_with_pdfplumber(path: Path):
    """Return (text, tables, n_pages) via pdfplumber (text fallback + tables)."""
    import pdfplumber  # lazy: package imports without the 'pdf' extra

    text_chunks: List[str] = []
    tables: List[ExtractedTable] = []
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for page_index, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            if page_text:
                text_chunks.append(page_text)
            for table in page.extract_tables() or []:
                tables.append(ExtractedTable(page=page_index, rows=table))
    return clean_text("\n".join(text_chunks)), tables, n_pages


def extract_pdf(path: str | Path) -> ExtractedDocument:
    """Extract text (pdftotext, falling back to pdfplumber) + tables (pdfplumber)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    doc = ExtractedDocument(path=path)

    # Tables (and page count) come from pdfplumber. Done first so we still get a
    # page count even if pdftotext is the text source.
    try:
        pp_text, doc.tables, doc.n_pages = _extract_with_pdfplumber(path)
    except Exception:
        pp_text, doc.tables, doc.n_pages = "", [], 0

    # Prefer pdftotext for the text body (column-aware); fall back to pdfplumber.
    pt_text = _run_pdftotext(path, layout=False)
    if pt_text is not None:
        doc.text, doc.engine = pt_text, "pdftotext"
    else:
        doc.text, doc.engine = pp_text, "pdfplumber"

    # Second pass: layout-preserved text recovers unruled column tables and PDF
    # supplements that the reading-order pass collapses. Best-effort; empty if
    # pdftotext is unavailable.
    layout = _run_pdftotext(path, layout=True)
    if layout is not None:
        doc.layout_text = layout

    return doc
