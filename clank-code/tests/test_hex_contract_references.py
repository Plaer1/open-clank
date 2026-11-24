from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.hex_contract import CandidateDiff, evaluate_contract


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _contract(root: Path, parameter: str = "['.references']") -> Path:
    path = root / ".hex"
    path.write_text(
        "contract: open-clank-hexes/v2\nhexes:\n"
        "  - hex: References never enter Git\n"
        f"    untracked_only: {parameter}\n",
        encoding="utf-8",
    )
    return path


def _reference(root: Path, name: str = "study.txt") -> Path:
    path = root / ".references" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("local study material", encoding="utf-8")
    return path


@pytest.mark.parametrize("stage", ["check", "pre-commit", "pre-push"])
def test_force_added_ignored_reference_is_blocked_in_every_stage(tmp_path, stage):
    _git(tmp_path, "init", "-q")
    contract = _contract(tmp_path)
    (tmp_path / ".gitignore").write_text(".references/\n", encoding="utf-8")
    _reference(tmp_path, "name with spaces.txt")
    _git(tmp_path, "add", "-f", "--", ".references")

    # Normal discovery excludes .references; the index check must not depend
    # on discovery finding either its files or its directory.
    result = evaluate_contract(contract, root=tmp_path, files=[], stage=stage)
    assert not result["allowed"]
    assert ".references/name with spaces.txt" in result["findings"][0]["details"][0]


def test_untracked_references_and_similar_tracked_name_are_allowed(tmp_path):
    _git(tmp_path, "init", "-q")
    contract = _contract(tmp_path)
    _reference(tmp_path)
    sibling = tmp_path / ".references-old"
    sibling.mkdir()
    (sibling / "allowed.txt").write_text("ordinary tracked file", encoding="utf-8")
    _git(tmp_path, "add", "--", ".references-old")
    result = evaluate_contract(
        contract, root=tmp_path, files=[".references/study.txt"]
    )
    assert result["allowed"]


def test_gitlink_is_blocked_without_submodule_checkout(tmp_path):
    _git(tmp_path, "init", "-q")
    contract = _contract(tmp_path)
    _git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "commit", "--allow-empty", "-qm", "fixture")
    commit = _git(tmp_path, "rev-parse", "HEAD").decode().strip()
    _git(tmp_path, "update-index", "--add", "--cacheinfo",
         f"160000,{commit},.references/upstream")
    assert not (tmp_path / ".references").exists()
    result = evaluate_contract(contract, root=tmp_path, files=[])
    assert not result["allowed"]
    assert ".references/upstream" in result["findings"][0]["details"][0]


def test_shadow_evaluation_uses_source_index_not_candidate_file_list(tmp_path):
    source, shadow = tmp_path / "source", tmp_path / "shadow"
    source.mkdir()
    shadow.mkdir()
    _git(source, "init", "-q")
    contract = _contract(shadow)
    _reference(source)
    _git(source, "add", "--", ".references")
    diff = CandidateDiff(shadow, source, added=frozenset({"ordinary.txt"}))
    result = evaluate_contract(contract, root=shadow, files=[], diff=diff)
    assert not result["allowed"]

    # A new untracked reference candidate is legal; changed != staged.
    _git(source, "rm", "--cached", "-q", "--", ".references/study.txt")
    diff = CandidateDiff(shadow, source, added=frozenset({".references/new.txt"}))
    result = evaluate_contract(
        contract, root=shadow, files=[".references/new.txt"], diff=diff
    )
    assert result["allowed"]


def test_deleting_worktree_file_does_not_hide_still_tracked_reference(tmp_path):
    _git(tmp_path, "init", "-q")
    contract = _contract(tmp_path)
    reference = _reference(tmp_path)
    _git(tmp_path, "add", "--", ".references")
    reference.unlink()
    result = evaluate_contract(contract, root=tmp_path, files=[])
    assert not result["allowed"]


def test_non_git_workspace_does_not_require_subprocess(tmp_path, monkeypatch):
    contract = _contract(tmp_path)
    _reference(tmp_path)

    def unavailable(*args, **kwargs):
        raise AssertionError("non-Git workspace must not launch Git")

    monkeypatch.setattr("src.hex_contract.subprocess.run", unavailable)
    assert evaluate_contract(contract, root=tmp_path, files=[])["allowed"]


def test_unavailable_git_index_fails_closed(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    contract = _contract(tmp_path)

    def unavailable(*args, **kwargs):
        raise PermissionError("contained executable unavailable")

    monkeypatch.setattr("src.hex_contract.subprocess.run", unavailable)
    result = evaluate_contract(contract, root=tmp_path, files=[])
    assert not result["allowed"]
    assert "Git index is unavailable" in result["findings"][0]["details"][0]


@pytest.mark.parametrize("parameter", ["[]", "true", "['../outside']", "['/tmp']", "['.references/**']"])
def test_rule_requires_literal_relative_directories(tmp_path, parameter):
    result = evaluate_contract(_contract(tmp_path, parameter), root=tmp_path, files=[])
    assert not result["allowed"]
    assert "configure untracked_only" in result["findings"][0]["details"][0]
