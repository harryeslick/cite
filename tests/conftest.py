"""Shared test fixtures.

The central library (``ops.CENTRAL_PATH``, ~/.cite by default) is written to by
every successful ``cite add`` and read by smart-add. Left unmocked, the test suite
would create/pollute the developer's real per-user central library *and* leak state
between tests (a second add of the same bytes would import from central instead of
hitting the duplicate gate). Redirect it to a per-test throwaway dir everywhere.
"""

import pytest

from cite import ops
from cite.search import net
from cite.store import Library


@pytest.fixture(autouse=True)
def isolate_central(tmp_path_factory, monkeypatch):
    """Point the central library at a fresh empty dir for every test."""
    central = tmp_path_factory.mktemp("central-isolated")
    monkeypatch.setattr(ops, "CENTRAL_PATH", central)
    return central


@pytest.fixture(autouse=True)
def isolate_search_identity(tmp_path_factory, monkeypatch):
    """Give every test an unconfigured search identity.

    Same reasoning as ``isolate_central``: the contact email and OpenAlex key
    resolve from the developer's environment *and* from whatever ``cite.toml``
    the upward walk lands on, so a suite run from inside a real library would
    otherwise read that library's settings — and assertions about the
    unconfigured default would pass or fail depending on whose machine it is.
    Tests that want a configured identity set the env var or build a library and
    re-point ``net.resolve_library`` themselves.
    """
    monkeypatch.delenv(net.CONTACT_EMAIL_ENV, raising=False)
    monkeypatch.delenv(net.OPENALEX_API_KEY_ENV, raising=False)
    empty = tmp_path_factory.mktemp("no-cite-toml")
    monkeypatch.setattr(net, "resolve_library", lambda _: Library(empty))
