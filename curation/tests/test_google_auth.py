"""Tests for read-only Google credential handling (google_auth.py).

The behavior under test is mostly about what happens when things are NOT set up, because
that is the state with the dangerous failure mode: a fetch job that cannot authenticate
reads nothing and reports an empty inbox, which is indistinguishable from a week in which
nobody submitted anything.
"""
import json

import pytest

from malavi_curation import google_auth
from malavi_curation.google_auth import CredentialError
from malavi_curation.config import repo_root


def test_env_var_overrides_the_configured_path(tmp_path, monkeypatch):
    key = tmp_path / "service-account.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(google_auth.ENV_VAR, str(key))
    assert google_auth.key_path() == key.resolve()


def test_no_credential_configured_returns_none(monkeypatch):
    monkeypatch.setenv(google_auth.ENV_VAR, "")
    monkeypatch.setattr(google_auth, "load_config", lambda: {"google": {}})
    assert google_auth.key_path() is None
    assert google_auth.access_token() is None


def test_a_key_inside_the_repository_is_refused(monkeypatch):
    """Not fussiness: a bearer credential in git history is permanent.

    Relying on .gitignore means relying on nobody ever running `git add -f`, nobody
    copying it to a differently-named file, and every clone keeping the same rules.
    """
    inside = repo_root() / "service-account.json"
    monkeypatch.setenv(google_auth.ENV_VAR, str(inside))
    with pytest.raises(CredentialError, match="inside the repository"):
        google_auth.key_path()


def test_a_key_in_a_repository_subdirectory_is_also_refused(monkeypatch):
    inside = repo_root() / "curation" / "src" / "creds.json"
    monkeypatch.setenv(google_auth.ENV_VAR, str(inside))
    with pytest.raises(CredentialError, match="inside the repository"):
        google_auth.key_path()


def test_a_configured_but_absent_key_raises_rather_than_returning_none(tmp_path,
                                                                      monkeypatch):
    """"Configured but missing" is a mistake to report, not an absence to tolerate."""
    monkeypatch.setenv(google_auth.ENV_VAR, str(tmp_path / "nope.json"))
    with pytest.raises(CredentialError, match="No Google key at"):
        google_auth.access_token()


def test_the_service_account_address_is_readable_from_the_key(tmp_path, monkeypatch):
    """The address is what the Drive folders must be shared with, and is not otherwise
    visible to whoever is doing the sharing."""
    key = tmp_path / "service-account.json"
    key.write_text(json.dumps({"client_email": "malavi-fetcher@malavi.iam.example"}),
                   encoding="utf-8")
    monkeypatch.setenv(google_auth.ENV_VAR, str(key))
    assert google_auth.service_account_email() == "malavi-fetcher@malavi.iam.example"
    assert "malavi-fetcher@malavi.iam.example" in google_auth.describe()


def test_a_corrupt_key_does_not_raise_when_only_the_address_is_wanted(tmp_path,
                                                                     monkeypatch):
    key = tmp_path / "service-account.json"
    key.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(google_auth.ENV_VAR, str(key))
    assert google_auth.service_account_email() is None


def test_describe_never_raises(monkeypatch):
    """It is printed before a job starts; it must not be the thing that stops one."""
    monkeypatch.setenv(google_auth.ENV_VAR, str(repo_root() / "inside.json"))
    assert "MISCONFIGURED" in google_auth.describe()


def test_scope_is_read_only():
    """A fetch job has no business writing to Drive."""
    assert google_auth.SCOPES == ("https://www.googleapis.com/auth/drive.readonly",)
    assert all("readonly" in scope for scope in google_auth.SCOPES)


def test_the_configured_default_path_is_outside_the_repository():
    """The shipped config must not point somewhere the loader will refuse."""
    from malavi_curation.config import load_config
    from pathlib import Path

    configured = (load_config().get("google") or {}).get("service_account_key")
    assert configured, "config/project.yml has no google.service_account_key"
    resolved = Path(str(configured)).expanduser().resolve()
    with pytest.raises(ValueError):
        resolved.relative_to(repo_root().resolve())
