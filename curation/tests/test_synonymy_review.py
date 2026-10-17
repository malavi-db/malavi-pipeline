"""The synonymy review: the pair screen, the proposal rule, and decisions -> events.

The invariant: nothing is proposed that the curators' rule does not cover, and nothing
is applied that a curator did not write on the sheet.
"""
from __future__ import annotations

import pytest

from malavi_curation import lineage_history as lh
from malavi_curation import synonymy_review as sr

FULL = ("ATGCATGCTA" * 48)[:479]                 # a complete barcode
PARTIAL = "-" * 9 + FULL[9:]                      # the same, first nine bases undetermined (470)
SHORT = "-" * 150 + FULL[150:]                    # a 3' part (329 determined)
FULL_B = FULL[:300] + ("T" if FULL[300] != "T" else "A") + FULL[301:]   # one base off FULL, at 300
CODED = FULL[:100] + "R" + FULL[101:]             # an R where FULL has A or G


def _lineage(name, seq, genus="Haemoproteus", acc=""):
    return {"LINEAGE_NAME": name, "GENBANK_ACC": acc, "SEQ_LENGTH": "Full" if "-" not in seq else "Partial",
            "GENUS_NAME": genus, "SPECIES_NAME": "", "SEQUENCE": seq, "RECORD_ID": "", "_source": "seed",
            "_added": ""}


def _host(name, species, ref, country="Sweden"):
    return {"LINEAGE_NAME": name, "SPECIES_NAME": species, "GENUS_NAME": species.split()[0],
            "REFERENCE_NAME": ref, "COUNTRY_NAME": country, "RECORD_ID": "", "_source": "seed", "_added": ""}


def _store(lineages, hosts=(), alt=()):
    return {
        "lineages": list(lineages),
        "host_records": list(hosts),
        "vector_records": [],
        "alt_names": list(alt),
        "morpho_species": [],
        "references": [{"REFERENCE_NAME": "A 2020", "PUBLICATION_YEAR": "2020"},
                       {"REFERENCE_NAME": "B 2006", "PUBLICATION_YEAR": "2006"},
                       {"REFERENCE_NAME": "C 2007", "PUBLICATION_YEAR": "2007"}],
        "lineage_history": [],
        "genomic_resources": [],
    }


def _pair_store():
    """One partial with one container, and a duplicate of the container; one base-off decoy."""
    return _store(
        [_lineage("ACCBRE02", FULL, acc="AB2"), _lineage("ACCBRE01", SHORT, acc="AB1"),
         _lineage("TURGUL01", FULL, acc="AB4"), _lineage("DIFF01", FULL_B)],
        [_host("ACCBRE01", "Accipiter brevipes", "A 2020", "Israel"),
         _host("ACCBRE02", "Accipiter brevipes", "B 2006", "Kazakhstan"),
         _host("ACCBRE02", "Accipiter nisus", "B 2006", "Kazakhstan"),
         _host("TURGUL01", "Turdoides gularis", "C 2007", "Myanmar")],
        [{"LINEAGE_NAME": "ACCBRE02", "ALT_NAME": "ACCBRE01", "REFERENCE_NAME": "A 2020",
          "RECORD_ID": "", "_source": "seed", "_added": ""}])


class TestScreen:
    def test_kinds_and_orientation(self):
        pairs = {(p.a, p.b): p for p in sr.screen_pairs(
            {"FULL": FULL, "PART": PARTIAL, "DUP": FULL, "DIFF": FULL_B})}
        assert set(pairs) == {("PART", "FULL"), ("DUP", "FULL"), ("PART", "DUP")}
        assert pairs[("PART", "FULL")].kind == "contained"        # the partial sits in `a`
        assert pairs[("PART", "FULL")].comparable == 470
        assert (pairs[("PART", "FULL")].a_private, pairs[("PART", "FULL")].b_private) == (0, 9)
        assert pairs[("DUP", "FULL")].kind == "duplicate"
        assert not any("DIFF" in k for k in pairs)                 # one real difference: no pair

    def test_ambiguous_needs_overlap(self):
        p5 = "-" * 9 + FULL[9:470] + "-" * 9                      # 9..469
        p3 = FULL[:9] + "-" * 461 + FULL[470:]                    # 0..8, 470..478: no overlap
        assert sr.screen_pairs({"P5": p5, "P3": p3}) == []
        p5 = "-" * 9 + FULL[9:475] + "-" * 4                      # 9..474
        p3 = FULL[:5] + "-" * 5 + FULL[10:]                       # 0..4, 10..478
        (pair,) = sr.screen_pairs({"P5": p5, "P3": p3})
        assert pair.kind == "ambiguous" and (pair.a, pair.b) == ("P3", "P5")
        assert (pair.a_private, pair.b_private, pair.comparable) == (9, 1, 465)

    def test_ambiguity_codes_are_undetermined_but_counted(self):
        (pair,) = sr.screen_pairs({"FULL": FULL, "CODED": CODED})
        assert pair.kind == "contained" and pair.a == "CODED" and pair.b_private == 1
        assert pair.ambiguity_code_matches == 1

    def test_n_against_base_is_not_a_code_match(self):
        (pair,) = sr.screen_pairs({"FULL": FULL, "N": FULL[:100] + "N" + FULL[101:]})
        assert pair.kind == "contained" and pair.ambiguity_code_matches == 0


