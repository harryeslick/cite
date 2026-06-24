"""Shared test fixtures.

The central library (``ops.CENTRAL_PATH``, ~/.cite by default) is written to by
every successful ``cite add`` and read by smart-add. Left unmocked, the test suite
would create/pollute the developer's real per-user central library *and* leak state
between tests (a second add of the same bytes would import from central instead of
hitting the duplicate gate). Redirect it to a per-test throwaway dir everywhere.
"""

import pytest

from cite import ops


@pytest.fixture(autouse=True)
def isolate_central(tmp_path_factory, monkeypatch):
    """Point the central library at a fresh empty dir for every test."""
    central = tmp_path_factory.mktemp("central-isolated")
    monkeypatch.setattr(ops, "CENTRAL_PATH", central)
    return central
