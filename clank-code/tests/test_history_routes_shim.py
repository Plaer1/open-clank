"""Regression test for the history route shim (slice 2d, #4082/#4071).

The backward-compat shim at ``routes/history_routes.py`` uses ``sys.modules``
replacement so the legacy import path and the canonical ``routes.history.*``
path resolve to the *same* module object. This is required because
``test_history_compact_tool_calls.py`` and ``test_fork_session_metadata.py``
do ``import routes.history_routes as history_routes`` followed by
``monkeypatch.setattr(history_routes, "_verify_session_owner", ...)`` — for
those patches to take effect at runtime, the legacy module object and the
canonical one must be identical. This test pins that contract.
"""

import importlib

import routes.history_routes as _shim_history  # noqa: F401


def test_legacy_and_canonical_history_module_are_same_object():
    """``import routes.history_routes`` must alias the canonical module."""
    legacy = importlib.import_module("routes.history_routes")
    canonical = importlib.import_module("routes.history.history_routes")
    assert legacy is canonical, (
        "routes.history_routes shim must resolve to the canonical "
        "routes.history.history_routes module object"
    )
