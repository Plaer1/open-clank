"""Backward-compat shim - canonical location is routes/search/search_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.search_routes``, ``from routes.search_routes import X``,
``importlib.import_module("routes.search_routes")``, and
``monkeypatch.setattr(routes.search_routes, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.search import search_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
