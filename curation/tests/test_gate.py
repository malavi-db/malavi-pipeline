"""Tests for the pre-ingest validation gate (gate.py).

The snapshot-backed checks are exercised with an explicit in-memory snapshot so
the tests don't depend on the generated data/db_snapshot.json.
"""
from malavi_curation.gate import apply_gate, run_gate

SNAPSHOT = {
    "lineages": ["SGS1", "GRW04"],
    "accession_to_lineage": {"AF254975": "SGS1"},
}


def _findings_by_check(submission, snapshot=SNAPSHOT):
    out = {}
    for f in run_gate(submission, snapshot=snapshot):
        out.setdefault(f.check, []).append(f)
    return out


def test_prevalence_found_exceeds_tested_is_error():
    sub = {"records": [{"host_species": "Parus major",
                        "number_tested": 5, "number_found": 9}]}
    f = _findings_by_check(sub)["prevalence_sanity"][0]
    assert f.severity == "error"


def test_prevalence_found_without_tested_warns():
    sub = {"records": [{"host_species": "Parus major", "number_found": 3}]}
    f = _findings_by_check(sub)["prevalence_sanity"][0]
    assert f.severity == "warn"


def test_prevalence_valid_counts_no_finding():
    sub = {"records": [{"host_species": "Parus major",
                        "number_tested": 50, "number_found": 7}]}
    assert "prevalence_sanity" not in _findings_by_check(sub)


def test_malformed_accession_warns():
    sub = {"accessions": ["NOTANACC"]}
    assert _findings_by_check(sub)["accession_format"][0].severity == "warn"


def test_known_accession_flagged_as_re_report():
    sub = {"accessions": ["AF254975"]}
    f = _findings_by_check(sub)["accession_collision"][0]
    assert "SGS1" in f.message and f.severity == "info"


def test_new_lineage_flagged_info():
    sub = {"records": [{"lineage_name": "NEWLIN99", "host_species": "Parus major"}]}
    f = _findings_by_check(sub)["lineage_known"][0]
    assert f.severity == "info" and "NEWLIN99" in f.message


def test_known_lineage_not_flagged():
    sub = {"records": [{"lineage_name": "SGS1", "host_species": "Parus major"}]}
    assert "lineage_known" not in _findings_by_check(sub)


def test_apply_gate_sets_passed_false_on_error():
    sub = {"records": [{"number_tested": 1, "number_found": 5}], "accessions": [], "vectors": []}
    apply_gate(sub, snapshot=SNAPSHOT)
    assert sub["gate"]["passed"] is False
    assert sub["gate"]["n_error"] >= 1


def test_apply_gate_passes_clean_submission():
    sub = {"records": [{"host_species": "Parus major", "number_tested": 50, "number_found": 7}],
           "accessions": ["AF254975"], "vectors": []}
    apply_gate(sub, snapshot=SNAPSHOT)
    assert sub["gate"]["passed"] is True  # only info-level findings


# ---------------------------------------------------------------------------
# INSDC accession-availability check
#
# The network is always stubbed here. These tests must not depend on NCBI being
# reachable, and must not hit it: the point is to pin the gate's behavior given
# a lookup result, not to test NCBI.
# ---------------------------------------------------------------------------

def test_accession_resolves_flags_unreleased(monkeypatch):
    """A well-formed accession that INSDC cannot serve is a warning, not an error.

    This is the Magana Vazquez et al. 2026 case: PX312166-PX312169 are cited in a
    published paper, pass every format rule, and return nothing from GenBank, ENA
    or DDBJ because they were reserved and never released.
    """
    import malavi_curation.gate as g
    monkeypatch.setattr(g, "_resolve_accessions_insdc", lambda accs, **kw: {"PX312166"})
    sub = {"records": [], "vectors": [], "accessions": ["PX312166", "PX312167"]}
    findings = [f for f in g.run_gate(sub, snapshot=SNAPSHOT, check_online=True)
                if f.check == "accession_resolves"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].where == "PX312167"
    # A missing sequence must never block ingestion on its own: submitting before
    # the sequences go public is part of the intended two-stage workflow.
    g.apply_gate(sub, snapshot=SNAPSHOT, check_online=True)
    assert sub["gate"]["passed"] is True


def test_accession_resolves_all_present(monkeypatch):
    import malavi_curation.gate as g
    monkeypatch.setattr(g, "_resolve_accessions_insdc",
                        lambda accs, **kw: {"OP006613", "OP006612"})
    sub = {"records": [], "vectors": [], "accessions": ["OP006613.1", "OP006612"]}
    findings = [f for f in g.run_gate(sub, snapshot=SNAPSHOT, check_online=True)
                if f.check == "accession_resolves"]
    assert len(findings) == 1 and findings[0].severity == "info"


def test_accession_resolves_network_failure_is_a_warning(monkeypatch):
    """No network must degrade to "not checked", never to a false accusation."""
    import malavi_curation.gate as g
    monkeypatch.setattr(g, "_resolve_accessions_insdc", lambda accs, **kw: None)
    sub = {"records": [], "vectors": [], "accessions": ["PX312166"]}
    findings = [f for f in g.run_gate(sub, snapshot=SNAPSHOT, check_online=True)
                if f.check == "accession_resolves"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert "Could not reach INSDC" in findings[0].message


def test_accession_check_is_off_by_default(monkeypatch):
    """The gate stays offline and deterministic unless explicitly asked."""
    import malavi_curation.gate as g

    def _boom(*a, **k):
        raise AssertionError("the gate hit the network without check_online=True")

    monkeypatch.setattr(g, "_resolve_accessions_insdc", _boom)
    sub = {"records": [], "vectors": [], "accessions": ["PX312166"]}
    assert "accession_resolves" not in _findings_by_check(sub)


def test_offline_env_var_overrides_check_online(monkeypatch):
    import malavi_curation.gate as g

    def _boom(*a, **k):
        raise AssertionError("MALAVI_GATE_OFFLINE=1 did not suppress the lookup")

    monkeypatch.setattr(g, "_resolve_accessions_insdc", _boom)
    monkeypatch.setenv("MALAVI_GATE_OFFLINE", "1")
    sub = {"records": [], "vectors": [], "accessions": ["PX312166"]}
    g.run_gate(sub, snapshot=SNAPSHOT, check_online=True)
