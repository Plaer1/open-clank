from __future__ import annotations

import pytest

from src.clanker_paths import (
    ClankerPathError,
    canonical_plan_path,
    canonical_relative_path,
    canonical_robonote_path,
)


def test_legacy_plan_paths_translate_only_at_namespace_boundary() -> None:
    assert canonical_plan_path(".futures/plan.md") == ".clanker/futures/plan.md"
    assert canonical_plan_path(".clanker/futures/plan.md") == ".clanker/futures/plan.md"
    with pytest.raises(ClankerPathError):
        canonical_plan_path("docs/.futures/plan.md")


def test_robonote_legacy_paths_translate_to_plural_clankers() -> None:
    assert canonical_robonote_path(".robonotes/run.md") == ".clankers/robonotes/run.md"
    assert canonical_robonote_path("robonotes/run.md") == ".clankers/robonotes/run.md"
    assert canonical_relative_path(".clankers/robonotes/run.md") == ".clankers/robonotes/run.md"


@pytest.mark.parametrize(
    "value",
    [
        ".clankers/futures/plan.md",
        ".clanker/hexes/contract.yaml",
        ".clanker/robonotes/run.md",
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
