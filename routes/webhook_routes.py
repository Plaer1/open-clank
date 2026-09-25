"""Backward-compat shim - canonical location is routes/webhook/webhook_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.webhook_routes``, ``from routes.webhook_routes import X``,
``importlib.import_module("routes.webhook_routes")``, and
``monkeypatch.setattr(routes.webhook_routes, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.webhook import webhook_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
