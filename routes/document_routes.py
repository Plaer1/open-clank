"""Backward-compat shim - canonical location is routes/document/document_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.document_routes``, ``from routes.document_routes import X``,
``importlib.import_module("routes.document_routes")``, and
``monkeypatch.setattr(routes.document_routes, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.document import document_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
