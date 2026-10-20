"""Cross-language checks for the additive Frankenmemory v2 contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.memory_scope import CanonicalScope, effective_scope_set
from src.memory_provider import MemoryScope, MemoryScopeError


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "frankenmemory" / "v2"


def _fixture(name: str) -> dict:
    return json.loads((CONTRACT / "fixtures" / name).read_text(encoding="utf-8"))


def test_contract_bundle_and_golden_fixtures_are_json() -> None:
    bundle = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
    assert bundle["$id"].endswith("frankenmemory/v2/schema.json")
    assert bundle["$defs"]["typed_value"]["oneOf"]
    assert _fixture("scope-project.json")["contract_version"] == "frankenmemory.v2"
    assert _fixture("open-question.json")["payload"]["value"] == {"type": "null"}


def test_effective_scope_set_is_owner_bound_and_ordered() -> None:
    scopes = effective_scope_set("e", "workspace", "project")
    assert scopes == [
        CanonicalScope("e"),
        CanonicalScope("e", "workspace"),
        CanonicalScope("e", "workspace", "project"),
    ]
    assert all(scope.owner_id == "e" for scope in scopes)


def test_project_without_workspace_fails_at_python_boundary() -> None:
    with pytest.raises(ValueError, match="requires workspace"):
        effective_scope_set("e", None, "project")


def test_memory_scope_exposes_same_exact_scopes() -> None:
    scope = MemoryScope(owner="e", workspace_id="w", project_id="p")
    assert [item.storage_keys for item in scope.exact_scopes()] == [
        ("e", "", ""),
        ("e", "w", ""),
        ("e", "w", "p"),
    ]


def test_memory_scope_still_requires_authenticated_workspace() -> None:
    with pytest.raises(MemoryScopeError):
        MemoryScope(owner="", workspace_id="w")
