"""Backward-compat shim - canonical location is routes/document/document_helpers.py.

This module is replaced in ``sys.modules`` by the canonical module object so
that ``import routes.document_helpers``, ``from routes.document_helpers import X``,
``importlib.import_module("routes.document_helpers")``, and
``monkeypatch.setattr(routes.document_helpers, ...)`` all operate on the same
object the application actually uses. Keeps existing import paths working
after S12 canonical route identity.
"""

import sys as _sys

from routes.document import document_helpers as _canonical  # noqa: F401

_sys.modules[__name__] = _canonical
