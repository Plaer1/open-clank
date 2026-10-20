"""S12 — canonical route identity aliases (import identity contract).

Every legacy flat-path module must resolve to the *same* module object as its
canonical package sibling so monkeypatch/shared state works and every endpoint
registers exactly once. Mirrors tests/test_gallery_routes_shim.py.
"""

import importlib

import routes.document_helpers  # noqa: F401
import routes.document_routes  # noqa: F401
import routes.mcp_routes  # noqa: F401
import routes.search_routes  # noqa: F401
import routes.task_routes  # noqa: F401
import routes.vault_routes  # noqa: F401
import routes.webhook_routes  # noqa: F401


_PAIRS = [
    ("routes.document_routes", "routes.document.document_routes"),
    ("routes.document_helpers", "routes.document.document_helpers"),
    ("routes.mcp_routes", "routes.mcp.mcp_routes"),
    ("routes.search_routes", "routes.search.search_routes"),
    ("routes.task_routes", "routes.task.task_routes"),
    ("routes.vault_routes", "routes.vault.vault_routes"),
    ("routes.webhook_routes", "routes.webhook.webhook_routes"),
]


def test_legacy_and_canonical_module_are_same_object():
    for legacy_name, canonical_name in _PAIRS:
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(canonical_name)
        assert legacy is canonical, (
            f"{legacy_name} must resolve to the same object as {canonical_name}"
        )


def test_monkeypatch_via_legacy_path_affects_canonical(monkeypatch):
    for legacy_name, canonical_name in _PAIRS:
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(canonical_name)
        sentinel = object()
        attr = next(iter(k for k in legacy.__dict__ if not k.startswith("_") and k != "sys"), "setup")
        monkeypatch.setattr(legacy, attr, sentinel)
        assert getattr(canonical, attr) is sentinel, (
            f"monkeypatch via {legacy_name}.{attr} did not reach {canonical_name}"
        )


def test_expected_setup_exports_exist():
    expected = {
        "routes.document.document_routes": "setup_document_routes",
        "routes.mcp.mcp_routes": "setup_mcp_routes",
        "routes.search.search_routes": "setup_search_routes",
        "routes.task.task_routes": "setup_task_routes",
        "routes.vault.vault_routes": "setup_vault_routes",
        "routes.webhook.webhook_routes": "setup_webhook_routes",
    }
    for mod_name, fn_name in expected.items():
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, fn_name), f"{mod_name} missing {fn_name}"
