"""Backward-compat shim - canonical location is routes/task/task_routes.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.task_routes``, ``from routes.task_routes import X``,
``importlib.import_module("routes.task_routes")``, and
``monkeypatch.setattr(routes.task_routes, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.task import task_routes as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