class TestReview:
    def test_rows_and_proposals(self):
        rows, summary = sr.build_review(_pair_store())
        by = {(r["LINEAGE"], r["OTHER"]): r for r in rows}
        assert set(by) == {("ACCBRE01", "ACCBRE02"), ("ACCBRE01", "TURGUL01"), ("TURGUL01", "ACCBRE02")}
        # ACCBRE01 is contained in two complete lineages: the rule stops
        row = by[("ACCBRE01", "ACCBRE02")]
        assert row["N_CONTAINERS"] == 2 and row["CONTAINERS"] == "ACCBRE02, TURGUL01"
        assert row["PROPOSAL"].startswith("decide: compatible with 2") and row["PROPOSED_INTO"] == ""
        assert row["ALREADY_ALT_NAME"] == "yes" and by[("ACCBRE01", "TURGUL01")]["ALREADY_ALT_NAME"] == ""
        # duplicate: fewer records (TURGUL01, 1) merges into more (ACCBRE02, 2)
        dup = by[("TURGUL01", "ACCBRE02")]
        assert dup["KIND"] == "duplicate" and dup["PROPOSED_INTO"] == "ACCBRE02" == dup["INTO"]
        assert dup["DECISION"] == "" and dup["PROPOSAL"] == "merge into ACCBRE02: one sequence under two names"
        assert summary["by_kind"] == {"duplicate": 1, "contained": 2, "ambiguous": 0}
        assert summary["n_proposed_merges"] == 1 and summary["names_stored_twice"] == []
        assert set(rows[0]) == set(sr.REVIEW_COLUMNS)

    def test_genus_mismatch_stops_the_rule(self):
        store = _store([_lineage("H01", FULL), _lineage("P01", FULL, genus="Plasmodium")])
        (row,), summary = sr.build_review(store)
        assert row["PROPOSAL"].startswith("decide: genus labels differ") and row["PROPOSED_INTO"] == ""
        assert summary["n_cross_genus"] == 1

    def test_duplicate_group_has_one_survivor(self):
        store = _store([_lineage("TURMER11", FULL), _lineage("TURMER12", FULL), _lineage("TURMER13", FULL)],
                       [_host("TURMER12", "Turdus merula", "A 2020"), _host("TURMER12", "Turdus merula", "B 2006"),
                        _host("TURMER11", "Turdus merula", "A 2020"), _host("TURMER13", "Turdus merula", "C 2007")])
        rows, _ = sr.build_review(store)
        assert {r["PROPOSED_INTO"] for r in rows} == {"TURMER12"}          # most records survives
        assert "TURMER12" not in {r["LINEAGE"] for r in rows}
        weak = [r for r in rows if r["OTHER"] != "TURMER12"]
        (row,) = weak
        assert (row["LINEAGE"], row["OTHER"]) == ("TURMER11", "TURMER13")  # later first year goes first
        assert row["PROPOSAL"] == "merge into TURMER12: one sequence under TURMER11, TURMER12, TURMER13"

    def test_profile_facts(self):
        prof = sr.profiles(_pair_store())
        assert prof["ACCBRE02"].host_records == 2 and prof["ACCBRE02"].hosts == 2
        assert prof["ACCBRE02"].first_year == "2006" and prof["ACCBRE02"].countries == 1
        assert prof["ACCBRE01"].bases == 329 and prof["ACCBRE02"].alt_names == 1
        assert prof["ACCBRE02"].reference_names == ["B 2006"]

    def test_unique_container_small_lineage_is_proposed(self):
        store = _store([_lineage("ACCBRE02", FULL), _lineage("ACCBRE01", SHORT)],
                       [_host("ACCBRE01", "Accipiter brevipes", "A 2020")])
        (row,), summary = sr.build_review(store)
        assert row["PROPOSAL"] == "merge into ACCBRE02: adds 1 host record(s) and is contained in it"
        assert row["PROPOSED_INTO"] == "ACCBRE02" and summary["n_proposed_merges"] == 1

    def test_many_records_are_not_proposed(self):
        hosts = [_host("CCF2", "Parus major", "A 2020")] * 3 + [_host("FULL01", "Parus major", "B 2006")] * 5
        store = _store([_lineage("FULL01", FULL), _lineage("CCF2", PARTIAL)], hosts)
        (row,), _ = sr.build_review(store)
        assert row["PROPOSAL"] == "decide: contained in FULL01, but 3 host record(s) would move"

    def test_better_established_partial_suggests_absorb(self):
        hosts = [_host("GRW02", "Acrocephalus", "B 2006")] * 60 + [_host("FULL01", "Ficedula", "A 2020")] * 3
        store = _store([_lineage("FULL01", FULL), _lineage("GRW02", PARTIAL)], hosts)
        (row,), _ = sr.build_review(store)
        assert row["PROPOSAL"].startswith("decide: contained in FULL01 but better established (60 host records against 3); absorb")
        assert row["INTO"] == "" and row["PROPOSED_INTO"] == ""

    def test_floor_stops_the_rule(self):
        store = _store([_lineage("ACCBRE02", FULL), _lineage("ACCBRE01", SHORT)])
        (row,), _ = sr.build_review(store, min_comparable=300)
        assert row["PROPOSAL"].startswith("merge into ACCBRE02")
        (row,), summary = sr.build_review(store, min_comparable=400)
        assert row["PROPOSAL"] == "none: only 329 positions comparable (floor 400)"
        assert summary["n_below_floor"] == 1

    def test_second_row_under_one_name_is_reported_not_paired(self):
        store = _pair_store()
        store["lineages"].append(_lineage("ACCBRE02", FULL))
        rows, summary = sr.build_review(store)
        assert summary["names_stored_twice"] == ["ACCBRE02"]
        assert not any(r["LINEAGE"] == r["OTHER"] for r in rows)

    def test_prior_decisions_are_shown(self):
        rows, _ = sr.build_review(_pair_store(), prior={"TURGUL01": "remove"})
        assert {r["PRIOR_DECISION"] for r in rows if r["LINEAGE"] == "TURGUL01"} == {"remove"}


