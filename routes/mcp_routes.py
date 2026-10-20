"""Backward-compat shim - canonical location is routes/mcp/mcp_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.mcp_routes``, ``from routes.mcp_routes import X``,
``importlib.import_module("routes.mcp_routes")``, and
``monkeypatch.setattr(routes.mcp_routes, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.mcp import mcp_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
