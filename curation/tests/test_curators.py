"""Tests for the curator registry (curators.py).

The registry is what turns "this person controls this mailbox" — which is all Google's
verified-email collection establishes — into "this person may decide what goes into
MalAvi". Every test here is about a way that translation could go wrong quietly.
"""
import pytest
import yaml

from malavi_curation.curators import (
    active_curators, leads, load_registry, normalize_email, resolve,
)


def write_registry(tmp_path, curators):
    """Write a registry file and return its path."""
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": curators}), encoding="utf-8")
    return path


BASIC = [
    {"id": "vellis", "name": "Vincenzo Ellis", "email": "vaellis@udel.edu",
     "role": "lead", "active": True, "aliases": ["lead.personal@example.com"]},
    {"id": "someone", "name": "Given Family", "email": "someone@example.edu",
     "role": "curator", "active": True},
]


def test_resolves_primary_address(tmp_path):
    path = write_registry(tmp_path, BASIC)
    assert resolve("someone@example.edu", path).id == "someone"


def test_resolves_alias_address(tmp_path):
    """A curator signed into their personal Google account is still that curator."""
    path = write_registry(tmp_path, BASIC)
    assert resolve("lead.personal@example.com", path).id == "vellis"


def test_address_matching_ignores_case_and_whitespace(tmp_path):
    path = write_registry(tmp_path, BASIC)
    assert resolve("  Someone@Example.EDU  ", path).id == "someone"


def test_unknown_address_resolves_to_none(tmp_path):
    """The return value the whole authorization model rests on."""
    path = write_registry(tmp_path, BASIC)
    assert resolve("stranger@example.com", path) is None


def test_gmail_dots_are_not_collapsed(tmp_path):
    """Deliberate: a provider-specific rule would be wrong for the next curator."""
    assert normalize_email("a.b@gmail.com") != normalize_email("ab@gmail.com")


def test_lead_role_grants_override_power(tmp_path):
    path = write_registry(tmp_path, BASIC)
    assert resolve("vaellis@udel.edu", path).is_lead
    assert not resolve("someone@example.edu", path).is_lead


def test_retired_lead_is_not_a_lead(tmp_path):
    """Retiring someone must remove their authority without erasing their history."""
    path = write_registry(tmp_path, [
        {"id": "past", "name": "Past Lead", "email": "past@example.edu",
         "role": "lead", "active": False},
    ])
    curator = resolve("past@example.edu", path)
    assert curator is not None          # still resolvable, so old verdicts stay attributed
    assert not curator.is_lead
    assert leads(path) == []
    assert active_curators(path) == []


def test_duplicate_id_is_rejected(tmp_path):
    path = write_registry(tmp_path, [
        {"id": "dup", "email": "a@example.edu"},
        {"id": "dup", "email": "b@example.edu"},
    ])
    with pytest.raises(ValueError, match="duplicate curator id"):
        load_registry(path)


def test_address_claimed_by_two_curators_is_rejected(tmp_path):
    """Otherwise attribution is ambiguous, and only on a verdict that mattered."""
    path = write_registry(tmp_path, [
        {"id": "one", "email": "shared@example.edu"},
        {"id": "two", "email": "other@example.edu", "aliases": ["shared@example.edu"]},
    ])
    with pytest.raises(ValueError, match="is listed for both"):
        load_registry(path)


def test_unknown_role_is_rejected_rather_than_downgraded(tmp_path):
    path = write_registry(tmp_path, [
        {"id": "typo", "email": "t@example.edu", "role": "leed"},
    ])
    with pytest.raises(ValueError, match="unknown role"):
        load_registry(path)


def test_curator_without_email_is_rejected(tmp_path):
    path = write_registry(tmp_path, [{"id": "nobody", "name": "No Address"}])
    with pytest.raises(ValueError, match="no email address"):
        load_registry(path)


def test_missing_registry_raises_rather_than_allowing_everyone(tmp_path):
    with pytest.raises(FileNotFoundError, match="Curator registry not found"):
        load_registry(tmp_path / "does_not_exist.yml")


def test_shipped_registry_parses():
    """The real config/curators.yml must load; a broken one blocks every verdict."""
    registry = load_registry()
    assert registry, "the shipped registry is empty"
    assert any(c.is_lead for c in registry.values()), "no active lead curator"


def test_quoted_false_does_not_leave_a_retired_curator_empowered(tmp_path):
    """REGRESSION: bool("false") is True, and this is an authorization control."""
    path = tmp_path / "curators.yml"
    path.write_text('curators:\n  - id: past\n    email: p@example.edu\n'
                    '    active: "false"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must be true or false"):
        load_registry(path)


def test_registry_is_re_read_after_an_edit(tmp_path):
    """REGRESSION: an mtime-keyed cache is unsafe on NFS, where mtime can be stale."""
    path = write_registry(tmp_path, BASIC)
    assert resolve("someone@example.edu", path).active
    write_registry(tmp_path, [dict(BASIC[1], active=False)])
    assert not resolve("someone@example.edu", path).active


def test_mutating_the_returned_registry_does_not_poison_the_next_call(tmp_path):
    path = write_registry(tmp_path, BASIC)
    load_registry(path).clear()
    assert load_registry(path), "a caller's mutation leaked into the next read"


def test_null_review_block_does_not_break_registry_lookup(tmp_path, monkeypatch):
    """A commented-out `review:` body used to raise AttributeError from an unrelated file."""
    from malavi_curation import curators as mod
    monkeypatch.setattr(mod, "load_config", lambda: {"review": None})
    assert mod.registry_path().name == "curators.yml"
