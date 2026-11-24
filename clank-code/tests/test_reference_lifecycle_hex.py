from __future__ import annotations

import shutil
from pathlib import Path

from src.hex_contract import evaluate_contract


def _contract(root: Path) -> Path:
    checks = root / ".clanker/hexes/checks"
    checks.mkdir(parents=True)
    shutil.copy(Path(__file__).parents[1] / ".clanker/hexes/checks/project_checks.py", checks / "project_checks.py")
    contract = root / ".clanker/hexes/contract.yaml"
    contract.write_text(
        "contract: open-clank-hexes/v2\nhexes:\n"
        "  - hex: lifecycle\n    in: ./.clanker/robonotes/references/lifecycle-*.md\n"
        "    reference_lifecycle: {references_root: .references, deferred_prefix: .references/deferred/, lore_root: .clanker/robonotes/}\n"
        "  - hex: no reference runtime\n    in: ./routes/**\n    no_reference_runtime_dependency: .references\n",
        encoding="utf-8",
    )
    return contract


def _record(root: Path, name: str, state: str, path: str, extra: str = "") -> str:
    lore = root / ".clanker/robonotes/references"
    lore.mkdir(parents=True, exist_ok=True)
    (lore / "anchor.md").write_text("anchor\n", encoding="utf-8")
    target = lore / name
    target.write_text(
        "---\nreference_path: " + path + "\nowner: e\ntask: reference-retirement\nstate: " + state
        + "\nlore_anchor: .clanker/robonotes/references/anchor.md\nprovenance_hash: sha256:" + "a" * 64 + "\n" + extra + "---\nrecord\n",
        encoding="utf-8",
    )
    return target.relative_to(root).as_posix()


def test_active_worktree_record_is_allowed(tmp_path: Path):
    contract = _contract(tmp_path)
    record = _record(tmp_path, "lifecycle-active.md", "active", ".references/upstream/worktree")
    assert evaluate_contract(contract, root=tmp_path, files=[record])["allowed"]


def test_deferred_record_requires_focused_path_process_and_resumption(tmp_path: Path):
    contract = _contract(tmp_path)
    record = _record(tmp_path, "lifecycle-deferred.md", "deferred", ".references/deferred/s28", "resumption_plan: S28\nreview_or_retirement: review-2026-10-01\nprocess_state: none-running\n")
    assert evaluate_contract(contract, root=tmp_path, files=[record])["allowed"]
    bad = _record(tmp_path, "lifecycle-bad.md", "deferred", ".references/s28", "process_state: running\n")
    result = evaluate_contract(contract, root=tmp_path, files=[bad])
    assert not result["allowed"]
    assert "deferred" in result["findings"][0]["details"][0]


def test_retired_record_requires_absence_and_runtime_paths_cannot_reference(tmp_path: Path):
    contract = _contract(tmp_path)
    (tmp_path / ".references/old").mkdir(parents=True)
    record = _record(tmp_path, "lifecycle-retired.md", "retired", ".references/old", "retirement_receipt: .clanker/robonotes/references/anchor.md\n")
    result = evaluate_contract(contract, root=tmp_path, files=[record])
    assert not result["allowed"]
    (tmp_path / "routes").mkdir()
    runtime = tmp_path / "routes/runtime.py"
    runtime.write_text("ROOT = '/workspace/open-clank/.references/input'\n", encoding="utf-8")
    result = evaluate_contract(contract, root=tmp_path, files=["routes/runtime.py"])
    assert not result["allowed"]


def test_runtime_symlink_into_references_is_blocked(tmp_path: Path):
    contract = _contract(tmp_path)
    reference = tmp_path / ".references/payload.py"
    reference.parent.mkdir(parents=True)
    reference.write_text("payload\n", encoding="utf-8")
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes/link.py").symlink_to(reference)
    result = evaluate_contract(contract, root=tmp_path, files=["routes/link.py"])
    assert not result["allowed"]
    assert "resolves inside .references/" in result["findings"][0]["details"][0]
