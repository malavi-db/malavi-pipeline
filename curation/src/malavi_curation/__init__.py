"""malavi_curation — paper-first curation helper for MalAvi.

Pipeline (each stage is a module; orchestrated end-to-end by ``pipeline.py``):

    pdf_extract     PDF -> text (pdftotext) + tables (pdfplumber)   [implemented]
    accession_mine  text -> NCBI accessions, incl. range expansion  [implemented]
    hosts_geography text -> host binomials + countries (gazetteer)  [implemented]
    record_builder  the above -> a submission.schema.json candidate [implemented]
    curator_report  candidate(s) -> a review-ready Markdown report  [implemented]

    validate        submission -> malaviR flags (R bridge)            [implemented]

Run the whole thing on a folder of PDFs:
    python -m malavi_curation.pipeline <pdf_dir> [--out report.md] [--validate]

Python is used only for parsing. Host-name reconciliation and the improbable
host/locality check are delegated to the malaviR R package via curation/r/
(validate_record.R, host_geo_flag.R), invoked through validate.py.
"""

__version__ = "0.0.1"
