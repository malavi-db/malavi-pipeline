"""Fixtures that apply to every test in this directory.

The one fixture here exists because of a bug found on 2026-10-12 and traced on
2026-10-14: three test files (close_submission, notify_submitters, promote) ran their
program's ``main()`` against a temporary inbox, and the program then called
``public_feeds.refresh()``, which rebuilds the REAL feed files in ``docs/`` and runs the
REAL publisher, so every run of the suite committed and pushed to the public site
repository (398 feed publishes between 2026-09-23 and 2026-10-14, most of them from
tests; the content was always the real feeds, so nothing false was published). The
programs import the ``public_feeds`` module and look ``refresh`` up on it at call time,
so replacing the attribute here reaches every program the tests load.

``test_public_feeds.py`` tests ``refresh`` itself and stubs the subprocess layer; it
opts out through the module-name check below. ``public_feeds.refresh`` also refuses to
run under pytest on its own (it checks ``PYTEST_CURRENT_TEST``), so a future test that
somehow bypasses this fixture still publishes nothing.
"""
from __future__ import annotations

import pytest

from malavi_curation import public_feeds

# The one module allowed to call the real refresh (with subprocess.run stubbed).
TESTS_REFRESH_ITSELF = "test_public_feeds"


@pytest.fixture(autouse=True)
def mail_mode_live(monkeypatch):
    """The mail mode (report_delivery.MAIL_MODE_ENV) is LIVE for every test by default.

    Since 2026-10-16 an unset MALAVI_MAIL_MODE means dry: nothing that carries mail is
    posted. The suite's delivery tests exercise the live path through an injected
    transport (no network, no deployed script), so they set the mode here; the tests of
    the guard itself override it with their own monkeypatch.setenv / delenv.
    """
    monkeypatch.setenv("MALAVI_MAIL_MODE", "live")
    yield


@pytest.fixture(autouse=True)
def no_public_feeds(request, monkeypatch):
    """Replace public_feeds.refresh with a stub for every test but the ones that test it."""
    if request.node.module.__name__.rsplit(".", 1)[-1] == TESTS_REFRESH_ITSELF:
        yield None
        return

    calls = []

    def stub(dry_run: bool = False, publish: bool = True, python=None) -> bool:
        calls.append({"dry_run": dry_run, "publish": publish})
        print("  [test] public feeds not rebuilt or published (stubbed by conftest).")
        return True

    monkeypatch.setattr(public_feeds, "refresh", stub)
    yield calls
