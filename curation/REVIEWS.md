# Reviews of the guarded files

The guarded files are the ones that can send an email or publish to a public destination:
every Apps Script file (as the bundle `curation/apps_script/dist/malavi_curation.gs`) and
the Python programs and modules listed in `ops/record_review.py` (`GUARDED_PYTHON`).
Vincenzo's rule of 2026-10-16: every change to one of them gets an independent review
before it is committed, pasted into the live project or run live.

`curation/tests/test_review_log.py` fails until the CURRENT hashes of both are on a line
below; the Actions workflow runs that test before a live cycle; `build_bundle.py` says
whether the bundle it just wrote is reviewed. To record a review after it has been done
and its findings fixed:

```bash
.venv/bin/python ops/record_review.py --reviewer "review-code agent" \
    --findings results/REVIEW_<date>_<topic>.md --note "<n> findings, all fixed"
.venv/bin/python ops/record_review.py --show      # the hashes now, and whether reviewed
```

The findings record (a file under `results/`, never published) is the evidence; this
table is the index. A line here without a findings file is not a review.

| date | bundle | guarded | reviewer | findings | note |
|---|---|---|---|---|---|
| 2026-10-16 | 22d74d09be59ce1c | f28433e614b22eba | review-code agent (independent, read-only) | results/REVIEW_2026-10-16_guardrails_code.md | 20 findings, 4 MUST, all fixed; plan review 19 findings before it |
