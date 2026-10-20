"""Backward-compat shim - canonical location is routes/vault/vault_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.vault_routes``, ``from routes.vault_routes import X``,
``importlib.import_module("routes.vault_routes")``, and
``monkeypatch.setattr(routes.vault_routes, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.vault import vault_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
