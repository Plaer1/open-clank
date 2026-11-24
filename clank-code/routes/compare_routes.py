"""Backward-compat shim — canonical location is routes/compare/compare_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.compare_routes``, ``from routes.compare_routes import X``,
``importlib.import_module("routes.compare_routes")``, and the
``import ... as cr`` + ``monkeypatch.setattr(cr, "SessionLocal", ...)`` pattern
used by route tests all operate on the *same* object the application actually
uses. Keeps existing import paths working after slice 2i (#4082/#4071).
"""

import sys as _sys

from routes.compare import compare_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