def _sheet(store, **decisions):
    """The review rows with (DECISION, INTO, CURATOR_NOTE) written on every row of a lineage."""
    rows, _ = sr.build_review(store)
    for row in rows:
        d = decisions.get(row["LINEAGE"])
        if d:
            row["DECISION"], row["INTO"], row["CURATOR_NOTE"] = d
    return rows


class TestDecisions:
    def test_events_from_the_sheet(self):
        store = _pair_store()
        rows = _sheet(store, TURGUL01=("remove", "", "Sequence does not belong to a MalAvi lineage"),
                      ACCBRE01=("merge", "ACCBRE02", "chosen container"))
        events, notes = sr.decisions_to_events(rows, store, "2026-10-05", "2026-11-01", "curator decision 2026-10-05")
        assert [e.event for e in events] == ["removed", "merged_into"]
        assert events[0].reason == "Sequence does not belong to a MalAvi lineage"
        assert events[1].new_name == "ACCBRE02"
        assert events[1].reason == ("Identical to ACCBRE02 over all 329 positions both cover; ACCBRE02 is "
                                    "the more complete sequence (479 determined bases against 329). chosen container")
        assert all(e.source == "curator decision 2026-10-05" and e.release == "2026-11-01" for e in events)
        for ev in events:
            lh.apply(store, ev)
        assert {r["LINEAGE_NAME"] for r in store["lineages"]} == {"ACCBRE02", "DIFF01"}
        assert len(store["lineage_history"]) == 2

    def test_keep_and_blank_make_no_event(self):
        store = _pair_store()
        rows = _sheet(store, TURGUL01=("keep", "", ""))
        assert sr.decisions_to_events(rows, store, "d", "r", "s") == ([], [])

    def test_absorb_keeps_the_name_and_completes_the_sequence(self):
        hosts = [_host("GRW02", "Acrocephalus", "B 2006")] * 6 + [_host("FULL01", "Ficedula", "A 2020")]
        store = _store([_lineage("FULL01", FULL), _lineage("GRW02", PARTIAL)], hosts)
        rows = _sheet(store, GRW02=("absorb", "FULL01", ""))
        events, notes = sr.decisions_to_events(rows, store, "d", "r", "s")
        assert [(e.event, e.lineage, e.new_name) for e in events] == [
            ("sequence_updated", "GRW02", ""), ("merged_into", "FULL01", "GRW02")]
        assert events[0].new_sequence == FULL
        assert events[0].reason == ("Completed from the sequence of FULL01, identical over all 470 positions "
                                    "both cover and determining 9 more base(s) (470 -> 479); FULL01 is merged "
                                    "into GRW02 in the same edition")
        assert events[1].reason == ("Identical to GRW02 over all 470 positions both cover; the name GRW02 is "
                                    "kept and its sequence completed from FULL01")
        for ev in events:
            lh.apply(store, ev)
        (row,) = store["lineages"]
        assert row["LINEAGE_NAME"] == "GRW02" and row["SEQUENCE"] == FULL
        assert sum(1 for r in store["host_records"] if r["LINEAGE_NAME"] == "GRW02") == 7

    def test_absorb_of_a_duplicate_updates_no_sequence(self):
        store = _pair_store()
        rows = _sheet(store, TURGUL01=("absorb", "ACCBRE02", ""))       # keep the weaker name
        events, _ = sr.decisions_to_events(rows, store, "d", "r", "s")
        assert [(e.event, e.lineage, e.new_name) for e in events] == [("merged_into", "ACCBRE02", "TURGUL01")]
        assert events[0].reason == "Identical to TURGUL01 over all 479 positions both cover; the name TURGUL01 is kept"

    def test_chain_follows_to_the_survivor(self):
        store = _store([_lineage("A", SHORT), _lineage("B", PARTIAL), _lineage("C", FULL)])
        rows = _sheet(store, A=("merge", "B", ""), B=("merge", "C", ""))
        events, notes = sr.decisions_to_events(rows, store, "d", "r", "s")
        assert {(e.lineage, e.new_name) for e in events} == {("A", "C"), ("B", "C")}
        a = [e for e in events if e.lineage == "A"][0]
        assert a.reason.endswith("; B was itself merged into C in the same edition")
        for ev in events:
            lh.apply(store, ev)
        assert [r["LINEAGE_NAME"] for r in store["lineages"]] == ["C"]

    @pytest.mark.parametrize("decisions, message", [
        ({"TURGUL01": ("remove", "", "")}, "needs CURATOR_NOTE"),
        ({"ACCBRE01": ("merge", "", "")}, "needs INTO"),
        ({"ACCBRE01": ("absorb", "", "")}, "needs INTO"),
        ({"ACCBRE01": ("merge", "NOPE01", "")}, "not a lineage in the store"),
        ({"ACCBRE01": ("rename", "ACCBRE02", "x")}, "already a lineage"),
        ({"ACCBRE01": ("burn", "", "")}, "is not one of"),
        ({"ACCBRE01": ("merge", "ACCBRE01", "")}, "into itself"),
    ])
    def test_refusals(self, decisions, message):
        store = _pair_store()
        rows = _sheet(store, **decisions)
        with pytest.raises(sr.ReviewError, match=message):
            sr.decisions_to_events(rows, store, "d", "r", "s")

    def test_two_different_decisions_on_one_lineage_are_refused(self):
        store = _pair_store()
        rows = _sheet(store)
        mine = [r for r in rows if r["LINEAGE"] == "ACCBRE01"]
        assert len(mine) == 2
        mine[0]["DECISION"], mine[0]["INTO"] = "merge", "ACCBRE02"
        mine[1]["DECISION"], mine[1]["INTO"] = "merge", "TURGUL01"
        with pytest.raises(sr.ReviewError, match="two different decisions"):
            sr.decisions_to_events(rows, store, "d", "r", "s")
        mine[1]["INTO"] = "ACCBRE02"                                  # the same decision twice is fine
        events, _ = sr.decisions_to_events(rows, store, "d", "r", "s")
        assert len(events) == 1

    def test_absorb_conflicts_are_refused(self):
        store = _store([_lineage("A", SHORT), _lineage("B", PARTIAL), _lineage("C", FULL)],
                       [_host("A", "x", "A 2020")] * 3)
        # A absorbs B, and B is also merged elsewhere: B decided twice
        rows = _sheet(store, A=("absorb", "B", ""), B=("merge", "C", ""))
        with pytest.raises(sr.ReviewError, match="two different decisions"):
            sr.decisions_to_events(rows, store, "d", "r", "s")
        # A absorbs B and is itself merged away
        rows = _sheet(store, A=("merge", "C", ""))
        for r in rows:
            if (r["LINEAGE"], r["OTHER"]) == ("A", "B"):
                r["DECISION"], r["INTO"] = "absorb", "B"
        with pytest.raises(sr.ReviewError, match="absorbs B but is itself merge C"):
            sr.decisions_to_events(rows, store, "d", "r", "s")

    def test_merge_into_a_removed_lineage_is_refused(self):
        store = _store([_lineage("A", SHORT), _lineage("B", FULL)])
        rows = _sheet(store, A=("merge", "B", ""))
        rows.append({"KIND": "contained", "LINEAGE": "B", "OTHER": "", "DECISION": "remove",
                     "INTO": "", "CURATOR_NOTE": "gone"})
        with pytest.raises(sr.ReviewError, match="which the sheet removes"):
            sr.decisions_to_events(rows, store, "d", "r", "s")
