from __future__ import annotations

import pytest

from src.clanker_paths import (
    ClankerPathError,
    canonical_plan_path,
    canonical_relative_path,
    canonical_robonote_path,
    project_root_for_contract,
)


def test_legacy_plan_paths_translate_only_at_namespace_boundary() -> None:
    assert canonical_plan_path(".futures/plan.md") == ".clanker/futures/plan.md"
    assert canonical_plan_path(".clanker/futures/plan.md") == ".clanker/futures/plan.md"
    with pytest.raises(ClankerPathError):
        canonical_plan_path("docs/.futures/plan.md")


def test_robonote_legacy_paths_translate_to_singular_clanker() -> None:
    assert canonical_robonote_path(".robonotes/run.md") == ".clanker/robonotes/run.md"
    assert canonical_robonote_path("robonotes/run.md") == ".clanker/robonotes/run.md"
    assert canonical_relative_path(".clankers/robonotes/run.md") == ".clanker/robonotes/run.md"


@pytest.mark.parametrize("category", ["archive", "futures", "hexes", "robonotes"])
def test_canonical_artifacts_use_one_singular_root(category: str) -> None:
    relative = f".clanker/{category}/nested/artifact.md"
    assert canonical_relative_path(relative, allow_legacy=False) == relative


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        (".archive/run.md", ".clanker/archive/run.md"),
        (".clankers/archive/run.md", ".clanker/archive/run.md"),
        (".clankers/hexes/contract.yaml", ".clanker/hexes/contract.yaml"),
        (".clankers/robonotes/run.md", ".clanker/robonotes/run.md"),
        (".robonotes/run.md", ".clanker/robonotes/run.md"),
        (".futures/run.md", ".clanker/futures/run.md"),
    ],
)
def test_legacy_inputs_normalize_but_strict_writes_reject_them(legacy, canonical) -> None:
    assert canonical_relative_path(legacy) == canonical
    with pytest.raises(ClankerPathError):
        canonical_relative_path(legacy, allow_legacy=False)


@pytest.mark.parametrize("layout", [".clanker", ".clankers"])
def test_contract_root_preserves_nested_project_boundary(tmp_path, layout) -> None:
    project = tmp_path / ".clanker" / "hexes" / "nested-project"
    contract = project / layout / "hexes" / "contract.yaml"
    assert project_root_for_contract(contract) == project
    assert project_root_for_contract(contract.parent / "contracts" / "manual.yaml") == project
    assert project_root_for_contract(project / ".hex") == project


@pytest.mark.parametrize(
    "value",
    [
        ".clankers/futures/plan.md",
        ".clanker/hexes/../contract.yaml",
        ".clanker/robonotes//run.md",
        ".futures/../escape.md",
        ".futures\\plan.md",
        "/tmp/plan.md",
        "C:/plan.md",
        ".futures/plan\x00.md",
    ],
)
def test_invalid_namespaces_and_path_forms_fail_closed(value: str) -> None:
    with pytest.raises(ClankerPathError):
        canonical_relative_path(value)
